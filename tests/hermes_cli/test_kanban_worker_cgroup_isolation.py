from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


def _make_task(kb, *, claim_lock: str = "test-host:claim-token"):
    return kb.Task(
        id="t_scope_worker",
        title="scope worker",
        body=None,
        assignee="patch",
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=1,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=None,
        claim_lock=claim_lock,
        claim_expires=None,
        tenant=None,
        current_run_id=17,
    )


def _prepare_spawn(monkeypatch, tmp_path):
    from hermes_cli import kanban_db as kb

    root = tmp_path / ".hermes"
    (root / "profiles" / "patch").mkdir(parents=True)
    root.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(kb, "_retag_legacy_worker_sessions", lambda _root: None)
    monkeypatch.setattr(kb, "worker_logs_dir", lambda board=None: tmp_path / "logs")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return kb, workspace


@pytest.mark.linux_only
def test_systemd_owned_dispatcher_spawns_worker_in_sibling_scope(monkeypatch, tmp_path):
    kb, workspace = _prepare_spawn(monkeypatch, tmp_path)
    monkeypatch.setattr(kb, "_systemd_worker_scope_required", lambda: True, raising=False)
    monkeypatch.setattr(
        kb, "_systemd_run_user_scope_available", lambda: True, raising=False
    )

    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["start_new_session"] = kwargs["start_new_session"]
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    pid = kb._default_spawn(_make_task(kb), str(workspace))

    assert pid == 4242
    assert captured["cmd"][:4] == [
        "/usr/bin/systemd-run",
        "--user",
        "--scope",
        "--quiet",
    ]
    assert "--collect" in captured["cmd"]
    unit_index = captured["cmd"].index("--unit")
    unit_name = captured["cmd"][unit_index + 1]
    assert unit_name.startswith("hermes-kanban-worker-")
    assert "claim-token" not in unit_name
    assert captured["cmd"][captured["cmd"].index("--") + 1] == "hermes"
    assert captured["start_new_session"] is True


@pytest.mark.linux_only
def test_systemd_owned_dispatcher_fails_closed_when_scope_is_unavailable(
    monkeypatch, tmp_path
):
    kb, workspace = _prepare_spawn(monkeypatch, tmp_path)
    monkeypatch.setattr(kb, "_systemd_worker_scope_required", lambda: True, raising=False)
    monkeypatch.setattr(
        kb, "_systemd_run_user_scope_available", lambda: False, raising=False
    )

    def forbidden_popen(*_args, **_kwargs):
        raise AssertionError("worker must not spawn in the gateway service cgroup")

    monkeypatch.setattr(subprocess, "Popen", forbidden_popen)

    with pytest.raises(RuntimeError, match="transient scope.*unavailable"):
        kb._default_spawn(_make_task(kb), str(workspace))


@pytest.mark.linux_only
def test_reclaim_stops_exact_worker_scope_before_pid_fallback(monkeypatch):
    from hermes_cli import kanban_db as kb

    host = kb._claimer_id().split(":", 1)[0]
    claim_lock = f"{host}:owned-claim"
    stopped = []
    signalled = []
    state = {"alive": True}

    def stop_scope(lock):
        stopped.append(lock)
        state["alive"] = False
        return True

    monkeypatch.setattr(kb, "_stop_kanban_worker_scope", stop_scope, raising=False)
    monkeypatch.setattr(kb, "_systemd_worker_scope_required", lambda: True)
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: state["alive"])

    result = kb._terminate_reclaimed_worker(
        12345,
        claim_lock,
        signal_fn=lambda pid, sig: signalled.append((pid, sig)),
    )

    assert stopped == [claim_lock]
    assert signalled == []
    assert result["terminated"] is True
    assert result["termination_target"] == "systemd_scope"


@pytest.mark.linux_only
def test_scope_stop_requires_inactive_verification(monkeypatch):
    from hermes_cli import kanban_db as kb
    from tools import process_registry

    stopped = []
    monkeypatch.setattr(
        process_registry,
        "_stop_systemd_unit",
        lambda unit: stopped.append(unit) or True,
    )
    monkeypatch.setattr(
        kb,
        "_systemd_worker_scope_inactive",
        lambda _unit: False,
        raising=False,
    )

    assert kb._stop_kanban_worker_scope("host:pid:scope-token") is False
    assert stopped == [kb._kanban_worker_scope_unit("host:pid:scope-token")]


@pytest.mark.linux_only
def test_dead_wrapper_does_not_hide_failed_scope_cleanup(monkeypatch):
    from hermes_cli import kanban_db as kb

    host = kb._claimer_id().split(":", 1)[0]
    monkeypatch.setattr(kb, "_stop_kanban_worker_scope", lambda _lock: False)
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)

    result = kb._terminate_reclaimed_worker(
        12345,
        f"{host}:owned-scope",
        scope_expected=True,
    )

    assert result["terminated"] is True
    assert result["scope_stop_attempted"] is True
    assert result["scope_stopped"] is False
    assert result["cleanup_verified"] is False
    assert kb._worker_survived_termination(result) is True


