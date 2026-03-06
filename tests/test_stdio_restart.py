import asyncio

import pytest

from mcp_proxy.proxy.stdio import StdioUpstreamTransport


class _FakeStdout:
    async def readline(self):
        return b""


class _FakeProc:
    def __init__(self):
        self.stdout = _FakeStdout()
        self.stdin = None
        self.returncode = 0


@pytest.mark.asyncio
async def test_stdio_restart_after_process_exit(monkeypatch) -> None:
    transport = StdioUpstreamTransport("s", {"command": "python", "args": []})
    transport._running = True
    transport._proc = _FakeProc()

    restarted = asyncio.Event()

    async def fake_maybe_restart():
        transport._restart_attempts += 1
        restarted.set()

    monkeypatch.setattr(transport, "_maybe_restart", fake_maybe_restart)

    await transport._reader()

    assert restarted.is_set()
    assert transport.health()["restart_attempts"] == 1
