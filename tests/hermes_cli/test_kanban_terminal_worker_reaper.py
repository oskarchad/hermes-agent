"""Invariant: a worker that outlives its own terminal transition is still
reachable and gets reaped by the dispatcher (issue #111791).

``complete_task`` wipes ``tasks.worker_pid`` and the ``running``-only reclaim
sweeps never look at a ``done`` card again, so a worker that called
``kanban_complete`` and then hung (holding deleted ``state.db`` sidecar fds)
was invisible to every command. The closed ``task_runs`` row now keeps the pid
and its spawn fingerprint, and ``reap_terminal_workers`` ends such a worker on
the next tick — never a recycled PID, never a legacy row without a fingerprint.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    with kbc.connect() as c:
        yield c


def _sleeper():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(0.2)
    return proc


def _completed_card_with_worker(conn, proc, *, ended_ago: int = 600) -> tuple[str, int]:
    tid = kb.create_task(conn, title="finished", assignee="coder")
    kb.claim_task(conn, tid, claimer=kb._claimer_id())
    run_id = kb._current_run_id(conn, tid)
    kbd._set_worker_pid(conn, tid, proc.pid)
    assert kb.complete_task(conn, tid, result="done", expected_run_id=run_id) is True
    # Default: the run closed long enough ago that the reaper's grace window has passed.
    conn.execute("UPDATE task_runs SET ended_at = ended_at - ? WHERE id=?", (ended_ago, run_id))
    return tid, run_id


def test_worker_alive_after_completion_is_reaped_on_dispatch_tick(conn):
    proc = _sleeper()
    try:
        tid, run_id = _completed_card_with_worker(conn, proc)
        # The evidence the running-only sweeps lost lives on the closed run row.
        run = conn.execute("SELECT worker_pid, worker_started_at, ended_at FROM task_runs WHERE id=?", (run_id,)).fetchone()
        assert run["ended_at"] is not None and run["worker_pid"] == proc.pid and run["worker_started_at"] is not None

        result = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 0, dry_run=True, max_spawn=0)

        assert result.reaped_terminal_workers == [tid]
        assert proc.wait(timeout=10) is not None
        run = conn.execute("SELECT worker_pid, worker_started_at FROM task_runs WHERE id=?", (run_id,)).fetchone()
        assert run["worker_pid"] is None and run["worker_started_at"] is None
        kinds = [r["kind"] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=?", (tid,))]
        assert "terminal_worker_reaped" in kinds
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_recycled_pid_and_legacy_row_are_never_signalled(conn):
    stranger = _sleeper()
    legacy = _sleeper()
    try:
        _tid, run_id = _completed_card_with_worker(conn, stranger)
        # PID reuse: the recorded fingerprint belongs to a process that no longer exists.
        conn.execute("UPDATE task_runs SET worker_started_at = worker_started_at - 1000000 WHERE id=?", (run_id,))
        _, legacy_run = _completed_card_with_worker(conn, legacy)
        conn.execute("UPDATE task_runs SET worker_started_at = NULL WHERE id=?", (legacy_run,))

        assert kbd.reap_terminal_workers(conn) == []

        assert stranger.poll() is None and legacy.poll() is None
        # Stale evidence is dropped so the row is not rescanned; the legacy row is left alone.
        assert conn.execute("SELECT worker_pid FROM task_runs WHERE id=?", (run_id,)).fetchone()["worker_pid"] is None
        assert conn.execute("SELECT worker_pid FROM task_runs WHERE id=?", (legacy_run,)).fetchone()["worker_pid"] == legacy.pid
    finally:
        for p in (stranger, legacy):
            p.kill()
            p.wait()


def test_fresh_terminal_run_is_left_alone_until_grace_passes(conn):
    """A worker is still alive for a moment after its own kanban_complete returns
    (final turn, session persistence): a just-closed run is not signalled."""
    proc = _sleeper()
    signals = []

    def signal_fn(pid, sig):
        signals.append((pid, sig))
        os.kill(pid, sig)

    try:
        tid, run_id = _completed_card_with_worker(conn, proc, ended_ago=0)

        assert kbd.reap_terminal_workers(conn, signal_fn=signal_fn) == []

        assert signals == [] and proc.poll() is None
        run = conn.execute("SELECT worker_pid FROM task_runs WHERE id=?", (run_id,)).fetchone()
        assert run["worker_pid"] == proc.pid  # evidence kept for a later tick
        conn.execute(
            "UPDATE task_runs SET ended_at = ended_at - ? WHERE id=?",
            (kbd.TERMINAL_WORKER_REAP_GRACE_SECONDS, run_id),
        )
        assert kbd.reap_terminal_workers(conn, signal_fn=signal_fn) == [tid]
        assert [pid for pid, _ in signals] == [proc.pid]
    finally:
        proc.kill()
        proc.wait()


class _FakeScopes:
    """Loaded transient scopes; ``stop`` unloads exactly the named unit."""

    def __init__(self, monkeypatch, *loaded):
        self.loaded, self.stopped = set(loaded), []
        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)  # main worker pid already exited
        monkeypatch.setattr(kb, "_stop_kanban_worker_scope", self.stop)
        monkeypatch.setattr(kb, "_systemd_worker_scope_inactive", lambda u: u not in self.loaded)
        monkeypatch.setattr(kb, "_systemd_worker_scope_unloaded", lambda u: u not in self.loaded)

    def stop(self, unit):
        self.stopped.append(unit)
        self.loaded.discard(unit)
        return True


def _scoped_run(conn, tid, pid=999_001):
    run_id = kb._current_run_id(conn, tid)
    unit = kb._kanban_worker_scope_unit(tid, run_id)
    assert kbd._set_worker_pid(conn, tid, kbd._SpawnedWorkerPid(
        pid, isolation_mode="systemd_scope", scope_unit=unit, started_at="boot|1"))
    return run_id, unit


def _age_run(conn, run_id, seconds=600):
    conn.execute("UPDATE task_runs SET ended_at = ended_at - ? WHERE id=?", (seconds, run_id))


@pytest.mark.parametrize("handoff", ["completed", "review_requested"])
def test_finished_run_scope_with_live_descendant_is_stopped(conn, monkeypatch, handoff):
    """t_64f4ab8e/t_a0138b28: main pid exited, a background child (http.server)
    kept the run's scope loaded forever and pinned the review on prior_worker_teardown."""
    tid = kb.create_task(conn, title="finished", assignee="coder")
    kb.claim_task(conn, tid, claimer=kb._claimer_id())
    run_id, unit = _scoped_run(conn, tid)
    scopes = _FakeScopes(monkeypatch, unit)
    if handoff == "completed":
        assert kb.complete_task(conn, tid, result="done", expected_run_id=run_id)
    else:
        assert kb.request_review(conn, tid, summary="ready", reviewer="gauge", expected_run_id=run_id)
    _age_run(conn, run_id)

    assert kbd.reap_terminal_workers(conn) == [tid]

    assert scopes.stopped == [unit]
    run = conn.execute("SELECT worker_pid FROM task_runs WHERE id=?", (run_id,)).fetchone()
    assert run["worker_pid"] is None
    assert any(e.kind == "terminal_worker_reaped" and e.payload.get("scope_unit") == unit
               for e in kb.list_events(conn, tid))
    assert kbd._handoff_worker_teardown_pending(conn, tid) is False
    assert kbd.reap_terminal_workers(conn) == [] and scopes.stopped == [unit]  # not rescanned