@pytest.mark.linux_only
def test_reclaim_never_stops_scope_for_foreign_host(monkeypatch):
    from hermes_cli import kanban_db as kb

    stopped = []
    signalled = []
    monkeypatch.setattr(
        kb,
        "_stop_kanban_worker_scope",
        lambda lock: stopped.append(lock) or True,
        raising=False,
    )

    result = kb._terminate_reclaimed_worker(
        12345,
        "another-host:claim",
        signal_fn=lambda pid, sig: signalled.append((pid, sig)),
    )

    assert stopped == []
    assert signalled == []
    assert result["host_local"] is False


def _alive_non_zombie(pid: int) -> bool:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (FileNotFoundError, ProcessLookupError):
        return False
    right = raw.rsplit(")", 1)
    return len(right) == 2 and not right[1].lstrip().startswith("Z")


@pytest.mark.linux_only
@pytest.mark.live_system_guard_bypass
def test_reclaim_fallback_kills_worker_process_group_without_orphan(tmp_path):
    from hermes_cli import kanban_db as kb

    child_pid_file = tmp_path / "child.pid"
    parent_code = (
        "import pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding='utf-8')\n"
        "time.sleep(60)\n"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_code, str(child_pid_file)],
        start_new_session=True,
    )
    child_pid = None
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not child_pid_file.exists():
            time.sleep(0.05)
        assert child_pid_file.exists(), "worker child did not start"
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        assert _alive_non_zombie(parent.pid)
        assert _alive_non_zombie(child_pid)

        host = kb._claimer_id().split(":", 1)[0]
        result = kb._terminate_reclaimed_worker(
            parent.pid,
            f"{host}:legacy-unscoped-worker",
        )

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (
            _alive_non_zombie(parent.pid) or _alive_non_zombie(child_pid)
        ):
            time.sleep(0.05)
        assert result["terminated"] is True
        assert not _alive_non_zombie(parent.pid)
        assert not _alive_non_zombie(child_pid)
        assert result["termination_target"] == "process_group"
    finally:
        try:
            os.killpg(parent.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            parent.wait(timeout=5)
        except subprocess.TimeoutExpired:
            parent.kill()
            parent.wait(timeout=5)


def test_timeout_uses_scope_aware_cleanup_and_records_sanitized_target(
    monkeypatch, tmp_path
):
    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="bounded worker",
            assignee="patch",
            max_runtime_seconds=1,
        )
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        kb._set_worker_pid(conn, task_id, 45678)
        old = int(time.time()) - 30
        conn.execute(
            "UPDATE task_runs SET started_at = ? WHERE id = ?",
            (old, claimed.current_run_id),
        )
        conn.commit()

        cleanups = []

        def fake_cleanup(pid, claim_lock, **_kwargs):
            cleanups.append((pid, claim_lock))
            return {
                "prev_pid": pid,
                "host_local": True,
                "termination_attempted": True,
                "terminated": True,
                "sigkill": False,
                "termination_target": "systemd_scope",
                "scope_stop_attempted": True,
                "scope_stopped": True,
            }

        monkeypatch.setattr(kb, "_terminate_reclaimed_worker", fake_cleanup)

        assert kb.enforce_max_runtime(conn) == [task_id]
        assert cleanups == [(45678, claimed.claim_lock)]

        event = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'timed_out' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        payload = json.loads(event["payload"])
        assert payload["termination_target"] == "systemd_scope"
        assert payload["scope_stopped"] is True
        assert claimed.claim_lock not in event["payload"]
    finally:
        conn.close()


def test_dispatcher_restart_keeps_live_run_identity_and_does_not_duplicate_claim(
    monkeypatch, tmp_path
):
    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="survive restart", assignee="patch")
        claimed = kb.claim_task(conn, task_id, ttl_seconds=3600)
        assert claimed is not None
        worker_pid = 56789
        kb._set_worker_pid(conn, task_id, worker_pid)
        assert kb.heartbeat_worker(
            conn, task_id, note="still working", expected_run_id=claimed.current_run_id
        )
        before = kb.get_task(conn, task_id)
        assert before is not None

        # A restarted gateway creates a fresh dispatcher connection/process,
        # but the board remains the source of run ownership. A live PID and
        # unexpired claim must make this tick observational only.
        conn.close()
        conn = kb.connect()
        monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid == worker_pid)

        spawns = []
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda *_args, **_kwargs: spawns.append("duplicate") or 99999,
        )

        after = kb.get_task(conn, task_id)
        assert after is not None
        assert result.spawned == []
        assert spawns == []
        assert after.status == "running"
        assert after.current_run_id == before.current_run_id
        assert after.last_heartbeat_at == before.last_heartbeat_at
        assert after.worker_pid == worker_pid
    finally:
        conn.close()


def test_spawn_event_identifies_scope_without_exposing_claim(monkeypatch, tmp_path):
    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="audited spawn", assignee="patch")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        scope_unit = kb._kanban_worker_scope_unit(claimed.claim_lock)
        pid = kb._SpawnedWorkerPid(
            67890,
            isolation_mode="systemd_scope",
            scope_unit=scope_unit,
        )

        kb._set_worker_pid(conn, task_id, pid)

        event = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'spawned' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        payload = json.loads(event["payload"])
        assert payload == {
            "pid": 67890,
            "isolation_mode": "systemd_scope",
            "scope_unit": scope_unit,
        }
        assert claimed.claim_lock not in event["payload"]
    finally:
        conn.close()


