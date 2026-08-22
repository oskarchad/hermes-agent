"""Regression tests for explicit headless MCP OAuth login attempts."""

import socket
import threading
import time
import urllib.request
from unittest.mock import MagicMock

import pytest


def _send_callback_when_ready(url: str) -> None:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1).close()
            return
        except OSError:
            time.sleep(0.01)
    raise AssertionError(f"callback listener never became ready: {url}")


def _assert_port_released(port: int) -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("127.0.0.1", port))
    finally:
        probe.close()


def _callback_pair(result) -> tuple[str, str | None]:
    if hasattr(result, "code"):
        return result.code, result.state
    return result


@pytest.mark.asyncio
async def test_forced_headless_oauth_eof_does_not_retry_authorization(monkeypatch):
    """One explicit login must not turn paste-channel EOF into OAuth retries."""
    import tools.mcp_oauth as oauth
    import tools.mcp_tool as mcp_tool

    stdin = MagicMock()
    stdin.isatty.return_value = False
    stdin.readline.return_value = ""
    monkeypatch.setattr(oauth.sys, "stdin", stdin)
    monkeypatch.setattr(mcp_tool, "_ensure_mcp_sdk", lambda: None)

    authorization_attempts: list[int] = []
    port = oauth._find_free_port()
    server = mcp_tool.MCPServerTask("headless-oauth")

    async def fail_after_eof(_self, _config):
        authorization_attempts.append(len(authorization_attempts) + 1)
        waiter = oauth._make_callback_waiter(port, timeout=0.0)
        await waiter()

    async def stop_after_retry_budget(_self, *, timeout):
        return "shutdown"

    monkeypatch.setattr(mcp_tool.MCPServerTask, "_run_http", fail_after_eof)
    monkeypatch.setattr(
        mcp_tool.MCPServerTask,
        "_wait_for_reconnect_or_shutdown",
        stop_after_retry_budget,
    )
    monkeypatch.setattr(mcp_tool, "_jittered", lambda _seconds: 0.0)

    with oauth.force_interactive_oauth():
        await server.run(
            {
                "url": "https://mcp.example.test/mcp",
                "auth": "oauth",
                "skip_preflight": True,
            }
        )

    assert authorization_attempts == [1]
    assert isinstance(server._error, oauth.OAuthNonInteractiveError)
    assert server._ready.is_set()
    stdin.readline.assert_not_called()
    _assert_port_released(port)


@pytest.mark.asyncio
async def test_forced_headless_loopback_callback_completes_without_paste(monkeypatch):
    """A non-TTY explicit login still accepts its original loopback callback."""
    import tools.mcp_oauth as oauth

    stdin = MagicMock()
    stdin.isatty.return_value = False
    stdin.readline.side_effect = AssertionError("non-TTY stdin must not be read")
    monkeypatch.setattr(oauth.sys, "stdin", stdin)
    port = oauth._find_free_port()
    waiter = oauth._make_callback_waiter(port, timeout=5.0)
    callback = (
        f"http://127.0.0.1:{port}/callback"
        "?code=fake-loopback-code&state=fake-loopback-state"
    )

    with oauth.force_interactive_oauth():
        sender = threading.Thread(
            target=_send_callback_when_ready,
            args=(callback,),
            daemon=True,
        )
        sender.start()
        result = await waiter()
        sender.join(timeout=5)

    assert _callback_pair(result) == ("fake-loopback-code", "fake-loopback-state")
    stdin.readline.assert_not_called()
    _assert_port_released(port)


@pytest.mark.asyncio
async def test_tty_paste_callback_completes_and_releases_listener(monkeypatch):
    """Usable paste-back remains supported and closes its loopback listener."""
    import tools.mcp_oauth as oauth

    stdin = MagicMock()
    stdin.isatty.return_value = True
    stdin.readline.return_value = (
        "https://mcp.example.test/callback"
        "?code=fake-paste-code&state=fake-paste-state\n"
    )
    monkeypatch.setattr(oauth.sys, "stdin", stdin)
    port = oauth._find_free_port()

    result = await oauth._make_callback_waiter(port, timeout=5.0)()

    assert _callback_pair(result) == ("fake-paste-code", "fake-paste-state")
    _assert_port_released(port)


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["skip", "cancel"])
async def test_tty_skip_or_cancel_releases_listener(monkeypatch, decision):
    """Explicit skip stays non-fatal and deterministically closes the port."""
    import tools.mcp_oauth as oauth

    stdin = MagicMock()
    stdin.isatty.return_value = True
    stdin.readline.return_value = decision + "\n"
    monkeypatch.setattr(oauth.sys, "stdin", stdin)
    port = oauth._find_free_port()

    with pytest.raises(oauth.OAuthNonInteractiveError, match="user_skipped"):
        await oauth._make_callback_waiter(port, timeout=5.0)()

    _assert_port_released(port)
