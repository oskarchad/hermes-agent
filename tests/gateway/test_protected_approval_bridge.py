"""Authority and correlation tests for headless protected-write approvals."""

import asyncio
import contextlib
import json
import logging
import threading
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


def _run_notice(request_id="req-run", fingerprint="a" * 64, timeout=2.0):
    return {
        "request_id": request_id,
        "task_id": "t_worker",
        "worker_run_id": 123,
        "operation_fingerprint": fingerprint,
        "operation": "patch",
        "paths": ["/repo/AGENTS.md"],
        "summary": "protected run write",
        "timeout_seconds": timeout,
        "deadline_monotonic": time.monotonic() + timeout,
        "approval_session_key": f"protected-write:{request_id}",
        "message": "approval requested",
    }


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("condition did not become true")


def _resolve_notice(notifications, choice, *, index=-1, request_id=None):
    notice = notifications[index][1]
    return approval.resolve_gateway_approval(
        notice["approval_session_key"],
        choice,
        request_id=request_id or notice["request_id"],
    )


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
        notify=lambda resolved, notice: notifications.append((resolved, notice)) or True,
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
    assert notifications and "AGENTS.md" in notifications[0][1]["message"]
    assert request["operation_fingerprint"] not in notifications[0][1]["message"]
    assert notifications[0][1]["operation_fingerprint"] == request["operation_fingerprint"]
    assert request["summary"] in notifications[0][1]["message"]

    assert _resolve_notice(notifications, "once", request_id="req-other") == 0
    assert bridge.poll(request)["status"] == "pending"
    assert _resolve_notice(notifications, "once") == 1

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
    assert _resolve_notice(notifications, "deny") == 1
    def poll_denied():
        result = bridge.poll(request)
        return result if result.get("status") != "pending" else None

    result = _wait_for(poll_denied)
    assert result["status"] == "denied"
    assert result["choice"] == "deny"


def test_exact_request_response_cannot_resolve_a_different_pending_request():
    notifications = []
    bridge = _bridge(notifications)
    first = _request("req-1", "a" * 64)
    second = _request("req-2", "b" * 64)
    bridge.submit(first)
    bridge.submit(second)
    _wait_for(lambda: len(notifications) == 2)

    first_notice = next(notice for _, notice in notifications if notice["request_id"] == "req-1")
    assert approval.resolve_gateway_approval(
        first_notice["approval_session_key"], "once", request_id="req-2"
    ) == 0
    assert bridge.poll(first)["status"] == "pending"
    assert bridge.poll(second)["status"] == "pending"
    for _, notice in notifications:
        assert approval.resolve_gateway_approval(
            notice["approval_session_key"], "deny", request_id=notice["request_id"]
        ) == 1


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
        notify=lambda resolved, notice: notifications.append((resolved, notice)) or True,
        validate_run=lambda _request: True,
        monotonic=lambda: now[0],
    )
    request = {**_request(), "timeout_seconds": 5.0}

    assert bridge.submit(request)["status"] == "pending"
    _wait_for(lambda: notifications)
    assert _resolve_notice(notifications, "once") == 1
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
        notify=lambda resolved, notice: notifications.append((resolved, notice)) or True,
        validate_run=lambda _request: True,
        monotonic=lambda: now[0],
        terminal_retention_seconds=2.0,
        max_terminal_records=2,
    )

    for index in range(4):
        request = _request(f"req-{index}", f"{index + 1:x}" * 64)
        assert bridge.submit(request)["status"] == "pending"
        _wait_for(lambda: len(notifications) == index + 1)
        assert _resolve_notice(notifications, "deny", index=index) == 1
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
        notify=lambda resolved, notice: notifications.append((resolved, notice)) or True,
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

    assert _resolve_notice(notifications, "once") == 1
    _wait_for(lambda: bridge._pending[request["request_id"]].status == "approved")
    current[0] = False

    result = bridge.poll(request)

    assert result["status"] == "invalid_run"
    assert result["consumed"] is True
    assert bridge.poll(request)["status"] == "stale"
    assert "run=41" in caplog.text
    assert "claim-secret" not in caplog.text


