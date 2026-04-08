"""Policy engine: method ACLs, rate limiting, and size caps."""

from __future__ import annotations

import fnmatch
import time
from dataclasses import dataclass
from typing import Any

from mcp_proxy.config import (
    AppConfig,
    MethodPolicy,
    PoliciesConfig,
    RateLimitPolicy,
    SizePolicy,
    UpstreamPolicies,
)


@dataclass
class PolicyDecision:
    """Result of a policy check."""

    allowed: bool
    reason: str | None = None

    @classmethod
    def allow(cls) -> "PolicyDecision":
        return cls(True)

    @classmethod
    def deny(cls, reason: str) -> "PolicyDecision":
        return cls(False, reason)


class TokenBucket:
    """Classic token bucket.

    Configured with a steady-state rate (tokens/second) and a burst capacity.
    Each successful `take` consumes one token; refill is lazy, computed on
    demand from the monotonic clock.
    """

    __slots__ = ("rate", "capacity", "_tokens", "_last_refill")

    def __init__(self, rate: float, capacity: int, now: float | None = None) -> None:
        self.rate = rate
        self.capacity = capacity
        self._tokens: float = float(capacity)
        # Allow callers (and tests) to pin the initial timestamp so the bucket
        # is consistent with whichever clock the caller subsequently uses.
        self._last_refill: float | None = now

    def take(self, now: float | None = None) -> bool:
        """Attempt to consume one token. Returns True if allowed."""
        current = now if now is not None else time.monotonic()
        if self._last_refill is None:
            self._last_refill = current
        elapsed = current - self._last_refill
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._last_refill = current
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False

    def snapshot(self) -> dict[str, Any]:
        return {
            "rate": self.rate,
            "capacity": self.capacity,
            "tokens": round(self._tokens, 3),
        }


class PolicyEngine:
    """Evaluates per-request policies and manages in-memory rate-limit state."""

    def __init__(self, config: AppConfig | None = None) -> None:
        self._policies: PoliciesConfig = (
            config.policies if config is not None else PoliciesConfig()
        )
        # Bucket map keyed by (upstream, scope, scope_key).
        self._buckets: dict[tuple[str, str, str], TokenBucket] = {}
        self._last_access: dict[tuple[str, str, str], float] = {}

    def replace_config(self, config: AppConfig) -> None:
        """Apply a new config atomically.

        Preserves bucket state for entries whose `(upstream, scope, key)`
        still maps to an identical rate-limit configuration.
        """
        self._policies = config.policies
        if not self._policies.per_upstream and self._policies.global_ is None:
            self._buckets.clear()
            self._last_access.clear()

    # ---------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------

    def policies(self) -> PoliciesConfig:
        return self._policies

    def check(
        self,
        *,
        upstream: str,
        message: dict[str, Any],
        request_bytes: int = 0,
        client_ip: str | None = None,
    ) -> PolicyDecision:
        """Evaluate all policies. First-match deny wins."""
        method = message.get("method") if isinstance(message, dict) else None
        resolved = self._resolve_for(upstream)

        # 1. Size check (cheapest, apply first).
        if resolved.size and request_bytes > resolved.size.max_request_bytes:
            return PolicyDecision.deny("size_exceeded")

        # 2. Method ACL.
        if resolved.methods and not self._method_allowed(resolved.methods, method):
            return PolicyDecision.deny("method_denied")

        # 3. Rate limit.
        if resolved.rate_limit:
            decision = self._rate_check(upstream, resolved.rate_limit, client_ip)
            if not decision.allowed:
                return decision

        return PolicyDecision.allow()

    def buckets_snapshot(self) -> dict[str, Any]:
        return {
            f"{upstream}:{scope}:{key}": bucket.snapshot()
            for (upstream, scope, key), bucket in self._buckets.items()
        }

    def evict_idle_buckets(self, idle_seconds: float = 600.0) -> int:
        """Remove per-client buckets that have been idle longer than the threshold."""
        now = time.monotonic()
        removed = 0
        for key, last in list(self._last_access.items()):
            scope = key[1]
            if scope != "client_ip":
                continue
            if now - last > idle_seconds:
                self._buckets.pop(key, None)
                self._last_access.pop(key, None)
                removed += 1
        return removed

    # ---------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------

    def _resolve_for(self, upstream: str) -> UpstreamPolicies:
        """Merge global + per-upstream policies (per-upstream wins)."""
        global_ = self._policies.global_ or UpstreamPolicies()
        per = self._policies.per_upstream.get(upstream) or UpstreamPolicies()
        return UpstreamPolicies(
            methods=per.methods or global_.methods,
            rate_limit=per.rate_limit or global_.rate_limit,
            size=per.size or global_.size,
        )

    @staticmethod
    def _method_allowed(policy: MethodPolicy, method: str | None) -> bool:
        m = method or ""
        if policy.deny:
            for pattern in policy.deny:
                if fnmatch.fnmatch(m, pattern):
                    return False
        if policy.allow:
            for pattern in policy.allow:
                if fnmatch.fnmatch(m, pattern):
                    return True
            return False
        return True

    def _rate_check(
        self,
        upstream: str,
        limit: RateLimitPolicy,
        client_ip: str | None,
    ) -> PolicyDecision:
        now = time.monotonic()
        scopes: list[tuple[str, str]] = []
        if limit.scope in ("upstream", "both"):
            scopes.append(("upstream", "-"))
        if limit.scope in ("client_ip", "both"):
            scopes.append(("client_ip", client_ip or "unknown"))

        for scope, key in scopes:
            bucket_key = (upstream, scope, key)
            bucket = self._buckets.get(bucket_key)
            if bucket is None or bucket.rate != limit.requests_per_second or bucket.capacity != limit.burst:
                bucket = TokenBucket(limit.requests_per_second, limit.burst)
                self._buckets[bucket_key] = bucket
            self._last_access[bucket_key] = now
            if not bucket.take(now):
                return PolicyDecision.deny(f"rate_limited:{scope}")
        return PolicyDecision.allow()