def test_timeout_defers_release_when_exact_worker_scope_survives(
    monkeypatch, tmp_path
):
    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="unkillable bounded worker",
            assignee="patch",
            max_runtime_seconds=1,
        )
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        kb._set_worker_pid(conn, task_id, 78901)
        conn.execute(
            "UPDATE task_runs SET started_at = ? WHERE id = ?",
            (int(time.time()) - 30, claimed.current_run_id),
        )
        conn.commit()
        monkeypatch.setattr(
            kb,
            "_terminate_reclaimed_worker",
            lambda *_args, **_kwargs: {
                "prev_pid": 78901,
                "host_local": True,
                "termination_attempted": True,
                "terminated": False,
                "sigkill": True,
                "termination_target": "systemd_scope",
                "scope_stop_attempted": True,
                "scope_stopped": False,
            },
        )

        assert kb.enforce_max_runtime(conn) == []

        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "running"
        assert task.current_run_id == claimed.current_run_id
        assert task.worker_pid == 78901
        event = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'reclaim_deferred' "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        assert json.loads(event["payload"])["reason"] == "max_runtime_worker_alive"
    finally:
        conn.close()


@pytest.mark.linux_only
def test_scope_unavailable_is_spawn_failure_not_worker_crash(monkeypatch, tmp_path):
    kb, workspace = _prepare_spawn(monkeypatch, tmp_path)
    monkeypatch.setattr(kb, "_systemd_worker_scope_required", lambda: True)
    monkeypatch.setattr(kb, "_systemd_run_user_scope_available", lambda: False)
    monkeypatch.setattr(kb, "resolve_workspace", lambda _task, board=None: workspace)

    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="isolated dispatch", assignee="patch")
        result = kb.dispatch_once(conn, failure_limit=2)

        assert result.spawned == []
        assert result.crashed == []
        events = kb.list_events(conn, task_id)
        kinds = [event.kind for event in events]
        assert "spawn_failed" in kinds
        assert "crashed" not in kinds
        failure = next(event for event in events if event.kind == "spawn_failed")
        assert "external controller's service cgroup" in failure.payload["error"]
        serialized = json.dumps(failure.payload)
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.claim_lock is None
        assert "claim-token" not in serialized
    finally:
        conn.close()


def test_dispatcher_claims_concurrent_tasks_with_distinct_scope_identities(
    monkeypatch, tmp_path
):
    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    conn = kb.connect()
    try:
        first = kb.create_task(conn, title="first worker", assignee="patch")
        second = kb.create_task(conn, title="second worker", assignee="patch")
        locks = [
            kb._dispatcher_claim_lock(conn, first),
            kb._dispatcher_claim_lock(conn, second),
        ]

        assert len(locks) == 2
        assert locks[0] != locks[1]
        assert all(lock.startswith(f"{kb._claimer_id()}:") for lock in locks)
        assert len({kb._kanban_worker_scope_unit(lock) for lock in locks}) == 2
    finally:
        conn.close()


def test_crash_reclaim_stops_surviving_worker_scope_descendants(
    monkeypatch, tmp_path
):
    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="crashed parent", assignee="patch")
        lock = kb._dispatcher_claim_lock(conn, task_id)
        claimed = kb.claim_task(conn, task_id, claimer=lock)
        assert claimed is not None
        kb._set_worker_pid(conn, task_id, 81234)
        conn.execute(
            "UPDATE tasks SET started_at = ? WHERE id = ?",
            (int(time.time()) - 30, task_id),
        )
        conn.commit()

        stopped = []
        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
        monkeypatch.setattr(kb, "_resolve_crash_grace_seconds", lambda: 0)
        monkeypatch.setattr(kb, "_classify_worker_exit", lambda _pid: ("unknown", None))
        monkeypatch.setattr(kb, "_systemd_worker_scope_required", lambda: True)
        monkeypatch.setattr(
            kb,
            "_stop_kanban_worker_scope",
            lambda claim: stopped.append(claim) or True,
        )

        assert kb.detect_crashed_workers(conn) == [task_id]
        assert stopped == [lock]

        events = kb.list_events(conn, task_id)
        crash = next(event for event in events if event.kind == "crashed")
        assert crash.run_id == claimed.current_run_id
        assert not any(event.kind == "worker_scope_cleanup" for event in events)
    finally:
        conn.close()


