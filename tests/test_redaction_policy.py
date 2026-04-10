"""Tests for the PII / PCI redaction policy.

Covers: config validation, the regex-based redaction engine, the
policy engine integration, and the bridge request/response redaction.
"""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from mcpxy_proxy.config import (
    AppConfig,
    PoliciesConfig,
    RedactionPolicy,
    UpstreamPolicies,
)
from mcpxy_proxy.policy.engine import PolicyEngine
from mcpxy_proxy.policy.redaction import build_redactor


# ---------------------------------------------------------------------------
# Config model tests
# ---------------------------------------------------------------------------


def test_redaction_defaults():
    r = RedactionPolicy()
    assert r.pii is True
    assert r.pci is True
    assert r.redact_request is True
    assert r.redact_response is True
    assert r.replacement == "[REDACTED]"


def test_redaction_custom_pattern_valid():
    r = RedactionPolicy(custom_patterns={"badge_id": r"BADGE-\d{6}"})
    assert "badge_id" in r.custom_patterns


def test_redaction_custom_pattern_invalid():
    with pytest.raises(ValidationError, match="invalid regex"):
        RedactionPolicy(custom_patterns={"bad": r"[unclosed"})


def test_redaction_in_upstream_policies():
    p = UpstreamPolicies(redaction=RedactionPolicy(pii=True, pci=False))
    assert p.redaction is not None
    assert p.redaction.pci is False


# ---------------------------------------------------------------------------
# Redaction engine: PII patterns
# ---------------------------------------------------------------------------


def _redact(msg, **kwargs):
    policy = RedactionPolicy(**kwargs)
    fn = build_redactor(policy)
    return fn(msg)


def test_redact_email():
    msg = {"params": {"text": "Contact alice@example.com for details"}}
    _redact(msg, pii=True, pci=False)
    assert "alice@example.com" not in msg["params"]["text"]
    assert "[REDACTED]" in msg["params"]["text"]


def test_redact_phone():
    msg = {"params": {"note": "Call me at 555-123-4567 or (555) 987-6543"}}
    _redact(msg, pii=True, pci=False)
    assert "555-123-4567" not in msg["params"]["note"]
    assert "(555) 987-6543" not in msg["params"]["note"]


def test_redact_ssn():
    msg = {"params": {"ssn": "My SSN is 123-45-6789"}}
    _redact(msg, pii=True, pci=False)
    assert "123-45-6789" not in msg["params"]["ssn"]


def test_redact_ipv4():
    msg = {"params": {"ip": "Client connected from 192.168.1.100"}}
    _redact(msg, pii=True, pci=False)
    assert "192.168.1.100" not in msg["params"]["ip"]


# ---------------------------------------------------------------------------
# Redaction engine: PCI patterns
# ---------------------------------------------------------------------------


def test_redact_visa_card():
    msg = {"params": {"card": "4111 1111 1111 1111"}}
    _redact(msg, pii=False, pci=True)
    assert "4111" not in msg["params"]["card"]
    assert "[REDACTED]" in msg["params"]["card"]


def test_redact_mastercard():
    msg = {"params": {"card": "5500-0000-0000-0004"}}
    _redact(msg, pii=False, pci=True)
    assert "5500" not in msg["params"]["card"]


def test_redact_amex():
    msg = {"params": {"card": "3782 822463 10005"}}
    _redact(msg, pii=False, pci=True)
    assert "3782" not in msg["params"]["card"]


def test_redact_cvv():
    msg = {"params": {"security": "CVV: 123"}}
    _redact(msg, pii=False, pci=True)
    assert "123" not in msg["params"]["security"]


def test_redact_expiry():
    msg = {"params": {"exp": "Expires 12/2025"}}
    _redact(msg, pii=False, pci=True)
    assert "12/2025" not in msg["params"]["exp"]


# ---------------------------------------------------------------------------
# Redaction engine: custom patterns
# ---------------------------------------------------------------------------


def test_redact_custom_pattern():
    msg = {"params": {"badge": "Employee BADGE-123456 entered"}}
    _redact(msg, pii=False, pci=False, custom_patterns={"badge": r"BADGE-\d{6}"})
    assert "BADGE-123456" not in msg["params"]["badge"]
    assert "[REDACTED]" in msg["params"]["badge"]


def test_redact_custom_replacement():
    msg = {"params": {"data": "alice@example.com"}}
    _redact(msg, pii=True, pci=False, replacement="***")
    assert msg["params"]["data"] == "***"