def test_multiplex_fallback_presents_to_exact_secondary_profile_run(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from agent.secret_scope import set_multiplex_active
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter

    secondary_home = tmp_path / "profiles" / "otto"
    secondary_home.mkdir(parents=True)
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda **_kwargs: [("otto", secondary_home)],
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir",
        lambda profile: secondary_home if profile == "otto" else tmp_path,
    )

    async def scenario():
        adapter = APIServerAdapter(
            PlatformConfig(enabled=True, extra={"key": "listener-owner-api-key"})
        )
        run_id = "run-secondary"
        session_id = "agent:otto:webui:dm:user-7"
        adapter._run_statuses[run_id] = {
            "run_id": run_id,
            "status": "running",
            "session_id": session_id,
            "profile": "otto",
        }
        adapter._run_operator_sessions[run_id] = frozenset({session_id})
        adapter._run_streams[run_id] = asyncio.Queue()
        active_run = asyncio.create_task(asyncio.Event().wait())
        adapter._active_run_tasks[run_id] = active_run

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
        target = bridge_module.OperatorTarget(
            profile="otto",
            session_id=session_id,
            platform="webui",
            task_id="t_worker",
        )
        notice = {
            "request_id": "req-secondary",
            "task_id": "t_worker",
            "worker_run_id": 123,
            "operation_fingerprint": "a" * 64,
            "operation": "patch",
            "paths": ["/repo/AGENTS.md"],
            "summary": "protected secondary write",
            "timeout_seconds": 2.0,
            "deadline_monotonic": time.monotonic() + 2.0,
            "approval_session_key": "protected-write:req-secondary",
            "message": "approval requested",
        }
        set_multiplex_active(True)
        try:
            delivered = await asyncio.get_running_loop().run_in_executor(
                None, bridge._notify, target, notice
            )
            assert delivered is True
            event = await asyncio.wait_for(
                adapter._run_streams[run_id].get(), timeout=1.0
            )
            assert event["request_id"] == "req-secondary"
            assert event["run_id"] == run_id
            assert event["task_id"] == "t_worker"
            assert event["worker_run_id"] == 123
        finally:
            set_multiplex_active(False)
            active_run.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await active_run

    asyncio.run(scenario())


def test_run_presentation_rejects_wrong_profile_session_and_zombie():
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter

    async def scenario():
        adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "key"}))
        session_id = "agent:main:webui:dm:user-7"
        run_id = "run-zombie"
        adapter._run_statuses[run_id] = {
            "run_id": run_id,
            "status": "running",
            "session_id": session_id,
            "profile": "default",
        }
        adapter._run_operator_sessions[run_id] = frozenset({session_id})
        adapter._run_streams[run_id] = asyncio.Queue()
        zombie = asyncio.create_task(asyncio.sleep(0))
        await zombie
        adapter._active_run_tasks[run_id] = zombie

        assert not await adapter.present_protected_approval(
            profile="other",
            session_id=session_id,
            approval_session_key="protected-write:req-run",
            approval_data=_run_notice(),
        )
        assert not await adapter.present_protected_approval(
            profile="default",
            session_id="agent:main:webui:dm:other",
            approval_session_key="protected-write:req-run",
            approval_data=_run_notice(),
        )
        assert not await adapter.present_protected_approval(
            profile="default",
            session_id=session_id,
            approval_session_key="protected-write:req-run",
            approval_data=_run_notice(),
        )
        assert adapter._run_protected_approvals == {}
        assert adapter._run_streams[run_id].empty()

    asyncio.run(scenario())


def test_run_presentation_rejects_ambiguous_same_session_runs():
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter

    async def scenario():
        adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "key"}))
        session_id = "agent:main:webui:dm:user-7"
        tasks = []
        for run_id in ("run-one", "run-two"):
            adapter._run_statuses[run_id] = {
                "run_id": run_id,
                "status": "running",
                "session_id": session_id,
                "profile": "default",
            }
            adapter._run_operator_sessions[run_id] = frozenset({session_id})
            adapter._run_streams[run_id] = asyncio.Queue()
            task = asyncio.create_task(asyncio.Event().wait())
            adapter._active_run_tasks[run_id] = task
            tasks.append(task)
        try:
            assert not await adapter.present_protected_approval(
                profile="default",
                session_id=session_id,
                approval_session_key="protected-write:req-run",
                approval_data=_run_notice(),
            )
            assert adapter._run_protected_approvals == {}
            assert all(queue.empty() for queue in adapter._run_streams.values())
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