def test_crash_cleanup_failure_preserves_run_and_suppresses_retry_until_verified(
    monkeypatch, tmp_path
):
    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="dead wrapper live child", assignee="patch")
        lock = kb._dispatcher_claim_lock(conn, task_id)
        claimed = kb.claim_task(conn, task_id, claimer=lock)
        assert claimed is not None
        kb._set_worker_pid(
            conn,
            task_id,
            kb._SpawnedWorkerPid(
                82345,
                isolation_mode="systemd_scope",
                scope_unit=kb._kanban_worker_scope_unit(lock),
            ),
        )
        conn.execute(
            "UPDATE tasks SET started_at = ? WHERE id = ?",
            (int(time.time()) - 30, task_id),
        )
        conn.commit()

        state = {"scope_stopped": False}
        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
        monkeypatch.setattr(kb, "_resolve_crash_grace_seconds", lambda: 0)
        monkeypatch.setattr(kb, "_classify_worker_exit", lambda _pid: ("unknown", None))
        monkeypatch.setattr(
            kb,
            "_stop_kanban_worker_scope",
            lambda _lock: state["scope_stopped"],
        )

        assert kb.detect_crashed_workers(conn) == []
        held = kb.get_task(conn, task_id)
        assert held is not None
        assert held.status == "running"
        assert held.claim_lock == lock
        assert held.worker_pid == 82345
        assert held.current_run_id == claimed.current_run_id
        deferred = next(
            event for event in kb.list_events(conn, task_id)
            if event.kind == "reclaim_deferred"
        )
        assert deferred.run_id == claimed.current_run_id
        assert deferred.payload["reason"] == "crash_scope_cleanup_incomplete"
        assert deferred.payload["cleanup_verified"] is False

        spawns = []
        dispatch = kb.dispatch_once(
            conn,
            spawn_fn=lambda *_args, **_kwargs: spawns.append("duplicate") or 90001,
        )
        assert dispatch.spawned == []
        assert spawns == []

        state["scope_stopped"] = True
        assert kb.detect_crashed_workers(conn) == [task_id]
        released = kb.get_task(conn, task_id)
        assert released is not None
        assert released.status == "ready"
        assert released.claim_lock is None
        assert released.worker_pid is None
        assert released.current_run_id is None
    finally:
        conn.close()


def test_manual_reclaim_scope_failure_preserves_ownership(monkeypatch, tmp_path):
    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="reclaim exact scope", assignee="patch")
        lock = kb._dispatcher_claim_lock(conn, task_id)
        claimed = kb.claim_task(conn, task_id, claimer=lock)
        assert claimed is not None
        kb._set_worker_pid(
            conn,
            task_id,
            kb._SpawnedWorkerPid(
                83456,
                isolation_mode="systemd_scope",
                scope_unit=kb._kanban_worker_scope_unit(lock),
            ),
        )
        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
        monkeypatch.setattr(kb, "_stop_kanban_worker_scope", lambda _lock: False)

        assert kb.reclaim_task(conn, task_id, reason="operator stop") is False

        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "running"
        assert task.claim_lock == lock
        assert task.worker_pid == 83456
        assert task.current_run_id == claimed.current_run_id
        event = kb.list_events(conn, task_id)[-1]
        assert event.kind == "reclaim_deferred"
        assert event.run_id == claimed.current_run_id
        assert event.payload["reason"] == "manual_reclaim_cleanup_incomplete"
    finally:
        conn.close()


def test_archive_rejects_running_task_until_verified_reclaim(monkeypatch, tmp_path):
    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="archive running", assignee="patch")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        kb._set_worker_pid(conn, task_id, 84567)

        assert kb.archive_task(conn, task_id) is False
        running = kb.get_task(conn, task_id)
        assert running is not None
        assert running.status == "running"
        assert running.claim_lock == claimed.claim_lock
        assert running.worker_pid == 84567
        assert running.current_run_id == claimed.current_run_id
        assert not any(event.kind == "archived" for event in kb.list_events(conn, task_id))

        monkeypatch.setattr(
            kb,
            "_terminate_reclaimed_worker",
            lambda *_args, **_kwargs: {
                "prev_pid": 84567,
                "host_local": True,
                "termination_attempted": True,
                "terminated": True,
                "sigkill": False,
                "termination_target": "process_group",
                "scope_stop_attempted": False,
                "scope_stopped": False,
                "cleanup_verified": True,
            },
        )
        assert kb.reclaim_task(conn, task_id, reason="archive requested") is True
        assert kb.archive_task(conn, task_id) is True

        archived = kb.get_task(conn, task_id)
        assert archived is not None
        assert archived.status == "archived"
        assert archived.claim_lock is None
        assert archived.worker_pid is None
        assert archived.current_run_id is None
    finally:
        conn.close()


def test_delete_rejects_unverified_running_scope_cleanup(monkeypatch, tmp_path):
    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="delete scoped worker", assignee="patch")
        claim_lock = kb._dispatcher_claim_lock(conn, task_id)
        claimed = kb.claim_task(conn, task_id, claimer=claim_lock)
        assert claimed is not None
        kb._set_worker_pid(
            conn,
            task_id,
            kb._SpawnedWorkerPid(
                87890,
                isolation_mode="systemd_scope",
                scope_unit=kb._kanban_worker_scope_unit(claim_lock),
            ),
        )
        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
        monkeypatch.setattr(kb, "_stop_kanban_worker_scope", lambda _lock: False)

        assert kb.delete_task(conn, task_id) is False

        held = kb.get_task(conn, task_id)
        assert held is not None
        assert held.status == "running"
        assert held.claim_lock == claim_lock
        assert held.worker_pid == 87890
        assert held.current_run_id == claimed.current_run_id
        run = kb.latest_run(conn, task_id)
        assert run is not None
        assert run.id == claimed.current_run_id
        assert run.status == "running"
        assert run.ended_at is None
        deferred = kb.list_events(conn, task_id)[-1]
        assert deferred.kind == "reclaim_deferred"
        assert deferred.run_id == claimed.current_run_id
        assert deferred.payload is not None
        assert deferred.payload["reason"] == "delete_cleanup_incomplete"

        spawns = []
        dispatch = kb.dispatch_once(
            conn,
            spawn_fn=lambda *_args, **_kwargs: spawns.append("duplicate") or 90003,
        )
        assert dispatch.spawned == []
        assert spawns == []
    finally:
        conn.close()


