"""Tests for the new discover / catalog / register CLI subcommands."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from mcpxy_proxy.cli import main


def test_catalog_list_plain_output(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["catalog", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "filesystem" in out
    assert "github" in out


def test_catalog_list_search(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["catalog", "list", "-q", "sqlite"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "sqlite" in out
    assert "filesystem" not in out


def test_catalog_list_json(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["catalog", "list", "--json", "-q", "git"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    ids = {e["id"] for e in payload["entries"]}
    assert "git" in ids
    assert "github" in ids


def test_catalog_install_dry_run_prints_materialised_config(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(
        [
            "catalog",
            "install",
            "filesystem",
            "--var",
            "allowed_path=/tmp/fs",
            "--name",
            "fs1",
            "--dry-run",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["name"] == "fs1"
    assert out["config"]["type"] == "stdio"
    assert "/tmp/fs" in out["config"]["args"]


def test_catalog_install_rejects_missing_required_variables(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["catalog", "install", "github", "--dry-run"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "missing required variable" in err


def test_discover_local_json(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    # Point every importer at a missing path.
    missing = [tmp_path / "nope.json"]
    with patch(
        "mcpxy_proxy.discovery.importers.ClaudeDesktopImporter.candidate_paths",
        return_value=missing,
    ), patch(
        "mcpxy_proxy.discovery.importers.ClaudeCodeImporter.candidate_paths",
        return_value=missing,
    ), patch(
        "mcpxy_proxy.discovery.importers.CursorImporter.candidate_paths",
        return_value=missing,
    ), patch(
        "mcpxy_proxy.discovery.importers.WindsurfImporter.candidate_paths",
        return_value=missing,
    ), patch(
        "mcpxy_proxy.discovery.importers.ContinueImporter.candidate_paths",
        return_value=missing,
    ):
        rc = main(["discover", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["clients"]) == 5


def test_import_dry_run_prints_selected_entries(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "one": {"command": "python", "args": ["-m", "foo"]},
                    "two": {"url": "http://bar"},
                }
            }
        ),
        encoding="utf-8",
    )
    with patch(
        "mcpxy_proxy.discovery.importers.ClaudeDesktopImporter.candidate_paths",
        return_value=[cfg],
    ):
        rc = main(["import", "--client", "claude-desktop", "--dry-run"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    names = {u["name"] for u in payload["imported"]}
    assert names == {"one", "two"}


def test_import_filters_by_name(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "only-this": {"command": "true"},
                    "not-this": {"command": "false"},
                }
            }
        ),
        encoding="utf-8",
    )
    with patch(
        "mcpxy_proxy.discovery.importers.ClaudeDesktopImporter.candidate_paths",
        return_value=[cfg],
    ):
        rc = main(
            [
                "import",
                "--client",
                "claude-desktop",
                "--name",
                "only-this",
                "--dry-run",
            ]
        )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    names = {u["name"] for u in payload["imported"]}
    assert names == {"only-this"}


def test_register_requires_a_transport_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["register", "--name", "x"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "stdio" in err


def test_register_and_unregister_calls_remote_endpoint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[str, str, dict | None]] = []

    def fake_remote(method, url, token_env, body=None):
        calls.append((method, url, body))
        return {"applied": True}

    with patch("mcpxy_proxy.cli._remote_call", side_effect=fake_remote):
        rc = main(
            [
                "register",
                "--name",
                "demo",
                "--http",
                "https://example.com/mcp",
                "--url",
                "http://proxy:8000",
            ]
        )
        assert rc == 0
        assert calls[-1][0] == "POST"
        assert calls[-1][1] == "http://proxy:8000/admin/api/upstreams"
        assert calls[-1][2] == {
            "name": "demo",
            "config": {"type": "http", "url": "https://example.com/mcp"},
            "replace": False,
        }

        rc = main(["unregister", "--name", "demo", "--url", "http://proxy:8000"])
        assert rc == 0
        assert calls[-1][0] == "DELETE"
        assert calls[-1][1] == "http://proxy:8000/admin/api/upstreams/demo"
