import time

import pytest

from mcp_proxy.config import (
    AppConfig,
    MethodPolicy,
    PoliciesConfig,
    RateLimitPolicy,
    SizePolicy,
    UpstreamPolicies,
)
from mcp_proxy.policy.engine import PolicyEngine, TokenBucket


def _config(policies: PoliciesConfig) -> AppConfig:
    return AppConfig(policies=policies)


# ---------- TokenBucket -------------------------------------------------------


def test_token_bucket_consumes_and_refuses() -> None:
    bucket = TokenBucket(rate=2.0, capacity=2)
    now = 1000.0
    assert bucket.take(now) is True
    assert bucket.take(now) is True
    assert bucket.take(now) is False


def test_token_bucket_refills_over_time() -> None:
    bucket = TokenBucket(rate=4.0, capacity=2)
    now = 1000.0
    assert bucket.take(now) is True
    assert bucket.take(now) is True
    assert bucket.take(now) is False
    # 0.5s elapsed → +2 tokens at 4 tok/s, capped at capacity=2
    assert bucket.take(now + 0.5) is True
    assert bucket.take(now + 0.5) is True
    assert bucket.take(now + 0.5) is False


# ---------- Method ACL --------------------------------------------------------


def test_method_deny_blocks_specific_method() -> None:
    engine = PolicyEngine(
        _config(
            PoliciesConfig(
                per_upstream={
                    "git": UpstreamPolicies(methods=MethodPolicy(deny=["tools/dangerous"]))
                }
            )
        )
    )
    decision = engine.check(upstream="git", message={"method": "tools/dangerous"})
    assert decision.allowed is False
    assert decision.reason == "method_denied"

    ok = engine.check(upstream="git", message={"method": "tools/safe"})
    assert ok.allowed is True


def test_method_allow_acts_as_whitelist() -> None:
    engine = PolicyEngine(
        _config(
            PoliciesConfig(
                per_upstream={
                    "git": UpstreamPolicies(methods=MethodPolicy(allow=["tools/list", "tools/call"]))
                }
            )
        )
    )
    assert engine.check(upstream="git", message={"method": "tools/list"}).allowed
    assert engine.check(upstream="git", message={"method": "tools/call"}).allowed
    blocked = engine.check(upstream="git", message={"method": "secret/exec"})
    assert blocked.allowed is False
    assert blocked.reason == "method_denied"


def test_method_wildcards() -> None:
    engine = PolicyEngine(
        _config(
            PoliciesConfig(
                per_upstream={
                    "git": UpstreamPolicies(methods=MethodPolicy(deny=["admin/*"], allow=["tools/*"]))
                }
            )
        )
    )
    assert engine.check(upstream="git", message={"method": "tools/list"}).allowed
    blocked = engine.check(upstream="git", message={"method": "admin/restart"})
    assert blocked.allowed is False
    # Method not matching allow whitelist when allow is set
    assert engine.check(upstream="git", message={"method": "secret"}).allowed is False


def test_global_policy_applies_when_per_upstream_missing() -> None:
    engine = PolicyEngine(
        _config(
            PoliciesConfig(
                global_=UpstreamPolicies(methods=MethodPolicy(deny=["evil/*"]))
            )
        )
    )
    blocked = engine.check(upstream="anywhere", message={"method": "evil/run"})
    assert blocked.allowed is False
    assert blocked.reason == "method_denied"


# ---------- Size --------------------------------------------------------------


def test_size_policy_blocks_oversize_request() -> None:
    engine = PolicyEngine(
        _config(
            PoliciesConfig(
                per_upstream={"a": UpstreamPolicies(size=SizePolicy(max_request_bytes=100))}
            )
        )
    )
    big = engine.check(upstream="a", message={"method": "x"}, request_bytes=200)
    assert big.allowed is False
    assert big.reason == "size_exceeded"
    small = engine.check(upstream="a", message={"method": "x"}, request_bytes=50)
    assert small.allowed is True


# ---------- Rate limit --------------------------------------------------------


def test_rate_limit_per_upstream_exhausts_then_recovers() -> None:
    engine = PolicyEngine(
        _config(
            PoliciesConfig(
                per_upstream={
                    "a": UpstreamPolicies(
                        rate_limit=RateLimitPolicy(requests_per_second=2.0, burst=2)
                    )
                }
            )
        )
    )
    # Two requests succeed.
    assert engine.check(upstream="a", message={"method": "x"}).allowed
    assert engine.check(upstream="a", message={"method": "x"}).allowed
    # Third is rate limited.
    blocked = engine.check(upstream="a", message={"method": "x"})
    assert blocked.allowed is False
    assert blocked.reason and blocked.reason.startswith("rate_limited")

    # Wait for refill (real sleep — small enough to keep tests fast).
    time.sleep(0.6)
    assert engine.check(upstream="a", message={"method": "x"}).allowed