def test_crash_cleanup_identity_swap_defers_replacement_run(monkeypatch, tmp_path):
    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="crash cleanup race", assignee="patch")
        old_lock = kb._dispatcher_claim_lock(conn, task_id)
        old_run = kb.claim_task(conn, task_id, claimer=old_lock)
        assert old_run is not None
        kb._set_worker_pid(
            conn,
            task_id,
            kb._SpawnedWorkerPid(
                88901,
                isolation_mode="systemd_scope",
                scope_unit=kb._kanban_worker_scope_unit(old_lock),
            ),
        )
        conn.execute(
            "UPDATE tasks SET started_at = ? WHERE id = ?",
            (int(time.time()) - 30, task_id),
        )
        conn.commit()

        replacement: dict[str, object] = {}

        def cleanup_then_replace(_pid, _claim_lock, **_kwargs):
            side = kb.connect()
            try:
                with kb.write_txn(side):
                    kb._end_run(
                        side,
                        task_id,
                        outcome="reclaimed",
                        status="reclaimed",
                        summary="concurrent replacement",
                    )
                    side.execute(
                        "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                        "claim_expires = NULL, worker_pid = NULL WHERE id = ?",
                        (task_id,),
                    )
                new_lock = kb._dispatcher_claim_lock(side, task_id)
                new_run = kb.claim_task(side, task_id, claimer=new_lock)
                assert new_run is not None
                kb._set_worker_pid(
                    side,
                    task_id,
                    kb._SpawnedWorkerPid(
                        88902,
                        isolation_mode="systemd_scope",
                        scope_unit=kb._kanban_worker_scope_unit(new_lock),
                    ),
                )
                replacement.update(lock=new_lock, run_id=new_run.current_run_id)
            finally:
                side.close()
            return {
                "prev_pid": 88901,
                "host_local": True,
                "termination_attempted": True,
                "terminated": True,
                "sigkill": False,
                "termination_target": "systemd_scope",
                "scope_stop_attempted": True,
                "scope_stopped": True,
                "scope_expected": True,
                "cleanup_verified": True,
            }

        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
        monkeypatch.setattr(kb, "_resolve_crash_grace_seconds", lambda: 0)
        monkeypatch.setattr(kb, "_terminate_reclaimed_worker", cleanup_then_replace)
        monkeypatch.setattr(kb, "_classify_worker_exit", lambda _pid: ("unknown", None))

        assert kb.detect_crashed_workers(conn) == []

        held = kb.get_task(conn, task_id)
        assert held is not None
        assert held.status == "running"
        assert held.claim_lock == replacement["lock"]
        assert held.worker_pid == 88902
        assert held.current_run_id == replacement["run_id"]
        replacement_run = kb.latest_run(conn, task_id)
        assert replacement_run is not None
        assert replacement_run.id == replacement["run_id"]
        assert replacement_run.status == "running"
        assert replacement_run.ended_at is None
        assert not any(
            event.kind == "crashed" and event.run_id == replacement["run_id"]
            for event in kb.list_events(conn, task_id)
        )
    finally:
        conn.close()


def test_dispatch_stops_exact_spawn_when_pid_persistence_cas_loses(
    monkeypatch, tmp_path,
):
    from hermes_cli import kanban_db as kb
    import hermes_cli.profiles as profiles

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="pid CAS loss",
            assignee="patch",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        stopped = []

        def cleanup_spawn(pid, claim_lock, *, scope_expected=None, **_kwargs):
            stopped.append((int(pid), claim_lock, scope_expected))
            return {
                "prev_pid": int(pid),
                "host_local": True,
                "termination_attempted": True,
                "terminated": True,
                "scope_stop_attempted": False,
                "scope_stopped": False,
                "cleanup_verified": True,
            }

        def spawn_then_lose_claim(claimed, _workspace):
            side = kb.connect()
            try:
                with kb.write_txn(side):
                    side.execute(
                        "UPDATE task_runs SET status = 'reclaimed', outcome = 'reclaimed', "
                        "ended_at = ?, claim_lock = NULL, claim_expires = NULL, "
                        "worker_pid = NULL WHERE id = ?",
                        (int(time.time()), claimed.current_run_id),
                    )
                    side.execute(
                        "UPDATE tasks SET status = 'done', completed_at = ?, "
                        "current_run_id = NULL, claim_lock = NULL, claim_expires = NULL, "
                        "worker_pid = NULL WHERE id = ?",
                        (int(time.time()), claimed.id),
                    )
            finally:
                side.close()
            return kb._SpawnedWorkerPid(
                89123,
                isolation_mode="process_session",
                scope_unit=None,
            )

        monkeypatch.setattr(kb, "_terminate_reclaimed_worker", cleanup_spawn)

        result = kb.dispatch_once(conn, spawn_fn=spawn_then_lose_claim)

        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "done"
        assert task.current_run_id is None
        assert task.worker_pid is None
        assert result.spawned == []
        assert len(stopped) == 1
        assert stopped[0][0] == 89123
        assert stopped[0][2] is False
        assert not any(
            event.kind == "spawned" for event in kb.list_events(conn, task_id)
        )
    finally:
        conn.close()


