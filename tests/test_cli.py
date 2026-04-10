import sys
from types import SimpleNamespace
from typing import Any

import pytest

from mcpxy_proxy import cli
from mcpxy_proxy.config import AppConfig, TlsConfig


def _state_with_tls(tls: TlsConfig | None = None) -> SimpleNamespace:
    """Return a stub AppState whose `.config.tls` is the only attribute
    ``cmd_serve``'s TLS resolver touches. ``build_state`` can be
    monkey-patched to return this in place of the real runtime.
    """
    cfg = AppConfig.model_validate({"upstreams": {}})
    if tls is not None:
        cfg = cfg.model_copy(update={"tls": tls})
    return SimpleNamespace(config=cfg)


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
    state = _state_with_tls()

    def fake_build_state(config: str) -> SimpleNamespace:
        called["config"] = config
        return state

    def fake_create_app(state: Any, health_path: str, request_timeout_s: float) -> str:
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
            "mcpxy-proxy",
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
            # Auto-generated TLS is the default; pass --no-tls so this
            # baseline test stays focused on the non-TLS kwargs wiring.
            "--no-tls",
        ],
    )

    cli.main()

    assert called["config"] == "config.json"
    assert called["create_app"] == {"state": state, "health_path": "/readyz", "request_timeout_s": 12.5}
    assert called["uvicorn"] == {
        "app": "app",
        "host": "0.0.0.0",
        "port": 9000,
        "log_level": "debug",
        "timeout_keep_alive": 9,
        "backlog": 444,
        "reload": True,
        # Uvicorn's default Proxy-Headers middleware trusts
        # ``X-Forwarded-For`` from any 127.0.0.1 peer, which silently
        # lets clients spoof their IP for admin allowlist, onboarding
        # allowlist, and rate-limit attribution purposes. MCPxy must
        # opt out so ``request.client.host`` always reflects the real
        # TCP peer.
        "proxy_headers": False,
    }


# ---------------------------------------------------------------------------
# TLS (--ssl-*) tests
# ---------------------------------------------------------------------------


def _install_serve_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    state: SimpleNamespace,
    called: dict[str, Any],
) -> None:
    def fake_build_state(config: str) -> SimpleNamespace:
        called["config"] = config
        return state

    def fake_create_app(state: Any, health_path: str, request_timeout_s: float) -> str:
        return "app"

    def fake_uvicorn_run(app: str, **kwargs: Any) -> None:
        called["uvicorn"] = {"app": app, **kwargs}

    monkeypatch.setattr(cli, "build_state", fake_build_state)
    monkeypatch.setattr(cli, "create_app", fake_create_app)
    monkeypatch.setattr(cli.uvicorn, "run", fake_uvicorn_run)


def test_main_serve_tls_flags_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, Any] = {}
    _install_serve_stubs(monkeypatch, state=_state_with_tls(), called=called)
    # Short-circuit the fail-fast existence check so the test doesn't need
    # real files on disk. Uvicorn is stubbed out so it would never read them.
    monkeypatch.setattr(cli.Path, "is_file", lambda self: True)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mcpxy-proxy",
            "serve",
            "--listen",
            "0.0.0.0:8443",
            "--ssl-certfile",
            "/tmp/cert.pem",
            "--ssl-keyfile",
            "/tmp/key.pem",
            "--ssl-keyfile-password",
            "hunter2",
        ],
    )

    assert cli.main() == 0
    kwargs = called["uvicorn"]
    assert kwargs["ssl_certfile"] == "/tmp/cert.pem"
    assert kwargs["ssl_keyfile"] == "/tmp/key.pem"
    assert kwargs["ssl_keyfile_password"] == "hunter2"


def test_main_serve_tls_requires_both_cert_and_key(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    called: dict[str, Any] = {}
    _install_serve_stubs(monkeypatch, state=_state_with_tls(), called=called)
    monkeypatch.setattr(cli.Path, "is_file", lambda self: True)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mcpxy-proxy",
            "serve",
            "--ssl-keyfile",
            "/tmp/key.pem",
        ],
    )

    rc = cli.main()
    assert rc == 2
    assert "uvicorn" not in called
    assert "ssl-certfile" in capsys.readouterr().err


def test_main_serve_tls_missing_cert_file_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    called: dict[str, Any] = {}
    _install_serve_stubs(monkeypatch, state=_state_with_tls(), called=called)
    monkeypatch.setattr(cli.Path, "is_file", lambda self: False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mcpxy-proxy",
            "serve",
            "--ssl-certfile",
            "/no/such/cert.pem",
            "--ssl-keyfile",
            "/no/such/key.pem",
        ],
    )

    rc = cli.main()
    assert rc == 2
    assert "uvicorn" not in called
    err = capsys.readouterr().err
    assert "tls:" in err
    assert "/no/such/cert.pem" in err


def test_main_serve_cli_tls_overrides_config_tls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: dict[str, Any] = {}
    state = _state_with_tls(
        TlsConfig(
            enabled=True,
            certfile="/from/config/cert.pem",
            keyfile="/from/config/key.pem",
        )
    )
    _install_serve_stubs(monkeypatch, state=state, called=called)
    monkeypatch.setattr(cli.Path, "is_file", lambda self: True)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mcpxy-proxy",
            "serve",
            "--ssl-certfile",
            "/from/cli/cert.pem",
        ],
    )

    assert cli.main() == 0
    kwargs = called["uvicorn"]
    # CLI certfile wins; keyfile still comes from the config base.
    assert kwargs["ssl_certfile"] == "/from/cli/cert.pem"
    assert kwargs["ssl_keyfile"] == "/from/config/key.pem"


