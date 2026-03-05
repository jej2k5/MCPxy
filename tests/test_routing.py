from mcp_proxy.config import AppConfig
from mcp_proxy.routing import resolve_upstream


def test_routing_precedence() -> None:
    config = AppConfig.model_validate(
        {
            "default_upstream": "default",
            "upstreams": {
                "path": {"type": "http", "url": "http://x"},
                "header": {"type": "http", "url": "http://x"},
                "inband": {"type": "http", "url": "http://x"},
                "default": {"type": "http", "url": "http://x"},
            },
        }
    )
    message = {"jsonrpc": "2.0", "id": 1, "params": {"mcp_upstream": "inband"}}
    upstream, cleaned = resolve_upstream(message, config, "path", "header")
    assert upstream == "path"
    assert "mcp_upstream" not in cleaned["params"]

    upstream, _ = resolve_upstream(message, config, None, "header")
    assert upstream == "header"

    upstream, _ = resolve_upstream(message, config, None, None)
    assert upstream == "inband"

    upstream, _ = resolve_upstream({"jsonrpc": "2.0", "id": 2}, config, None, None)
    assert upstream == "default"
