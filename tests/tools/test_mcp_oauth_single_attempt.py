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


def _send_callback_then_secondary_get(callback_url: str, secondary_url: str) -> None:
    _send_callback_when_ready(callback_url)
    urllib.request.urlopen(secondary_url, timeout=1).close()


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
async def test_loopback_callback_result_survives_secondary_get(monkeypatch):
    """A browser follow-up request must not erase the terminal callback result."""
    import tools.mcp_oauth as oauth

    stdin = MagicMock()
    stdin.isatty.return_value = False
    monkeypatch.setattr(oauth.sys, "stdin", stdin)
    port = oauth._find_free_port()
    callback = (
        f"http://127.0.0.1:{port}/callback"
        "?code=fake-latched-code&state=fake-latched-state"
    )
    secondary = f"http://127.0.0.1:{port}/favicon.ico"

    with oauth.force_interactive_oauth():
        sender = threading.Thread(
            target=_send_callback_then_secondary_get,
            args=(callback, secondary),
            daemon=True,
        )
        sender.start()
        result = await oauth._make_callback_waiter(port, timeout=1.0)()
        sender.join(timeout=5)

    assert not sender.is_alive()
    assert _callback_pair(result) == ("fake-latched-code", "fake-latched-state")
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
async def test_state_mismatch_is_redacted_before_sdk_and_application_logs(
    monkeypatch, caplog
):
    """A mismatched callback must not expose either OAuth state in any logger."""
    from mcp.client.auth import OAuthFlowError

    import tools.mcp_oauth as oauth
    import tools.mcp_oauth_manager as oauth_manager

    provider_classes = [
        provider_class
        for provider_class in (
            oauth._get_hermes_oauth_provider_class(),
            oauth_manager._HERMES_PROVIDER_CLS,
        )
        if provider_class is not None
    ]
    assert len(provider_classes) == 2
    upstream_provider = provider_classes[0].__bases__[0]
    received_state = "fake-received-secret-state"
    expected_state = "fake-expected-secret-state"

    async def raise_state_mismatch(_self):
        raise OAuthFlowError(
            f"State parameter mismatch: {received_state} != {expected_state}"
        )

    monkeypatch.setattr(
        upstream_provider,
        "_perform_authorization",
        raise_state_mismatch,
    )
    with caplog.at_level(logging.DEBUG):
        caught_errors = []
        for provider_class in provider_classes:
            provider = object.__new__(provider_class)
            with pytest.raises(OAuthFlowError) as caught:
                await provider._perform_authorization()
            caught_errors.append(caught.value)

            try:
                raise caught.value
            except OAuthFlowError:
                logging.getLogger("mcp.client.auth.oauth2").exception("OAuth flow error")
            logging.getLogger("tools.mcp_tool").warning(
                "MCP initial authentication failed: %s", caught.value
            )

    assert [str(error) for error in caught_errors] == [
        "OAuth state parameter mismatch",
        "OAuth state parameter mismatch",
    ]
    assert received_state not in caplog.text
    assert expected_state not in caplog.text


@pytest.mark.linux_only
@pytest.mark.asyncio
async def test_tty_paste_callback_completes_and_releases_listener(monkeypatch):
    """Usable paste-back remains supported and closes its loopback listener."""
    import os
    import pty

    import tools.mcp_oauth as oauth

    master_fd, slave_fd = pty.openpty()
    stdin = os.fdopen(os.dup(slave_fd), "r", encoding="utf-8", buffering=1)
    monkeypatch.setattr(oauth.sys, "stdin", stdin)
    port = oauth._find_free_port()

    try:
        os.write(
            master_fd,
            b"https://mcp.example.test/callback"
            b"?code=fake-paste-code&state=fake-paste-state\n",
        )
        result = await oauth._make_callback_waiter(port, timeout=5.0)()
    finally:
        stdin.close()
        os.close(master_fd)
        os.close(slave_fd)

    assert _callback_pair(result) == ("fake-paste-code", "fake-paste-state")
    _assert_port_released(port)


@pytest.mark.linux_only
@pytest.mark.asyncio
async def test_loopback_callback_stops_tty_reader_before_next_flow_input(monkeypatch):
    """Loopback completion must leave later TTY input for the next flow."""
    import os
    import pty
    import select

    import tools.mcp_oauth as oauth

    master_fd, slave_fd = pty.openpty()
    stdin = os.fdopen(os.dup(slave_fd), "r", encoding="utf-8", buffering=1)
    monkeypatch.setattr(oauth.sys, "stdin", stdin)
    port = oauth._find_free_port()
    callback = (
        f"http://127.0.0.1:{port}/callback"
        "?code=fake-loopback-code&state=fake-loopback-state"
    )

    try:
        sender = threading.Thread(
            target=_send_callback_when_ready,
            args=(callback,),
            daemon=True,
        )
        sender.start()
        result = await oauth._make_callback_waiter(port, timeout=5.0)()
        sender.join(timeout=5)

        assert not sender.is_alive()
        assert _callback_pair(result) == (
            "fake-loopback-code",
            "fake-loopback-state",
        )

        os.write(master_fd, b"next-flow-input\n")
        await asyncio.sleep(0.1)
        readable, _, _ = select.select([stdin], [], [], 1.0)
        assert readable, "the completed OAuth flow consumed the next flow's TTY input"
        assert stdin.readline() == "next-flow-input\n"
    finally:
        stdin.close()
        os.close(master_fd)
        os.close(slave_fd)

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
    monkeypatch.setattr(
        oauth,
        "_read_paste_line",
        lambda _stop: stdin.readline(),
    )
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