def test_rate_limit_per_client_ip_isolated_buckets() -> None:
    engine = PolicyEngine(
        _config(
            PoliciesConfig(
                per_upstream={
                    "a": UpstreamPolicies(
                        rate_limit=RateLimitPolicy(
                            requests_per_second=1.0, burst=1, scope="client_ip"
                        )
                    )
                }
            )
        )
    )
    # Each client gets its own bucket.
    assert engine.check(upstream="a", message={"method": "x"}, client_ip="1.1.1.1").allowed
    assert engine.check(upstream="a", message={"method": "x"}, client_ip="2.2.2.2").allowed
    blocked = engine.check(upstream="a", message={"method": "x"}, client_ip="1.1.1.1")
    assert blocked.allowed is False


# ---------- Hot reload --------------------------------------------------------


def test_replace_config_updates_policies_atomically() -> None:
    initial = _config(
        PoliciesConfig(per_upstream={"a": UpstreamPolicies(methods=MethodPolicy(deny=["foo"]))})
    )
    engine = PolicyEngine(initial)
    assert engine.check(upstream="a", message={"method": "foo"}).allowed is False

    updated = _config(
        PoliciesConfig(per_upstream={"a": UpstreamPolicies(methods=MethodPolicy(deny=["bar"]))})
    )
    engine.replace_config(updated)
    assert engine.check(upstream="a", message={"method": "foo"}).allowed is True
    assert engine.check(upstream="a", message={"method": "bar"}).allowed is False


def test_evict_idle_buckets_removes_stale_client_buckets() -> None:
    engine = PolicyEngine(
        _config(
            PoliciesConfig(
                per_upstream={
                    "a": UpstreamPolicies(
                        rate_limit=RateLimitPolicy(
                            requests_per_second=1.0, burst=1, scope="client_ip"
                        )
                    )
                }
            )
        )
    )
    engine.check(upstream="a", message={"method": "x"}, client_ip="1.1.1.1")
    # Pretend it's been a long time.
    for k in list(engine._last_access):  # type: ignore[attr-defined]
        engine._last_access[k] = 0.0  # type: ignore[attr-defined]
    removed = engine.evict_idle_buckets(idle_seconds=1.0)
    assert removed >= 1


# ---------- Bridge integration -----------------------------------------------


@pytest.mark.asyncio
async def test_bridge_blocks_denied_request_and_records_traffic() -> None:
    from mcp_proxy.jsonrpc import JsonRpcError
    from mcp_proxy.observability.traffic import TrafficRecorder
    from mcp_proxy.proxy.base import UpstreamTransport
    from mcp_proxy.proxy.bridge import ProxyBridge
    from mcp_proxy.proxy.manager import PluginRegistry, UpstreamManager

    class OkTransport(UpstreamTransport):
        def __init__(self, name, settings):
            pass

        async def start(self):
            return None

        async def stop(self):
            return None

        async def restart(self):
            return None

        async def request(self, message):
            return {"jsonrpc": "2.0", "id": message["id"], "result": "ok"}

        async def send_notification(self, message):
            return None

        def health(self):
            return {"ok": True}

    reg = PluginRegistry()
    reg.upstreams["dummy"] = OkTransport
    manager = UpstreamManager({"a": {"type": "dummy"}}, reg)
    await manager.start()
    bridge = ProxyBridge(manager)
    recorder = TrafficRecorder()
    bridge.set_traffic_recorder(recorder.record)
    engine = PolicyEngine(
        _config(
            PoliciesConfig(
                per_upstream={"a": UpstreamPolicies(methods=MethodPolicy(deny=["forbidden"]))}
            )
        )
    )
    bridge.set_policy_engine(engine)

    with pytest.raises(JsonRpcError) as exc:
        await bridge.forward("a", {"jsonrpc": "2.0", "id": 1, "method": "forbidden"})
    assert exc.value.code == -32003
    assert "policy_blocked:method_denied" in str(exc.value.message)

    items = recorder.recent()
    assert len(items) == 1
    assert items[0]["status"] == "denied"
    assert items[0]["error_code"] == "method_denied"
