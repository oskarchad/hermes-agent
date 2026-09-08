"""Explicit continuation of a parent-ready card does not fabricate a review phase."""

import argparse
import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli.kanban_db_promotion import promote_task


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(dispatch, "_profile_exists_fn", lambda: lambda _: True)
    with kbc.connect(home / "kanban.db") as conn:
        yield conn


def test_first_dispatch_requires_explicit_continuation_once(board, tmp_path, monkeypatch):
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    parent = kb.create_task(board, title="implementation", assignee="builder")
    tid = kb.create_task(board, title="independent check", assignee="inspector", parents=[parent],
                         workspace_kind="dir", workspace_path=str(tmp_path))
    kb.add_comment(board, tid, "builder", "Final https://github.com/example/project/pull/1")
    assert kb.complete_task(board, parent)
    kb.recompute_ready(board)
    before = dict(board.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())
    assert before["status"] == "ready"
    assert kb.latest_run(board, tid) is None
    spawned = []

    def spawn(task, workspace):
        spawned.append((task.id, task.assignee, workspace))
        return None

    assert (tid, "active_pr") in dispatch.dispatch_once(board, spawn_fn=spawn).respawn_guarded
    now += 2
    args = argparse.Namespace(task_id=tid, ids=None, reason=["Continue existing checkpoint"],
                              force=False, dry_run=True, json=True)
    assert cli._cmd_promote(args) == 0
    assert dispatch.check_respawn_guard(board, tid) == "active_pr"
    args.dry_run = False
    assert cli._cmd_promote(args) == 0
    after = dict(board.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())
    assert after == before  # No status, ownership, retry budget or provenance rewrite.
    event = board.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = 'promoted_manual' ORDER BY id DESC", (tid,)).fetchone()
    assert json.loads(event[0])["source_status"] == "ready"
    dispatch.dispatch_once(board, spawn_fn=spawn)
    assert spawned == [(tid, "inspector", str(tmp_path))]
    assert kb.latest_run(board, tid) is not None
    assert dispatch.check_respawn_guard(board, tid) == "active_pr"
    assert not promote_task(board, tid, actor="operator", reason="repeat")[0]
    # Automatic retry cannot reuse permission consumed by the first claim.
    assert kb.block_task(board, tid, reason="waiting", kind="dependency")
    kb.recompute_ready(board)
    dispatch.dispatch_once(board, spawn_fn=spawn)
    assert len(spawned) == 1


@pytest.mark.parametrize("gate", ["actor", "reason", "parent", "claim", "run", "open_run", "pid", "teardown", "auth", "cooldown"])
def test_ready_continuation_keeps_safety_gates(board, tmp_path, monkeypatch, gate):
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    tid = kb.create_task(board, title="checkpoint", assignee="inspector",
                         workspace_kind="dir", workspace_path=str(tmp_path))
    kb.add_comment(board, tid, "builder", "https://github.com/example/project/pull/1")
    now += 2
    actor, reason = "operator", "Continue checkpoint"
    if gate == "actor":
        actor = " "
    elif gate == "reason":
        reason = " "
    elif gate == "parent":
        parent = kb.create_task(board, title="unfinished")
        kb.link_tasks(board, parent, tid)
        # Legacy/stale ready rows must not let force mint continuation authority.
        with kb.write_txn(board):
            board.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
    elif gate == "open_run":
        with kb.write_txn(board):
            board.execute("INSERT INTO task_runs (task_id, profile, status, started_at) VALUES (?, 'inspector', 'running', ?)", (tid, now))
    elif gate in {"claim", "run", "pid", "auth", "cooldown"}:
        column, value = {"claim": ("claim_lock", "owner"), "run": ("current_run_id", 42),
                         "pid": ("worker_pid", 42), "auth": ("last_failure_error", "unauthorized"),
                         "cooldown": ("last_failure_error", "quota exceeded")}[gate]
        with kb.write_txn(board):
            board.execute(f"UPDATE tasks SET {column} = ? WHERE id = ?", (value, tid))
            if gate == "cooldown":
                board.execute("INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at) VALUES (?, 'inspector', 'ready', 'rate_limited', ?, ?)", (tid, now, now))
    else:
        monkeypatch.setattr(dispatch, "_handoff_worker_teardown_pending", lambda *_: True)
    before = dict(board.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())
    result = promote_task(board, tid, actor=actor, reason=reason, force=gate == "parent")
    if gate in {"auth", "cooldown"}:
        assert result[0]
        expected = "blocker_auth" if gate == "auth" else "rate_limit_cooldown"
        assert dispatch.check_respawn_guard(board, tid) == expected
    else:
        assert not result[0]
        assert board.execute("SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'promoted_manual'", (tid,)).fetchone() is None
    assert dict(board.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()) == before
