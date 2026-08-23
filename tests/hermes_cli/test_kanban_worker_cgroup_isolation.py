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
