from mcp_proxy.config import redact_secrets


def test_admin_redaction_masks_secret_fields() -> None:
    payload = {
        "auth": {"token_env": "MCP_PROXY_TOKEN"},
        "telemetry": {"headers": {"X-Api-Key": "abc", "Authorization": "Bearer x", "X-Trace": "ok"}},
    }

    redacted = redact_secrets(payload)

    assert redacted["auth"]["token_env"] == "***REDACTED_ENV***"
    assert redacted["telemetry"]["headers"]["X-Api-Key"] == "***REDACTED***"
    assert redacted["telemetry"]["headers"]["Authorization"] == "***REDACTED***"
    assert redacted["telemetry"]["headers"]["X-Trace"] == "ok"