@pytest.mark.parametrize("operation", ["manual_reclaim", "reassign"])
def test_dispatch_pre_pid_manual_recovery_preserves_original_owner(
    monkeypatch, tmp_path, operation,
):
    from hermes_cli import kanban_db as kb
    import hermes_cli.profiles as profiles

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    monkeypatch.setattr(
        kb,
        "_terminate_reclaimed_worker",
        lambda pid, *_args, **_kwargs: {
            "prev_pid": pid,
            "host_local": True,
            "termination_attempted": pid is not None,
            "terminated": pid is not None,
            "scope_stop_attempted": False,
            "scope_stopped": False,
            "cleanup_verified": pid is not None,
        },
    )
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title=f"pre-pid {operation}",
            assignee="patch",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        attempted = {}

        def spawn_during_recovery(claimed, _workspace):
            side = kb.connect()
            try:
                if operation == "manual_reclaim":
                    attempted["result"] = kb.reclaim_task(
                        side, claimed.id, reason="operator raced spawn",
                    )
                else:
                    attempted["result"] = kb.reassign_task(
                        side,
                        claimed.id,
                        "gauge",
                        reclaim_first=True,
                        reason="operator raced spawn",
                    )
                held = kb.get_task(side, claimed.id)
                assert held is not None
                attempted["run_id"] = held.current_run_id
                attempted["claim_lock"] = held.claim_lock
                attempted["assignee"] = held.assignee
            finally:
                side.close()
            return 89201

        result = kb.dispatch_once(conn, spawn_fn=spawn_during_recovery)

        held = kb.get_task(conn, task_id)
        assert held is not None
        assert attempted == {
            "result": False,
            "run_id": held.current_run_id,
            "claim_lock": held.claim_lock,
            "assignee": "patch",
        }
        assert held.status == "running"
        assert held.worker_pid == 89201
        assert [item[0] for item in result.spawned] == [task_id]
        active_runs = conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ? "
            "AND status = 'running' AND ended_at IS NULL",
            (task_id,),
        ).fetchone()[0]
        assert active_runs == 1
    finally:
        conn.close()


