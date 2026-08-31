"""Authority and correlation tests for headless protected-write approvals."""

import asyncio
import json
import logging
import time

import pytest

from gateway import protected_approval_bridge as bridge_module
from gateway.control_socket import GatewayControlServer
from tools import approval


OperatorTarget = getattr(bridge_module, "OperatorTarget", None)
ProtectedApprovalBridge = getattr(bridge_module, "ProtectedApprovalBridge", None)


def test_bridge_exposes_gateway_owned_authority():
    assert OperatorTarget is not None
    assert ProtectedApprovalBridge is not None


@pytest.fixture(autouse=True)
def _clear_gateway_approval_state():
    with approval._lock:
        approval._gateway_queues.clear()
        approval._gateway_notify_cbs.clear()
    yield
    with approval._lock:
        entries = [entry for queue in approval._gateway_queues.values() for entry in queue]
        approval._gateway_queues.clear()
        approval._gateway_notify_cbs.clear()
    for entry in entries:
        entry.event.set()


def _request(request_id="req-1", fingerprint="a" * 64):
    return {
        "schema_version": 1,
        "request_id": request_id,
        "task_id": "t_worker",
        "board": "default",
        "run_id": 41,
        "claim_lock": "claim-secret",
        "operation": "patch",
        "paths": ["/repo/AGENTS.md"],
        "operation_fingerprint": fingerprint,
        "summary": "Write to protected agent-instruction file(s): AGENTS.md",
        "timeout_seconds": 2.0,
    }