# ---------------------------------------------------------------------------
# Redaction engine: nested structures
# ---------------------------------------------------------------------------


def test_redact_deeply_nested():
    msg = {
        "params": {
            "a": {
                "b": [
                    {"c": "Email me at bob@corp.io"},
                    "Direct: 4111111111111111",
                ]
            }
        }
    }
    _redact(msg)
    assert "bob@corp.io" not in str(msg)
    assert "4111111111111111" not in str(msg)


def test_redact_list_of_strings():
    msg = {"params": {"items": ["alice@x.com", "safe-text", "4111111111111111"]}}
    _redact(msg)
    assert msg["params"]["items"][0] == "[REDACTED]"
    assert msg["params"]["items"][1] == "safe-text"


def test_redact_preserves_non_strings():
    msg = {"params": {"count": 42, "active": True, "items": None}}
    original = copy.deepcopy(msg)
    _redact(msg)
    assert msg == original


# ---------------------------------------------------------------------------
# Redaction engine: no-op when disabled
# ---------------------------------------------------------------------------


def test_no_redaction_when_both_disabled():
    msg = {"params": {"email": "alice@x.com", "card": "4111111111111111"}}
    original = copy.deepcopy(msg)
    _redact(msg, pii=False, pci=False)
    assert msg == original


# ---------------------------------------------------------------------------
# Policy engine integration
# ---------------------------------------------------------------------------


def _config(policies):
    return AppConfig(policies=policies)


def test_policy_engine_redacts_request():
    engine = PolicyEngine(
        _config(
            PoliciesConfig(
                global_=UpstreamPolicies(
                    redaction=RedactionPolicy(pii=True, pci=True)
                )
            )
        )
    )
    msg = {"method": "tools/call", "params": {"text": "Email: alice@x.com"}}
    engine.redact_request("any_upstream", msg)
    assert "alice@x.com" not in msg["params"]["text"]


def test_policy_engine_redacts_response():
    engine = PolicyEngine(
        _config(
            PoliciesConfig(
                global_=UpstreamPolicies(
                    redaction=RedactionPolicy(pii=True, pci=True)
                )
            )
        )
    )
    resp = {"result": {"output": "Card: 4111-1111-1111-1111"}}
    engine.redact_response("any_upstream", resp)
    assert "4111" not in resp["result"]["output"]


def test_policy_engine_skips_request_when_disabled():
    engine = PolicyEngine(
        _config(
            PoliciesConfig(
                global_=UpstreamPolicies(
                    redaction=RedactionPolicy(
                        pii=True, redact_request=False, redact_response=True
                    )
                )
            )
        )
    )
    msg = {"params": {"text": "alice@x.com"}}
    engine.redact_request("any", msg)
    assert msg["params"]["text"] == "alice@x.com"  # Not redacted


def test_policy_engine_per_upstream_overrides_global():
    engine = PolicyEngine(
        _config(
            PoliciesConfig(
                global_=UpstreamPolicies(
                    redaction=RedactionPolicy(pii=True, pci=True)
                ),
                per_upstream={
                    "trusted": UpstreamPolicies(
                        redaction=RedactionPolicy(pii=False, pci=False)
                    )
                },
            )
        )
    )
    msg = {"params": {"text": "alice@x.com"}}
    engine.redact_request("trusted", msg)
    assert msg["params"]["text"] == "alice@x.com"  # Per-upstream disabled

    msg2 = {"params": {"text": "bob@y.com"}}
    engine.redact_request("untrusted", msg2)
    assert "bob@y.com" not in msg2["params"]["text"]  # Global applied


def test_policy_engine_no_redaction_by_default():
    engine = PolicyEngine(_config(PoliciesConfig()))
    msg = {"params": {"text": "alice@x.com, 4111111111111111"}}
    original = copy.deepcopy(msg)
    engine.redact_request("any", msg)
    assert msg == original


def test_policy_engine_hot_reload_rebuilds_redactors():
    engine = PolicyEngine(_config(PoliciesConfig()))
    msg = {"params": {"text": "alice@x.com"}}
    engine.redact_request("up", msg)
    assert msg["params"]["text"] == "alice@x.com"

    # Hot-reload with redaction enabled
    engine.replace_config(
        _config(
            PoliciesConfig(
                global_=UpstreamPolicies(
                    redaction=RedactionPolicy(pii=True)
                )
            )
        )
    )
    msg2 = {"params": {"text": "bob@y.com"}}
    engine.redact_request("up", msg2)
    assert "bob@y.com" not in msg2["params"]["text"]