def test_run_presentation_requires_server_recorded_operator_session():
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter

    async def scenario():
        adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "key"}))
        session_id = "agent:main:webui:dm:user-7"
        run_id = "run-unbound"
        adapter._run_statuses[run_id] = {
            "run_id": run_id,
            "status": "running",
            "session_id": session_id,
            "profile": "default",
        }
        adapter._run_streams[run_id] = asyncio.Queue()
        active_run = asyncio.create_task(asyncio.Event().wait())
        adapter._active_run_tasks[run_id] = active_run
        try:
            assert not await adapter.present_protected_approval(
                profile="default",
                session_id=session_id,
                approval_session_key="protected-write:req-run",
                approval_data=_run_notice(),
            )
            assert adapter._run_protected_approvals == {}
            assert adapter._run_streams[run_id].empty()
        finally:
            active_run.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await active_run

    asyncio.run(scenario())


def test_run_presentation_rejects_mismatched_bridge_approval_session():
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter

    async def scenario():
        adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "key"}))
        session_id = "agent:main:webui:dm:user-7"
        run_id = "run-wrong-queue"
        adapter._run_statuses[run_id] = {
            "run_id": run_id,
            "status": "running",
            "session_id": session_id,
            "profile": "default",
        }
        adapter._run_operator_sessions[run_id] = frozenset({session_id})
        adapter._run_streams[run_id] = asyncio.Queue()
        active_run = asyncio.create_task(asyncio.Event().wait())
        adapter._active_run_tasks[run_id] = active_run
        try:
            assert not await adapter.present_protected_approval(
                profile="default",
                session_id=session_id,
                approval_session_key="protected-write:req-other",
                approval_data=_run_notice(),
            )
            assert adapter._run_protected_approvals == {}
            assert adapter._run_streams[run_id].empty()
        finally:
            active_run.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await active_run

    asyncio.run(scenario())


def test_protected_pending_rejects_unknown_request_without_resolving_native_queue():
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter

    async def scenario():
        adapter = APIServerAdapter(
            PlatformConfig(enabled=True, extra={"key": "sk-secret"})
        )
        run_id = "run-separated-approval-classes"
        protected_request_id = "req-protected"
        native_request_id = "approval-native"
        native_entry = approval._ApprovalEntry({"request_id": native_request_id})
        adapter._run_statuses[run_id] = {
            "run_id": run_id,
            "status": "waiting_for_approval",
        }
        adapter._run_approval_sessions[run_id] = run_id
        adapter._run_protected_approvals[run_id] = {
            protected_request_id: {
                "request_id": protected_request_id,
                "operation_fingerprint": "a" * 64,
                "fingerprint_prefix": "a" * 12,
                "approval_session_key": f"protected-write:{protected_request_id}",
                "deadline_monotonic": time.monotonic() + 5.0,
            }
        }
        with approval._lock:
            approval._gateway_queues[run_id] = [native_entry]
        app = web.Application()
        app.router.add_post(
            "/v1/runs/{run_id}/approval", adapter._handle_run_approval
        )
        try:
            async with TestClient(TestServer(app)) as client:
                response = await client.post(
                    f"/v1/runs/{run_id}/approval",
                    headers={"Authorization": "Bearer sk-secret"},
                    json={
                        "choice": "once",
                        "request_id": native_request_id,
                        "operation_fingerprint": "a" * 12,
                    },
                )
                body = await response.json()
        finally:
            approval.unregister_gateway_notify(run_id)

        assert response.status == 409
        assert body["error"]["code"] == "approval_not_pending"
        assert native_entry.result is None
        assert adapter._run_protected_approvals[run_id][protected_request_id]

    asyncio.run(scenario())


def test_protected_presentation_does_not_replace_visible_native_approval():
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter

    async def scenario():
        adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "key"}))
        run_id = "run-native-first"
        session_id = "agent:main:webui:dm:user-native-first"
        native_request_id = "approval-native-first"
        native_event = {
            "event": "approval.request",
            "run_id": run_id,
            "request_id": native_request_id,
            "command": "rm native-first.txt",
            "choices": ["once", "deny"],
            "timestamp": time.time(),
        }
        adapter._run_statuses[run_id] = {
            "run_id": run_id,
            "status": "waiting_for_approval",
            "session_id": session_id,
            "profile": "default",
            "approval": native_event,
        }
        adapter._run_operator_sessions[run_id] = frozenset({session_id})
        adapter._run_streams[run_id] = asyncio.Queue()
        active_run = asyncio.create_task(asyncio.Event().wait())
        adapter._active_run_tasks[run_id] = active_run
        try:
            assert not await adapter.present_protected_approval(
                profile="default",
                session_id=session_id,
                approval_session_key="protected-write:req-native-first",
                approval_data=_run_notice(request_id="req-native-first"),
            )
            assert adapter._run_statuses[run_id]["approval"] == native_event
            assert adapter._run_protected_approvals == {}
            assert adapter._run_streams[run_id].empty()
        finally:
            active_run.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await active_run

    asyncio.run(scenario())