def test_review_handoff_spawns_after_dispatcher_stops_leftover_scope(conn, monkeypatch):
    from hermes_cli import config, profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    monkeypatch.setattr(config, "load_config", lambda *_a, **_k: {"kanban": {"review_dispatch": True}})
    tid = kb.create_task(conn, title="handoff", assignee="coder")
    kb.claim_task(conn, tid, claimer=kb._dispatcher_claim_lock(conn, tid))
    run_id, unit = _scoped_run(conn, tid)
    scopes = _FakeScopes(monkeypatch, unit)
    assert kb.request_review(conn, tid, summary="ready", reviewer="gauge", expected_run_id=run_id)
    _age_run(conn, run_id)
    spawned = []

    result = kbd.dispatch_once(conn, spawn_fn=lambda task, _ws: spawned.append(task.id) or 999_002,
                               failure_limit=1)

    assert scopes.stopped == [unit]
    assert result.respawn_guarded == [] and spawned == [tid]
    assert kb.get_task(conn, tid).assignee == "gauge"


def test_scope_stop_spares_live_runs_foreign_cards_and_grace_window(conn, monkeypatch):
    # Same card: run 1 finished long ago, run 2 is the live current run.
    tid = kb.create_task(conn, title="retried", assignee="coder")
    kb.claim_task(conn, tid, claimer=kb._claimer_id())
    old_run, old_unit = _scoped_run(conn, tid)
    assert kb.request_review(conn, tid, summary="ready", reviewer="gauge", expected_run_id=old_run)
    _age_run(conn, old_run)
    kb.claim_review_task(conn, tid, claimer=kb._claimer_id())
    live_run, live_unit = _scoped_run(conn, tid, pid=999_003)
    assert live_run != old_run
    # Another card, still running; a third that finished inside the grace window.
    other = kb.create_task(conn, title="other", assignee="coder")
    kb.claim_task(conn, other, claimer=kb._claimer_id())
    _, other_unit = _scoped_run(conn, other, pid=999_004)
    fresh = kb.create_task(conn, title="fresh", assignee="coder")
    kb.claim_task(conn, fresh, claimer=kb._claimer_id())
    fresh_run, fresh_unit = _scoped_run(conn, fresh, pid=999_005)
    assert kb.complete_task(conn, fresh, result="done", expected_run_id=fresh_run)
    scopes = _FakeScopes(monkeypatch, old_unit, live_unit, other_unit, fresh_unit)

    assert kbd.reap_terminal_workers(conn) == [tid]

    assert scopes.stopped == [old_unit]
    assert scopes.loaded == {live_unit, other_unit, fresh_unit}
    assert conn.execute("SELECT worker_pid FROM task_runs WHERE id=?", (fresh_run,)).fetchone()[0] == 999_005