def test_dispatch_pre_pid_stale_ttl_reclaim_preserves_original_owner(
    monkeypatch, tmp_path,
):
    from hermes_cli import kanban_db as kb
    import hermes_cli.profiles as profiles

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    monkeypatch.setattr(
        kb,
        "_terminate_reclaimed_worker",
        lambda pid, *_args, **_kwargs: {
            "prev_pid": pid,
            "host_local": True,
            "termination_attempted": pid is not None,
            "terminated": pid is not None,
            "scope_stop_attempted": False,
            "scope_stopped": False,
            "cleanup_verified": pid is not None,
        },
    )
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="pre-pid stale ttl",
            assignee="patch",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        attempted = {}

        def spawn_during_stale_reclaim(claimed, _workspace):
            side = kb.connect()
            try:
                with kb.write_txn(side):
                    expired = int(time.time()) - 1
                    side.execute(
                        "UPDATE tasks SET claim_expires = ? WHERE id = ?",
                        (expired, claimed.id),
                    )
                    side.execute(
                        "UPDATE task_runs SET claim_expires = ? WHERE id = ?",
                        (expired, claimed.current_run_id),
                    )
                attempted["reclaimed"] = kb.release_stale_claims(side)
                held = kb.get_task(side, claimed.id)
                assert held is not None
                attempted["run_id"] = held.current_run_id
                attempted["claim_lock"] = held.claim_lock
            finally:
                side.close()
            return 89202

        result = kb.dispatch_once(
            conn,
            spawn_fn=spawn_during_stale_reclaim,
        )

        held = kb.get_task(conn, task_id)
        assert held is not None
        assert attempted == {
            "reclaimed": 0,
            "run_id": held.current_run_id,
            "claim_lock": held.claim_lock,
        }
        assert held.status == "running"
        assert held.worker_pid == 89202
        assert [item[0] for item in result.spawned] == [task_id]
        active_runs = conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ? "
            "AND status = 'running' AND ended_at IS NULL",
            (task_id,),
        ).fetchone()[0]
        assert active_runs == 1
    finally:
        conn.close()


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_pid_cas_loss_cleanup_failure_does_not_mutate_successor(
    monkeypatch, tmp_path, lane,
):
    from hermes_cli import kanban_db as kb
    import hermes_cli.config as config
    import hermes_cli.profiles as profiles

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    monkeypatch.setattr(
        config,
        "load_config",
        lambda *_args, **_kwargs: {"kanban": {"review_dispatch": True}},
    )
    cleanup_calls = []

    def cleanup_cannot_verify(pid, claim_lock, *, scope_expected=None, **_kwargs):
        cleanup_calls.append((int(pid), claim_lock, scope_expected))
        return {
            "prev_pid": int(pid),
            "host_local": True,
            "termination_attempted": True,
            "terminated": False,
            "scope_stop_attempted": False,
            "scope_stopped": False,
            "cleanup_verified": False,
        }

    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", cleanup_cannot_verify)
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title=f"{lane} pid CAS cleanup failure",
            assignee="patch",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        if lane == "review":
            implementation = kb.claim_task(conn, task_id)
            assert implementation is not None
            assert kb.request_review(
                conn,
                task_id,
                summary="ready for exact-boundary review",
                expected_run_id=implementation.current_run_id,
            )
        original = {}
        replacement = {}

        def spawn_then_replace_owner(claimed, _workspace):
            original.update(
                run_id=claimed.current_run_id,
                claim_lock=claimed.claim_lock,
            )
            side = kb.connect()
            try:
                with kb.write_txn(side):
                    kb._end_run(
                        side,
                        claimed.id,
                        outcome="reclaimed",
                        status="reclaimed",
                        summary="concurrent owner replacement",
                    )
                    side.execute(
                        "UPDATE tasks SET status = ?, claim_lock = NULL, "
                        "claim_expires = NULL, worker_pid = NULL WHERE id = ?",
                        (lane, claimed.id),
                    )
                successor_lock = kb._dispatcher_claim_lock(side, claimed.id)
                if lane == "review":
                    successor = kb.claim_review_task(
                        side, claimed.id, claimer=successor_lock,
                    )
                else:
                    successor = kb.claim_task(
                        side, claimed.id, claimer=successor_lock,
                    )
                assert successor is not None
                kb._set_worker_pid(side, claimed.id, 89300)
                replacement.update(
                    run_id=successor.current_run_id,
                    claim_lock=successor_lock,
                )
            finally:
                side.close()
            return kb._SpawnedWorkerPid(
                89301,
                isolation_mode="process_session",
                scope_unit=None,
            )

        result = kb.dispatch_once(conn, spawn_fn=spawn_then_replace_owner)

        held = kb.get_task(conn, task_id)
        assert held is not None
        assert held.status == "running"
        assert held.current_run_id == replacement["run_id"]
        assert held.claim_lock == replacement["claim_lock"]
        assert held.worker_pid == 89300
        assert held.consecutive_failures == 0
        successor = kb.latest_run(conn, task_id)
        assert successor is not None
        assert successor.id == replacement["run_id"]
        assert successor.status == "running"
        assert successor.ended_at is None
        assert result.spawned == []
        assert cleanup_calls == [(89301, original["claim_lock"], False)]
        active_runs = conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ? "
            "AND status = 'running' AND ended_at IS NULL",
            (task_id,),
        ).fetchone()[0]
        assert active_runs == 1
        events = kb.list_events(conn, task_id)
        assert not any(event.kind == "spawn_failed" for event in events)
        assert not any(
            event.kind in {"spawned", "spawn_failed"} and event.run_id is None
            for event in events
        )
        cleanup_failed = [
            event for event in events if event.kind == "spawn_cleanup_failed"
        ]
        assert len(cleanup_failed) == 1
        assert cleanup_failed[0].run_id == original["run_id"]
        assert cleanup_failed[0].payload is not None
        assert cleanup_failed[0].payload["pid"] == 89301
    finally:
        conn.close()


@pytest.mark.linux_only
@pytest.mark.parametrize(
    ("load_state", "active_state", "expected"),
    [
        ("loaded", "active", False),
        ("loaded", "inactive", False),
        ("not-found", "inactive", True),
    ],
)
def test_worker_scope_is_reusable_only_after_systemd_unloads_it(
    monkeypatch, load_state, active_state, expected,
):
    from hermes_cli import kanban_db as kb

    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=f"LoadState={load_state}\nActiveState={active_state}\n",
            stderr="",
        ),
    )

    assert kb._systemd_worker_scope_unloaded("hermes-kanban-worker-test.scope") is expected