def test_terminal_run_status_clears_unpublished_native_approval():
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter

    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "key"}))
    run_id = "run-terminal-overlap"
    adapter._run_deferred_native_approvals[run_id] = [
        {"request_id": "approval-native-terminal"}
    ]

    adapter._set_run_status(run_id, "completed", last_event="run.completed")

    assert run_id not in adapter._run_deferred_native_approvals


def test_native_approval_waits_until_visible_protected_request_is_resolved(monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter

    session_id = "agent:main:webui:dm:user-overlap"
    protected_request_id = "req-protected-overlap"
    native_request_id = "approval-native-overlap"
    native_notified = threading.Event()

    class NativeApprovalAgent:
        session_prompt_tokens = 0
        session_completion_tokens = 0
        session_total_tokens = 0

        def run_conversation(self, **_kwargs):
            approval_session_key = approval.get_current_session_key()
            native_entry = approval._ApprovalEntry(
                {
                    "request_id": native_request_id,
                    "command": "rm -rf /tmp/native-overlap",
                    "description": "native overlap request",
                    "pattern_key": "native-overlap",
                    "pattern_keys": ["native-overlap"],
                }
            )
            with approval._lock:
                approval._gateway_queues.setdefault(
                    approval_session_key, []
                ).append(native_entry)
                notify = approval._gateway_notify_cbs[approval_session_key]
            notify(dict(native_entry.data))
            native_notified.set()
            assert native_entry.event.wait(timeout=2.0)
            return {"final_response": f"native:{native_entry.result}"}

    async def scenario():
        adapter = APIServerAdapter(
            PlatformConfig(enabled=True, extra={"key": "sk-secret"})
        )
        monkeypatch.setattr(
            adapter,
            "_create_agent",
            lambda **_kwargs: NativeApprovalAgent(),
        )
        app = web.Application()
        app.router.add_post("/v1/runs", adapter._handle_runs)
        app.router.add_post(
            "/v1/runs/{run_id}/approval", adapter._handle_run_approval
        )

        async with TestClient(TestServer(app)) as client:
            started = await client.post(
                "/v1/runs",
                headers={
                    "Authorization": "Bearer sk-secret",
                    "X-Hermes-Session-Key": session_id,
                },
                json={"input": "overlap", "session_id": session_id},
            )
            assert started.status == 202
            run_id = (await started.json())["run_id"]
            run_task = adapter._active_run_tasks[run_id]

            protected_session_key = f"protected-write:{protected_request_id}"
            protected_entry = approval._ApprovalEntry(
                {"request_id": protected_request_id}
            )
            with approval._lock:
                approval._gateway_queues[protected_session_key] = [
                    protected_entry
                ]
            notice = _run_notice(
                request_id=protected_request_id,
                fingerprint="c" * 64,
            )
            assert await adapter.present_protected_approval(
                profile="default",
                session_id=session_id,
                approval_session_key=protected_session_key,
                approval_data=notice,
            )
            protected_event = None
            while (
                protected_event is None
                or protected_event.get("event") != "approval.request"
            ):
                protected_event = await asyncio.wait_for(
                    adapter._run_streams[run_id].get(), timeout=2.0
                )
            assert protected_event["request_id"] == protected_request_id
            assert protected_event["protected_write"] is True
            assert adapter._run_statuses[run_id]["approval"]["request_id"] == (
                protected_request_id
            )

            notified = await asyncio.get_running_loop().run_in_executor(
                None, native_notified.wait, 1.0
            )
            assert notified is True
            await asyncio.sleep(0)
            visible = adapter._run_statuses[run_id]["approval"]
            assert visible["request_id"] == protected_request_id
            assert visible["protected_write"] is True

            protected_response = await client.post(
                f"/v1/runs/{run_id}/approval",
                headers={"Authorization": "Bearer sk-secret"},
                json={
                    "choice": "once",
                    "request_id": protected_request_id,
                    "operation_fingerprint": "c" * 12,
                },
            )
            assert protected_response.status == 200
            assert protected_entry.result == "once"
            promoted = adapter._run_statuses[run_id]["approval"]
            assert promoted["request_id"] == native_request_id
            assert promoted.get("protected_write") is not True

            responded_event = await asyncio.wait_for(
                adapter._run_streams[run_id].get(), timeout=2.0
            )
            assert responded_event["event"] == "approval.responded"
            assert responded_event["request_id"] == protected_request_id
            native_event = await asyncio.wait_for(
                adapter._run_streams[run_id].get(), timeout=2.0
            )
            assert native_event["event"] == "approval.request"
            assert native_event["request_id"] == native_request_id

            native_response = await client.post(
                f"/v1/runs/{run_id}/approval",
                headers={"Authorization": "Bearer sk-secret"},
                json={"choice": "deny", "request_id": native_request_id},
            )
            assert native_response.status == 200
            await asyncio.wait_for(run_task, timeout=2.0)

        assert protected_entry.result == "once"
        assert adapter._run_protected_approvals == {}
        assert adapter._run_statuses[run_id]["status"] == "completed"
        assert adapter._run_statuses[run_id]["output"] == "native:deny"

    try:
        asyncio.run(scenario())
    finally:
        approval.unregister_gateway_notify(
            f"protected-write:{protected_request_id}"
        )


@pytest.mark.parametrize("orphaned", [False, True])
def test_unavailable_protected_request_promotes_deferred_native_approval(orphaned):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter

    async def scenario():
        adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "key"}))
        session_id = "agent:main:webui:dm:user-7"
        run_id = "run-expiring"
        request_id = "req-expiring"
        native_request_id = "approval-native-after-expiry"
        approval_session_key = f"protected-write:{request_id}"
        native_session_key = f"run-approval:{run_id}"
        adapter._run_statuses[run_id] = {
            "run_id": run_id,
            "status": "running",
            "session_id": session_id,
            "profile": "default",
        }
        adapter._run_operator_sessions[run_id] = frozenset({session_id})
        adapter._run_streams[run_id] = asyncio.Queue()
        active_run = asyncio.create_task(asyncio.Event().wait())
        adapter._active_run_tasks[run_id] = active_run
        adapter._run_approval_sessions[run_id] = native_session_key
        entry = approval._ApprovalEntry({"request_id": request_id})
        native_event = {
            "event": "approval.request",
            "run_id": run_id,
            "request_id": native_request_id,
            "command": "rm after-expiry.txt",
            "choices": ["once", "deny"],
            "timestamp": time.time(),
        }
        native_entry = approval._ApprovalEntry(dict(native_event))
        with approval._lock:
            approval._gateway_queues[approval_session_key] = [entry]
            approval._gateway_queues[native_session_key] = [native_entry]
        app = web.Application()
        app.router.add_post(
            "/v1/runs/{run_id}/approval", adapter._handle_run_approval
        )
        try:
            assert await adapter.present_protected_approval(
                profile="default",
                session_id=session_id,
                approval_session_key=approval_session_key,
                approval_data=_run_notice(request_id=request_id, timeout=10.0),
            )
            requested = await asyncio.wait_for(
                adapter._run_streams[run_id].get(), timeout=1.0
            )
            assert requested["event"] == "approval.request"
            if orphaned:
                approval.unregister_gateway_notify(approval_session_key)
            else:
                record = adapter._run_protected_approvals[run_id][request_id]
                record["expiry_handle"].cancel()
                record["deadline_monotonic"] = time.monotonic() - 1.0
            adapter._run_deferred_native_approvals[run_id] = [native_event]
            async with TestClient(TestServer(app)) as client:
                response = await client.post(
                    f"/v1/runs/{run_id}/approval",
                    headers={"Authorization": "Bearer key"},
                    json={
                        "choice": "once",
                        "request_id": request_id,
                        "operation_fingerprint": "a" * 12,
                    },
                )
                assert response.status == 409
            expired = await asyncio.wait_for(
                adapter._run_streams[run_id].get(), timeout=1.0
            )
            assert expired["event"] == "approval.expired"
            assert expired["run_id"] == run_id
            assert expired["request_id"] == request_id
            promoted = await asyncio.wait_for(
                adapter._run_streams[run_id].get(), timeout=1.0
            )
            assert promoted["request_id"] == native_request_id
            if not orphaned:
                assert entry.result == "deny"
            assert adapter._run_protected_approvals == {}
            assert adapter._run_statuses[run_id]["approval"]["request_id"] == (
                native_request_id
            )
        finally:
            active_run.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await active_run

    asyncio.run(scenario())


