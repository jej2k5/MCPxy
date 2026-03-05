import sys
from typing import Any

import pytest

from mcp_proxy import cli


def test_parse_listen_parses_host_port() -> None:
    assert cli.parse_listen("127.0.0.1:8000") == ("127.0.0.1", 8000)


@pytest.mark.parametrize("value", ["127.0.0.1", "127.0.0.1:abc", "127.0.0.1:70000"])
def test_parse_listen_rejects_invalid_values(value: str) -> None:
    with pytest.raises(SystemExit):
        parser = cli.argparse.ArgumentParser()
        parser.add_argument("--listen", type=cli.parse_listen)
        parser.parse_args(["--listen", value])


def test_main_serve_wires_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, Any] = {}

    def fake_build_state(config: str) -> str:
        called["config"] = config
        return "state"

    def fake_create_app(state: str, health_path: str, request_timeout_s: float) -> str:
        called["create_app"] = {
            "state": state,
            "health_path": health_path,
            "request_timeout_s": request_timeout_s,
        }
        return "app"

    def fake_uvicorn_run(app: str, **kwargs: Any) -> None:
        called["uvicorn"] = {"app": app, **kwargs}

    monkeypatch.setattr(cli, "build_state", fake_build_state)
    monkeypatch.setattr(cli, "create_app", fake_create_app)
    monkeypatch.setattr(cli.uvicorn, "run", fake_uvicorn_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mcp-proxy",
            "serve",
            "--config",
            "config.json",
            "--listen",
            "0.0.0.0:9000",
            "--log-level",
            "debug",
            "--health-path",
            "/readyz",
            "--request-timeout",
            "12.5",
            "--idle-timeout",
            "9",
            "--max-queue",
            "444",
            "--reload",
        ],
    )

    cli.main()

    assert called["config"] == "config.json"
    assert called["create_app"] == {"state": "state", "health_path": "/readyz", "request_timeout_s": 12.5}
    assert called["uvicorn"] == {
        "app": "app",
        "host": "0.0.0.0",
        "port": 9000,
        "log_level": "debug",
        "timeout_keep_alive": 9,
        "backlog": 444,
        "reload": True,
    }