def test_main_serve_config_tls_used_when_no_cli_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: dict[str, Any] = {}
    state = _state_with_tls(
        TlsConfig(
            enabled=True,
            certfile="/from/config/cert.pem",
            keyfile="/from/config/key.pem",
            keyfile_password="pw-from-config",
        )
    )
    _install_serve_stubs(monkeypatch, state=state, called=called)
    monkeypatch.setattr(cli.Path, "is_file", lambda self: True)
    monkeypatch.setattr(sys, "argv", ["mcpxy-proxy", "serve"])

    assert cli.main() == 0
    kwargs = called["uvicorn"]
    assert kwargs["ssl_certfile"] == "/from/config/cert.pem"
    assert kwargs["ssl_keyfile"] == "/from/config/key.pem"
    assert kwargs["ssl_keyfile_password"] == "pw-from-config"


def test_main_serve_no_tls_flag_disables_https(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--no-tls`` short-circuits the default auto-gen path and serves
    plain HTTP, matching the pre-default-TLS behavior for operators who
    terminate TLS upstream (reverse proxy, service mesh, etc.).
    """
    called: dict[str, Any] = {}
    _install_serve_stubs(monkeypatch, state=_state_with_tls(), called=called)

    # The auto-gen helper must never be called when --no-tls is set.
    def _should_not_run(state_dir: Any) -> tuple[str, str]:
        raise AssertionError("ensure_dev_cert should not run under --no-tls")

    monkeypatch.setattr("mcpxy_proxy.tls.ensure_dev_cert", _should_not_run)
    monkeypatch.setattr(sys, "argv", ["mcpxy-proxy", "serve", "--no-tls"])

    assert cli.main() == 0
    kwargs = called["uvicorn"]
    assert "ssl_certfile" not in kwargs
    assert "ssl_keyfile" not in kwargs
    assert "ssl_keyfile_password" not in kwargs


def test_main_serve_auto_generates_tls_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no TLS flags and no config tls block, MCPxy auto-generates a
    self-signed cert and serves HTTPS. This is the default first-run
    experience.
    """
    called: dict[str, Any] = {}
    _install_serve_stubs(monkeypatch, state=_state_with_tls(), called=called)

    ensure_calls: list[Any] = []

    def fake_ensure_dev_cert(state_dir: Any) -> tuple[str, str]:
        ensure_calls.append(state_dir)
        return "/auto/cert.pem", "/auto/key.pem"

    monkeypatch.setattr("mcpxy_proxy.tls.ensure_dev_cert", fake_ensure_dev_cert)
    monkeypatch.setattr(sys, "argv", ["mcpxy-proxy", "serve"])

    assert cli.main() == 0
    assert len(ensure_calls) == 1
    kwargs = called["uvicorn"]
    assert kwargs["ssl_certfile"] == "/auto/cert.pem"
    assert kwargs["ssl_keyfile"] == "/auto/key.pem"
    assert "ssl_keyfile_password" not in kwargs


def test_main_serve_explicit_ssl_flags_skip_auto_gen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit ``--ssl-certfile`` / ``--ssl-keyfile`` must skip the
    auto-gen path entirely — operators providing real certs shouldn't
    end up with a stray self-signed cert cached on disk.
    """
    called: dict[str, Any] = {}
    _install_serve_stubs(monkeypatch, state=_state_with_tls(), called=called)
    monkeypatch.setattr(cli.Path, "is_file", lambda self: True)

    def _should_not_run(state_dir: Any) -> tuple[str, str]:
        raise AssertionError("ensure_dev_cert should not run when --ssl-* flags are set")

    monkeypatch.setattr("mcpxy_proxy.tls.ensure_dev_cert", _should_not_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mcpxy-proxy",
            "serve",
            "--ssl-certfile",
            "/real/cert.pem",
            "--ssl-keyfile",
            "/real/key.pem",
        ],
    )

    assert cli.main() == 0
    kwargs = called["uvicorn"]
    assert kwargs["ssl_certfile"] == "/real/cert.pem"
    assert kwargs["ssl_keyfile"] == "/real/key.pem"


def test_main_serve_config_tls_skips_auto_gen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``tls`` block in AppConfig is operator intent; the auto-gen path
    must not run even when ``enabled=False`` (because staged certfile /
    keyfile values signal the operator is managing TLS themselves).
    """
    called: dict[str, Any] = {}
    state = _state_with_tls(
        TlsConfig(
            enabled=True,
            certfile="/from/config/cert.pem",
            keyfile="/from/config/key.pem",
        )
    )
    _install_serve_stubs(monkeypatch, state=state, called=called)
    monkeypatch.setattr(cli.Path, "is_file", lambda self: True)

    def _should_not_run(state_dir: Any) -> tuple[str, str]:
        raise AssertionError("ensure_dev_cert should not run when tls is configured")

    monkeypatch.setattr("mcpxy_proxy.tls.ensure_dev_cert", _should_not_run)
    monkeypatch.setattr(sys, "argv", ["mcpxy-proxy", "serve"])

    assert cli.main() == 0
    kwargs = called["uvicorn"]
    assert kwargs["ssl_certfile"] == "/from/config/cert.pem"