def test_run_approval_endpoint_returns_exact_operator_decision_to_blocked_write(
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

        adapter = APIServerAdapter(
            PlatformConfig(enabled=True, extra={"key": "sk-secret"})
        )
        run_id = "run_active_operator"
        adapter._run_statuses[run_id] = {
            "run_id": run_id,
            "status": "running",
            "session_id": session_key,
            "profile": "default",
        }
        adapter._run_operator_sessions[run_id] = frozenset({session_key})
        adapter._run_streams[run_id] = asyncio.Queue()
        adapter._run_streams[run_id].put_nowait(
            {"event": "run.started", "run_id": run_id, "timestamp": time.time()}
        )
        active_run = asyncio.create_task(asyncio.Event().wait())
        adapter._active_run_tasks[run_id] = active_run

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
        app.router.add_get(
            "/v1/runs/{run_id}/events", adapter._handle_run_events
        )
        app.router.add_post(
            "/v1/runs/{run_id}/approval", adapter._handle_run_approval
        )
        server = GatewayControlServer(
            socket_home, verb_handlers=bridge.control_handlers
        )
        assert await server.start()
        try:
            async with TestClient(TestServer(app)) as client:
                events = await client.get(
                    f"/v1/runs/{run_id}/events",
                    headers={"Authorization": "Bearer sk-secret"},
                )
                assert events.status == 200
                write_future = asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: write_file_tool(
                        str(protected), "operator-approved", task_id=task_id
                    ),
                )
                event = None
                while event is None or event.get("event") != "approval.request":
                    line = await asyncio.wait_for(
                        events.content.readline(), timeout=2.0
                    )
                    assert line
                    if line.startswith(b"data: "):
                        event = json.loads(line[len(b"data: ") :])
                assert event["event"] == "approval.request"
                assert event["run_id"] == run_id
                assert event["request_id"]
                assert event["task_id"] == task_id
                assert event["worker_run_id"] == claimed.current_run_id
                assert event["operation_fingerprint"]
                assert len(event["operation_fingerprint"]) == 12
                assert event["choices"] == ["once", "deny"]
                assert "approval_session_key" not in event
                assert "session_id" not in event

                wrong_run = await client.post(
                    "/v1/runs/run-other/approval",
                    headers={"Authorization": "Bearer sk-secret"},
                    json={
                        "choice": "once",
                        "request_id": event["request_id"],
                        "operation_fingerprint": event["operation_fingerprint"],
                    },
                )
                assert wrong_run.status == 404

                denied = await client.post(
                    f"/v1/runs/{run_id}/approval",
                    headers={"Authorization": "Bearer wrong"},
                    json={"choice": "once", "request_id": event["request_id"]},
                )
                assert denied.status == 401
                assert not write_future.done()

                wrong_request = await client.post(
                    f"/v1/runs/{run_id}/approval",
                    headers={"Authorization": "Bearer sk-secret"},
                    json={
                        "choice": "once",
                        "request_id": "approval-other",
                        "operation_fingerprint": event["operation_fingerprint"],
                    },
                )
                assert wrong_request.status == 409
                assert not write_future.done()

                wrong_fingerprint = await client.post(
                    f"/v1/runs/{run_id}/approval",
                    headers={"Authorization": "Bearer sk-secret"},
                    json={
                        "choice": "once",
                        "request_id": event["request_id"],
                        "operation_fingerprint": "b" * 12,
                    },
                )
                assert wrong_fingerprint.status == 409
                assert not write_future.done()

                approved = await client.post(
                    f"/v1/runs/{run_id}/approval",
                    headers={"Authorization": "Bearer sk-secret"},
                    json={
                        "choice": "once",
                        "request_id": event["request_id"],
                        "operation_fingerprint": event["operation_fingerprint"],
                    },
                )
                assert approved.status == 200
                response = await approved.json()
                assert response["request_id"] == event["request_id"]
                assert response["resolved"] == 1

                replay = await client.post(
                    f"/v1/runs/{run_id}/approval",
                    headers={"Authorization": "Bearer sk-secret"},
                    json={
                        "choice": "once",
                        "request_id": event["request_id"],
                        "operation_fingerprint": event["operation_fingerprint"],
                    },
                )
                assert replay.status == 409
                events.close()
            return await asyncio.wait_for(write_future, timeout=2.0)
        finally:
            await server.stop()
            active_run.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await active_run

    result = asyncio.run(scenario())
    if isinstance(result, str):
        result = json.loads(result)
    assert not result.get("error"), result
    assert protected.read_text(encoding="utf-8") == "operator-approved"
