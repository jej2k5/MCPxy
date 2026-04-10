"""PII / PCI redaction engine for JSON-RPC payloads.

Walks every string value in a nested dict/list structure and replaces
matches of known sensitive-data patterns with a configurable marker.

The engine is stateless and thread-safe: call :func:`build_redactor`
once from the policy engine (or on config hot-reload) and then invoke
the returned callable on each message dict. The callable mutates the
dict **in place** for zero-copy efficiency on the request hot path.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from mcp_proxy.config import RedactionPolicy

# -----------------------------------------------------------------------
# Built-in pattern libraries
# -----------------------------------------------------------------------
#
# Each pattern is a compiled regex. The engine tries every enabled
# pattern against every string value it encounters. Patterns are
# intentionally conservative (prefer false-negatives over false-positives
# on short fragments) but aggressive enough to catch the common shapes
# that appear in MCP tool outputs (web scraping results, database query
# responses, LLM-generated text, etc.).

# -- PII patterns -------------------------------------------------------

_EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
)

# US / international phone: +1-555-123-4567, (555) 123-4567, 555.123.4567
_PHONE_RE = re.compile(
    r"(?<!\d)"                       # not preceded by digit
    r"(?:\+?\d{1,3}[\s\-.]?)?"      # optional country code
    r"(?:\(?\d{3}\)?[\s\-.]?)"      # area code
    r"\d{3}[\s\-.]?"                 # exchange
    r"\d{4}"                         # subscriber
    r"(?!\d)",                       # not followed by digit
)

# US Social Security Number: 123-45-6789 or 123 45 6789
_SSN_RE = re.compile(
    r"(?<!\d)\d{3}[\s\-]\d{2}[\s\-]\d{4}(?!\d)",
)

# IPv4 address (basic — avoids matching version numbers like 2.0.1)
_IPV4_RE = re.compile(
    r"(?<!\d)(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
    r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?!\d)",
)

_PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("email", _EMAIL_RE),
    ("phone", _PHONE_RE),
    ("ssn", _SSN_RE),
    ("ipv4", _IPV4_RE),
]

# -- PCI patterns -------------------------------------------------------

# Credit card primary account numbers (PAN).
# Visa (4xxx), Mastercard (5[1-5]xx / 2[2-7]xx), Amex (3[47]xx),
# Discover (6011/65/644-649).
# Accepts optional separators (space, dash) every 4 digits.
_CARD_PAN_RE = re.compile(
    r"(?<!\d)"
    r"(?:"
    r"4\d{3}|5[1-5]\d{2}|2[2-7]\d{2}|3[47]\d{2}|6(?:011|5\d{2}|4[4-9]\d)"
    r")"
    r"(?:[\s\-]?\d{4,6}){2,3}"
    r"(?!\d)",
)

# CVV / CVC: 3 or 4 digits that appear to be a security code
# (very short, so only match when preceded by known keywords)
_CVV_RE = re.compile(
    r"(?i)(?:cvv|cvc|cvv2|cvc2|cid)[\s:=]*\d{3,4}",
)

# Expiry date: MM/YY or MM/YYYY near card context
_EXPIRY_RE = re.compile(
    r"(?i)(?:exp(?:ir[ey]s?)?|valid\s*(?:thru|through|until))[\s:=]*"
    r"(?:0[1-9]|1[0-2])\s*/\s*(?:\d{2}|\d{4})",
)

_PCI_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("card_pan", _CARD_PAN_RE),
    ("cvv", _CVV_RE),
    ("card_expiry", _EXPIRY_RE),
]


# -----------------------------------------------------------------------
# Redactor builder
# -----------------------------------------------------------------------


def build_redactor(
    policy: RedactionPolicy,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Compile a redaction function from a :class:`RedactionPolicy`.

    The returned callable walks a dict in-place, replacing every
    matching substring in every string value with ``policy.replacement``.
    Returns the same dict (mutated) for chaining convenience.
    """
    patterns: list[tuple[str, re.Pattern[str]]] = []
    if policy.pii:
        patterns.extend(_PII_PATTERNS)
    if policy.pci:
        patterns.extend(_PCI_PATTERNS)
    for label, raw in policy.custom_patterns.items():
        patterns.append((label, re.compile(raw)))

    if not patterns:
        # Nothing to redact — return a no-op.
        return lambda msg: msg

    replacement = policy.replacement

    def _redact_value(value: str) -> str:
        for _label, pat in patterns:
            value = pat.sub(replacement, value)
        return value

    def _walk(obj: Any) -> Any:
        if isinstance(obj, str):
            return _redact_value(obj)
        if isinstance(obj, dict):
            for key in obj:
                obj[key] = _walk(obj[key])
            return obj
        if isinstance(obj, list):
            for i, item in enumerate(obj):
                obj[i] = _walk(item)
            return obj
        return obj

    def redact(message: dict[str, Any]) -> dict[str, Any]:
        _walk(message)
        return message

    return redact