def test_failed_scope_stop_keeps_evidence_for_next_tick(conn, monkeypatch):
    tid = kb.create_task(conn, title="stuck", assignee="coder")
    kb.claim_task(conn, tid, claimer=kb._claimer_id())
    run_id, unit = _scoped_run(conn, tid)
    scopes = _FakeScopes(monkeypatch, unit)
    monkeypatch.setattr(kb, "_stop_kanban_worker_scope", lambda u: scopes.stopped.append(u) and False)
    assert kb.complete_task(conn, tid, result="done", expected_run_id=run_id)
    _age_run(conn, run_id)

    assert kbd.reap_terminal_workers(conn) == []

    assert scopes.stopped == [unit]
    assert conn.execute("SELECT worker_pid FROM task_runs WHERE id=?", (run_id,)).fetchone()[0] == 999_001


def test_one_failing_row_does_not_abort_the_sweep(conn):
    """A signal failure on one run is logged and skipped; the other rows are still reaped."""
    broken, healthy = _sleeper(), _sleeper()
    try:
        _completed_card_with_worker(conn, broken)
        tid, _ = _completed_card_with_worker(conn, healthy)

        def signal_fn(pid, sig):
            if pid == broken.pid:
                raise RuntimeError("boom")
            healthy.kill()

        assert kbd.reap_terminal_workers(conn, signal_fn=signal_fn) == [tid]
    finally:
        for p in (broken, healthy):
            p.kill()
            p.wait()