def _wait_for(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("condition did not become true")


@pytest.mark.skipif(ProtectedApprovalBridge is None, reason="bridge not implemented")
def _bridge(notifications, validate_run=lambda _request: True):
    target = OperatorTarget(
        profile="otto",
        session_id="session-1",
        platform="webui",
        task_id="t_worker",
    )
    return ProtectedApprovalBridge(
        resolve_target=lambda request: (
            target if request["task_id"] == "t_worker" else None
        ),
        notify=lambda resolved, text: notifications.append((resolved, text)) or True,
        validate_run=validate_run,
    )


def test_exact_active_session_approval_is_consumed_once(caplog):
    caplog.set_level(logging.INFO, logger="gateway.protected_approval_bridge")
    notifications = []
    bridge = _bridge(notifications)
    request = _request()

    submitted = bridge.submit(request)
    assert submitted["ok"] is True
    _wait_for(lambda: notifications)
    assert notifications and "AGENTS.md" in notifications[0][1]
    assert request["operation_fingerprint"] not in notifications[0][1]
    assert request["summary"] in notifications[0][1]

    assert bridge.handle_operator_text(
        profile="otto", session_key="session-1", text="czy to bezpieczne?"
    ) is None
    assert bridge.handle_operator_text(
        profile="wrench", session_key="session-1", text="wrzucaj"
    ) is None
    assert bridge.handle_operator_text(
        profile="otto", session_key="other-session", text="wrzucaj"
    ) is None

    reply = bridge.handle_operator_text(
        profile="otto",
        session_key="agent:main:webui:dm:user-7",
        session_id="session-1",
        text="wrzucaj",
    )
    assert reply and "Approved once" in reply

    def poll_approved():
        result = bridge.poll(request)
        return result if result.get("status") != "pending" else None

    result = _wait_for(poll_approved)
    assert result == {
        "ok": True,
        "request_id": "req-1",
        "operation_fingerprint": "a" * 64,
        "status": "approved",
        "choice": "once",
        "consumed": True,
    }
    assert bridge.poll(request)["status"] == "stale"
    assert "protected write approval consumed" in caplog.text
    assert "session_ref=" in caplog.text
    assert "session-1" not in caplog.text


def test_worker_timeout_cancellation_is_audited_and_denied(caplog):
    caplog.set_level(logging.INFO, logger="gateway.protected_approval_bridge")
    notifications = []
    bridge = _bridge(notifications)
    request = _request()
    bridge.submit(request)
    _wait_for(lambda: notifications)

    assert bridge.cancel({**request, "reason": "worker_timeout"}) == {
        "ok": True,
        "status": "cancelled",
    }

    def poll_denied():
        result = bridge.poll(request)
        return result if result.get("status") != "pending" else None

    result = _wait_for(poll_denied)
    assert result["status"] == "denied"
    assert result["consumed"] is True
    assert "protected write approval cancelled" in caplog.text
    assert "reason=worker_timeout" in caplog.text
    assert "session-1" not in caplog.text


def test_denial_and_modified_fingerprint_fail_closed():
    notifications = []
    bridge = _bridge(notifications)
    request = _request()
    bridge.submit(request)
    _wait_for(lambda: notifications)

    changed = dict(request, operation_fingerprint="b" * 64)
    assert bridge.poll(changed)["status"] == "stale"
    reply = bridge.handle_operator_text(
        profile="otto", session_key="session-1", text="odrzuć"
    )
    assert reply and "Denied" in reply
    def poll_denied():
        result = bridge.poll(request)
        return result if result.get("status") != "pending" else None

    result = _wait_for(poll_denied)
    assert result["status"] == "denied"
    assert result["choice"] == "deny"


def test_unqualified_text_cannot_resolve_multiple_pending_requests():
    notifications = []
    bridge = _bridge(notifications)
    first = _request("req-1", "a" * 64)
    second = _request("req-2", "b" * 64)
    bridge.submit(first)
    bridge.submit(second)
    _wait_for(lambda: len(notifications) == 2)

    reply = bridge.handle_operator_text(
        profile="otto", session_key="session-1", text="wrzucaj"
    )

    assert reply and "More than one" in reply
    assert bridge.poll(first)["status"] == "pending"
    assert bridge.poll(second)["status"] == "pending"


def test_approved_decision_cannot_be_consumed_after_monotonic_deadline():
    assert ProtectedApprovalBridge is not None and OperatorTarget is not None
    notifications = []
    now = [100.0]
    target = OperatorTarget(
        profile="otto",
        session_id="session-1",
        platform="webui",
        task_id="t_worker",
    )
    bridge = ProtectedApprovalBridge(
        resolve_target=lambda request: (
            target if request["task_id"] == "t_worker" else None
        ),
        notify=lambda resolved, text: notifications.append((resolved, text)) or True,
        validate_run=lambda _request: True,
        monotonic=lambda: now[0],
    )
    request = {**_request(), "timeout_seconds": 5.0}

    assert bridge.submit(request)["status"] == "pending"
    _wait_for(lambda: notifications)
    reply = bridge.handle_operator_text(
        profile="otto", session_key="session-1", text="wrzucaj"
    )
    assert reply and "Approved once" in reply
    _wait_for(lambda: bridge._pending[request["request_id"]].status == "approved")

    now[0] = 105.001
    expired = bridge.poll(request)

    assert expired["status"] == "timeout"
    assert expired["consumed"] is True
    assert bridge.poll(request)["status"] == "stale"


def test_terminal_approval_records_are_retained_with_a_hard_bound():
    assert ProtectedApprovalBridge is not None and OperatorTarget is not None
    notifications = []
    now = [200.0]
    target = OperatorTarget(
        profile="otto",
        session_id="session-1",
        platform="webui",
        task_id="t_worker",
    )
    bridge = ProtectedApprovalBridge(
        resolve_target=lambda request: (
            target if request["task_id"] == "t_worker" else None
        ),
        notify=lambda resolved, text: notifications.append((resolved, text)) or True,
        validate_run=lambda _request: True,
        monotonic=lambda: now[0],
        terminal_retention_seconds=2.0,
        max_terminal_records=2,
    )

    for index in range(4):
        request = _request(f"req-{index}", f"{index + 1:x}" * 64)
        assert bridge.submit(request)["status"] == "pending"
        _wait_for(lambda: len(notifications) == index + 1)
        reply = bridge.handle_operator_text(
            profile="otto", session_key="session-1", text="odrzuć"
        )
        assert reply and "Denied" in reply
        _wait_for(lambda: bridge._pending[request["request_id"]].status == "denied")
        assert bridge.poll(request)["status"] == "denied"

    assert len(bridge._pending) <= 2
    assert len(bridge._consumed) <= 2


def test_zombie_run_is_rejected_at_submit_without_leaking_claim(caplog):
    caplog.set_level(logging.INFO, logger="gateway.protected_approval_bridge")
    notifications = []
    bridge = _bridge(notifications, validate_run=lambda _request: False)

    result = bridge.submit(_request())

    assert result == {"ok": False, "status": "invalid_run"}
    assert notifications == []
    assert "run=41" in caplog.text
    assert "claim-secret" not in caplog.text


def test_operator_resolution_is_scoped_to_the_request_board():
    assert ProtectedApprovalBridge is not None and OperatorTarget is not None
    notifications = []
    seen_boards = []
    target = OperatorTarget(
        profile="otto",
        session_id="session-1",
        platform="webui",
        task_id="t_worker",
    )
    bridge = ProtectedApprovalBridge(
        resolve_target=lambda request: (
            seen_boards.append(request["board"]) or target
        ),
        notify=lambda resolved, text: notifications.append((resolved, text)) or True,
        validate_run=lambda _request: True,
    )
    request = {**_request(), "board": "team-a"}

    assert bridge.submit(request)["status"] == "pending"
    assert seen_boards == ["team-a"]


def test_run_reclaimed_after_approval_cannot_consume_grant(caplog):
    caplog.set_level(logging.INFO, logger="gateway.protected_approval_bridge")
    notifications = []
    current = [True]
    bridge = _bridge(notifications, validate_run=lambda _request: current[0])
    request = _request()
    assert bridge.submit(request)["status"] == "pending"
    _wait_for(lambda: notifications)

    reply = bridge.handle_operator_text(
        profile="otto", session_key="session-1", text="wrzucaj"
    )
    assert reply and "Approved once" in reply
    _wait_for(lambda: bridge._pending[request["request_id"]].status == "approved")
    current[0] = False

    result = bridge.poll(request)

    assert result["status"] == "invalid_run"
    assert result["consumed"] is True
    assert bridge.poll(request)["status"] == "stale"
    assert "run=41" in caplog.text
    assert "claim-secret" not in caplog.text


def test_multiplex_fallback_self_post_uses_secondary_profile_key(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from agent.secret_scope import set_multiplex_active
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter

    secondary_home = tmp_path / "profiles" / "otto"
    secondary_home.mkdir(parents=True)
    secondary_key = "secondary-profile-api-key"
    listener_key = "listener-owner-api-key"
    (secondary_home / ".env").write_text(
        f"API_SERVER_KEY={secondary_key}\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda **_kwargs: [("otto", secondary_home)],
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir",
        lambda profile: secondary_home if profile == "otto" else tmp_path,
    )

    async def scenario():
        wake_messages = []
        adapter = APIServerAdapter(
            PlatformConfig(enabled=True, extra={"key": listener_key})
        )

        class FakeAgent:
            session_prompt_tokens = 0
            session_completion_tokens = 0
            session_total_tokens = 0

            def __init__(self, session_id):
                self.session_id = session_id

            def run_conversation(self, *, user_message, **_kwargs):
                wake_messages.append(user_message)
                return {"final_response": "delivered", "messages": []}

        monkeypatch.setattr(
            adapter,
            "_create_agent",
            lambda **kwargs: FakeAgent(kwargs.get("session_id")),
        )

        class Runner:
            config = SimpleNamespace(
                multiplex_profiles=True,
                multiplex_profile_allowlist=None,
            )
            adapters = {Platform.API_SERVER: adapter}

            def _authorization_adapter(self, platform, profile):
                if platform == Platform.API_SERVER and profile == "default":
                    return adapter
                return None

        runner = Runner()
        adapter.gateway_runner = runner
        bridge = bridge_module.create_runtime_bridge(
            runner, asyncio.get_running_loop()
        )
        app = web.Application(
            middlewares=[adapter._make_profile_prefix_middleware()]
        )
        app.router.add_post(
            "/p/{profile}/v1/chat/completions", adapter._handle_chat_completions
        )
        target = bridge_module.OperatorTarget(
            profile="otto",
            session_id="agent:otto:webui:dm:user-7",
            platform="webui",
            task_id="t_worker",
        )
        set_multiplex_active(True)
        try:
            async with TestClient(TestServer(app)) as client:
                adapter._host = client.server.host
                assert client.server.port is not None
                adapter._port = int(client.server.port)

                denied = await client.post(
                    "/p/otto/v1/chat/completions",
                    headers={"Authorization": f"Bearer {listener_key}"},
                    json={
                        "model": "hermes-agent",
                        "messages": [{"role": "user", "content": "wrong key"}],
                    },
                )
                assert denied.status == 401

                delivered = await asyncio.get_running_loop().run_in_executor(
                    None, bridge._notify, target, "approval requested"
                )
                assert delivered is True
        finally:
            set_multiplex_active(False)

        assert wake_messages == ["approval requested"]

    asyncio.run(scenario())


def test_real_control_socket_returns_operator_decision_to_blocked_write(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter
    from hermes_cli import kanban_db as kb

    session_key = "agent:main:webui:dm:user-7"
    socket_home = tmp_path / "hermes-home"
    socket_home.mkdir()
    protected = tmp_path / "AGENTS.md"
    monkeypatch.setenv("HERMES_HOME", str(socket_home))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: socket_home)
    monkeypatch.setattr(
        "hermes_constants.get_default_hermes_root", lambda: socket_home
    )
    monkeypatch.setattr(
        "tools.file_tools._protected_instruction_config", lambda: (True, [])
    )
    from tools.terminal_tool import set_approval_callback

    set_approval_callback(None)
    conn = kb.connect(board="default")
    try:
        task_id = kb.create_task(
            conn, title="protected approval seam", assignee="wrench"
        )
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="webui",
            chat_id=session_key,
            notifier_profile="default",
        )
        claimed = kb.claim_task(conn, task_id, claimer="e2e-claim")
        assert claimed is not None
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", claimed.claim_lock)

    async def scenario():
        from tools.file_tools import write_file_tool

        wake_messages = []
        adapter = APIServerAdapter(
            PlatformConfig(enabled=True, extra={"key": "sk-secret"})
        )

        class FakeAgent:
            session_prompt_tokens = 0
            session_completion_tokens = 0
            session_total_tokens = 0

            def __init__(self, session_id):
                self.session_id = session_id

            def run_conversation(self, *, user_message, **_kwargs):
                wake_messages.append(user_message)
                return {"final_response": "approval request delivered", "messages": []}

        monkeypatch.setattr(
            adapter,
            "_create_agent",
            lambda **kwargs: FakeAgent(kwargs.get("session_id")),
        )

        class Runner:
            config = SimpleNamespace(multiplex_profiles=False)
            adapters = {Platform.API_SERVER: adapter}
            protected_approval_bridge: object | None = None

            def _authorization_adapter(self, platform, profile):
                if platform == Platform.API_SERVER and profile == "default":
                    return adapter
                return None

        runner = Runner()
        bridge = bridge_module.create_runtime_bridge(
            runner, asyncio.get_running_loop()
        )
        runner.protected_approval_bridge = bridge
        adapter.gateway_runner = runner
        app = web.Application()
        app.router.add_post(
            "/v1/chat/completions", adapter._handle_chat_completions
        )
        server = GatewayControlServer(
            socket_home, verb_handlers=bridge.control_handlers
        )
        assert await server.start()
        try:
            payload = {
                "model": "hermes-agent",
                "messages": [{"role": "user", "content": "wrzucaj"}],
            }
            async with TestClient(TestServer(app)) as client:
                adapter._host = client.server.host
                assert client.server.port is not None
                adapter._port = int(client.server.port)
                write_future = asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: write_file_tool(
                        str(protected), "operator-approved", task_id=task_id
                    ),
                )
                deadline = time.monotonic() + 2.0
                while not wake_messages and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                assert wake_messages

                denied = await client.post(
                    "/v1/chat/completions",
                    headers={
                        "Authorization": "Bearer wrong",
                        "X-Hermes-Session-Key": session_key,
                    },
                    json=payload,
                )
                assert denied.status == 401
                assert not write_future.done()

                wrong_session = await client.post(
                    "/v1/chat/completions",
                    headers={
                        "Authorization": "Bearer sk-secret",
                        "X-Hermes-Session-Key": "agent:main:webui:dm:other",
                    },
                    json=payload,
                )
                assert wrong_session.status == 200
                assert not write_future.done()

                approved = await client.post(
                    "/v1/chat/completions",
                    headers={
                        "Authorization": "Bearer sk-secret",
                        "X-Hermes-Session-Key": session_key,
                    },
                    json=payload,
                )
                assert approved.status == 200
                response = await approved.json()
                assert "Approved once" in response["choices"][0]["message"]["content"]
            return await write_future
        finally:
            await server.stop()

    result = asyncio.run(scenario())
    if isinstance(result, str):
        result = json.loads(result)
    assert not result.get("error"), result
    assert protected.read_text(encoding="utf-8") == "operator-approved"
