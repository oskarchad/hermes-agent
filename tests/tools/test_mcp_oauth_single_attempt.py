"""Regression tests for explicit headless MCP OAuth login attempts."""

import asyncio
import logging
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


def _connect_when_ready(port: int) -> socket.socket:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            client.connect(("127.0.0.1", port))
            return client
        except OSError:
            client.close()
            time.sleep(0.01)
    raise AssertionError(f"callback listener never became ready on port {port}")


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
async def test_loopback_callback_does_not_log_code_or_state(monkeypatch, caplog):
    """HTTP request logging must not expose OAuth callback credentials."""
    import tools.mcp_oauth as oauth

    stdin = MagicMock()
    stdin.isatty.return_value = False
    monkeypatch.setattr(oauth.sys, "stdin", stdin)
    port = oauth._find_free_port()
    code = "fake-secret-callback-code"
    state = "fake-secret-callback-state"
    callback = f"http://127.0.0.1:{port}/callback?code={code}&state={state}"

    with (
        oauth.force_interactive_oauth(),
        caplog.at_level(logging.DEBUG, logger="tools.mcp_oauth"),
    ):
        sender = threading.Thread(
            target=_send_callback_when_ready,
            args=(callback,),
            daemon=True,
        )
        sender.start()
        result = await oauth._make_callback_waiter(port, timeout=5.0)()
        sender.join(timeout=5)

    assert _callback_pair(result) == (code, state)
    assert code not in caplog.text
    assert state not in caplog.text
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


def test_stalled_callback_client_cannot_block_timeout_or_port_cleanup(monkeypatch):
    """An incomplete HTTP request must not hold the callback listener open."""
    import tools.mcp_oauth as oauth

    stdin = MagicMock()
    stdin.isatty.return_value = False
    monkeypatch.setattr(oauth.sys, "stdin", stdin)
    port = oauth._find_free_port()
    outcome: list[BaseException] = []

    def run_waiter() -> None:
        try:
            with oauth.force_interactive_oauth():
                asyncio.run(oauth._make_callback_waiter(port, timeout=0.1)())
        except BaseException as exc:
            outcome.append(exc)

    runner = threading.Thread(target=run_waiter, daemon=True)
    runner.start()
    client = _connect_when_ready(port)
    try:
        client.sendall(b"GET /callback?code=fake-partial-code")
        runner.join(timeout=2.0)
        completed_while_client_stalled = not runner.is_alive()
    finally:
        client.close()
        runner.join(timeout=2.0)

    assert completed_while_client_stalled, (
        "callback timeout blocked on a client that never completed its request"
    )
    assert len(outcome) == 1
    assert isinstance(outcome[0], oauth.OAuthNonInteractiveError)
    _assert_port_released(port)