def test_implementation_to_review_waits_for_loaded_source_scope_then_claims(
    monkeypatch, tmp_path,
):
    from hermes_cli import kanban_db as kb
    import hermes_cli.config as config
    import hermes_cli.profiles as profiles

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    monkeypatch.setattr(
        config,
        "load_config",
        lambda *_args, **_kwargs: {"kanban": {"review_dispatch": True}},
    )
    scope_state = {"unloaded": False}
    probed_units = []
    monkeypatch.setattr(
        kb,
        "_systemd_worker_scope_unloaded",
        lambda unit: probed_units.append(unit) or scope_state["unloaded"],
        raising=False,
    )
    # The wrapper PID may already be gone while a descendant still keeps the
    # systemd scope loaded. The scope is the authoritative overlap boundary.
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)

    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="implementation handoff",
            assignee="patch",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        implementation = kb.claim_task(
            conn,
            task_id,
            claimer=kb._dispatcher_claim_lock(conn, task_id),
        )
        assert implementation is not None
        assert implementation.claim_lock is not None
        scope_unit = kb._kanban_worker_scope_unit(implementation.claim_lock)
        assert kb._set_worker_pid(
            conn,
            task_id,
            kb._SpawnedWorkerPid(
                90101,
                isolation_mode="systemd_scope",
                scope_unit=scope_unit,
            ),
        )
        assert kb.request_review(
            conn,
            task_id,
            summary="ready for review",
            reviewer="gauge",
            expected_run_id=implementation.current_run_id,
        )

        spawn_calls = []

        def spawn_review(task, _workspace):
            spawn_calls.append((task.id, task.current_run_id))
            return 90102

        deferred = kb.dispatch_once(conn, spawn_fn=spawn_review, failure_limit=1)

        parked = kb.get_task(conn, task_id)
        assert parked is not None
        assert parked.status == "review"
        assert parked.current_run_id is None
        assert parked.consecutive_failures == 0
        assert spawn_calls == []
        assert deferred.respawn_guarded == [(task_id, "prior_worker_teardown")]
        assert probed_units == [scope_unit]
        assert not any(
            event.kind in {"spawn_failed", "gave_up"}
            for event in kb.list_events(conn, task_id)
        )

        scope_state["unloaded"] = True
        claimed = kb.dispatch_once(conn, spawn_fn=spawn_review, failure_limit=1)

        running = kb.get_task(conn, task_id)
        assert running is not None
        assert running.status == "running"
        assert running.assignee == "gauge"
        assert running.current_run_id is not None
        assert running.worker_pid == 90102
        assert spawn_calls == [(task_id, running.current_run_id)]
        assert [item[0] for item in claimed.spawned] == [task_id]
    finally:
        conn.close()


def test_review_to_repair_waits_for_live_source_process_then_claims(
    monkeypatch, tmp_path,
):
    from hermes_cli import kanban_db as kb
    import hermes_cli.config as config
    import hermes_cli.profiles as profiles

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    monkeypatch.setattr(
        config,
        "load_config",
        lambda *_args, **_kwargs: {"kanban": {"review_dispatch": True}},
    )
    live_pids = {90202}
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid in live_pids)

    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="review repair handoff",
            assignee="patch",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        implementation = kb.claim_task(conn, task_id, claimer="patch:implementation")
        assert implementation is not None
        assert kb.request_review(
            conn,
            task_id,
            summary="ready for review",
            reviewer="gauge",
            expected_run_id=implementation.current_run_id,
        )
        review = kb.claim_review_task(conn, task_id, claimer="gauge:review")
        assert review is not None
        assert kb._set_worker_pid(
            conn,
            task_id,
            kb._SpawnedWorkerPid(
                90202,
                isolation_mode="process_session",
                scope_unit=None,
            ),
        )
        assert kb.request_changes(
            conn,
            task_id,
            reason="add the missing regression",
            expected_run_id=review.current_run_id,
        ) == (True, "patch")

        spawn_calls = []

        def spawn_repair(task, _workspace):
            spawn_calls.append((task.id, task.current_run_id))
            return 90203

        deferred = kb.dispatch_once(conn, spawn_fn=spawn_repair, failure_limit=1)

        parked = kb.get_task(conn, task_id)
        assert parked is not None
        assert parked.status == "ready"
        assert parked.current_run_id is None
        assert parked.consecutive_failures == 0
        assert spawn_calls == []
        assert deferred.respawn_guarded == [(task_id, "prior_worker_teardown")]
        assert not any(
            event.kind in {"spawn_failed", "gave_up"}
            for event in kb.list_events(conn, task_id)
        )

        live_pids.clear()
        claimed = kb.dispatch_once(conn, spawn_fn=spawn_repair, failure_limit=1)

        running = kb.get_task(conn, task_id)
        assert running is not None
        assert running.status == "running"
        assert running.assignee == "patch"
        assert running.current_run_id is not None
        assert running.worker_pid == 90203
        assert spawn_calls == [(task_id, running.current_run_id)]
        assert [item[0] for item in claimed.spawned] == [task_id]
    finally:
        conn.close()


def test_genuine_spawn_failure_still_trips_failure_budget(monkeypatch, tmp_path):
    from hermes_cli import kanban_db as kb
    import hermes_cli.profiles as profiles

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="genuine spawn failure",
            assignee="patch",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )

        def fail_spawn(*_args, **_kwargs):
            raise RuntimeError("genuine worker bootstrap failure")

        result = kb.dispatch_once(conn, spawn_fn=fail_spawn, failure_limit=1)

        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "blocked"
        assert task.consecutive_failures == 1
        assert result.auto_blocked == [task_id]
        assert result.respawn_guarded == []
        events = kb.list_events(conn, task_id)
        assert any(event.kind == "gave_up" for event in events)
        assert not any(
            event.kind == "respawn_guarded"
            and event.payload is not None
            and event.payload.get("reason") == "prior_worker_teardown"
            for event in events
        )
    finally:
        conn.close()
