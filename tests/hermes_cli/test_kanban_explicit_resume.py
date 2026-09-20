"""An explicit continuation consumes one PR-checkpoint retry, not a blanket waiver."""

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_decompose as decomp


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with kbc.connect(home / "kanban.db") as conn:
        yield conn


@pytest.mark.parametrize("resume", ["specified", "unblocked", "promoted_manual", "review_reopened", "changes_requested"])
def test_checkpoint_continuation_preserves_identity_gates_and_is_consumed(board, tmp_path, monkeypatch, resume):
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    parent = kb.create_task(board, title="upstream", assignee="builder")
    tid = kb.create_task(
        board, title="continue checkpoint", assignee="builder",
        workspace_kind="worktree", workspace_path=str(tmp_path),
        branch_name="fix/existing-checkpoint", parents=[parent],
        triage=resume == "specified",
        initial_status="blocked" if resume == "unblocked" else "running",
    )
    comment_id = kb.add_comment(board, tid, "builder", "Checkpoint https://github.com/example/project/pull/1")
    assert kbd.check_respawn_guard(board, tid) == "active_pr"
    now += 2
    if resume == "specified":
        assert kb.specify_triage_task(board, tid, author="operator")
    elif resume == "unblocked":
        assert kb.unblock_task(board, tid)
    elif resume == "promoted_manual":
        # Unlink the undone parent and move to blocked so promotion legitimately succeeds on 20f7ef4df5
        assert kb.unlink_tasks(board, parent, tid)
        assert kb.block_task(board, tid, reason="manual pause", kind="needs_input")
        assert kb.promote_task(board, tid, actor="operator")[0]
        # Re-link parent to assert that claim still gates on it
        kb.link_tasks(board, parent, tid)
    else:
        assert kb.complete_task(board, parent)
        kb.recompute_ready(board)
        assert kb.request_review(board, tid, summary="checkpoint", reviewer="reviewer")
        if resume == "review_reopened":
            assert kb.reopen_review_task(board, tid)
        else:
            assert kb.claim_review_task(board, tid)
            assert kb.request_changes(board, tid, reason="finish checkpoint")[0]
        parent = kb.create_task(board, title="new prerequisite", assignee="builder")
        kb.link_tasks(board, parent, tid)
    assert kbd.check_respawn_guard(board, tid) is None
    assert kb.claim_task(board, tid) is None
    assert kb.complete_task(board, parent)
    kb.recompute_ready(board)
    assert kbd.check_respawn_guard(board, tid) is None
    claimed = kb.claim_task(board, tid)
    assert claimed is not None
    assert (claimed.id, claimed.assignee, claimed.workspace_path, claimed.branch_name) == (
        tid, "builder", str(tmp_path), "fix/existing-checkpoint",
    )
    assert kb.claim_task(board, tid) is None
    assert board.execute("SELECT body FROM task_comments WHERE id = ?", (comment_id,)).fetchone()[0].endswith("/pull/1")
    # Automatic dependency re-promotion after that claim must not mint permission.
    assert kb.block_task(board, tid, reason="wait", kind="dependency")
    kb.recompute_ready(board)
    assert kbd.check_respawn_guard(board, tid) == "active_pr"


@pytest.mark.parametrize("case", ["prose", "promoted", "reclaimed", "status", "same_second", "new_checkpoint", "auth", "cooldown", "teardown"])
def test_checkpoint_guard_needs_later_explicit_authority_and_keeps_safety_gates(board, monkeypatch, case):
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    tid = kb.create_task(board, title="checkpoint", assignee="builder", triage=True)
    kb.add_comment(board, tid, "builder", "https://github.com/example/project/pull/1")
    if case != "same_second":
        now += 2
    if case in {"prose", "promoted", "reclaimed", "status"}:
        if case == "prose":
            kb.add_comment(board, tid, "operator", "RESUME this work now")
        else:
            with kb.write_txn(board):
                kb._append_event(board, tid, case, {"status": "todo", "reason": "ancestor_reopened"})
        expected = "active_pr"
    else:
        assert kb.specify_triage_task(board, tid, author="operator")
        expected = "active_pr"
        if case == "new_checkpoint":
            now += 2
            kb.add_comment(board, tid, "builder", "New checkpoint https://github.com/example/project/pull/1")
        elif case in {"auth", "cooldown"}:
            with kb.write_txn(board):
                board.execute("UPDATE tasks SET last_failure_error = 'unauthorized' WHERE id = ?", (tid,))
                if case == "cooldown":
                    board.execute(
                        "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at) VALUES (?, 'builder', 'ready', 'rate_limited', ?, ?)",
                        (tid, now, now),
                    )
            expected = "blocker_auth" if case == "auth" else "rate_limit_cooldown"
        elif case == "teardown":
            monkeypatch.setattr(kbd, "_handoff_worker_teardown_pending", lambda *_: True)
            expected = "prior_worker_teardown"
    assert kbd.check_respawn_guard(board, tid) == expected


@pytest.mark.parametrize("author", ["auto-decomposer", "operator", None, "   "])
def test_single_task_specification_requires_explicit_provenance(board, tmp_path, monkeypatch, author):
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / ".hermes" / "kanban.db"))
    tid = kb.create_task(
        board, title="existing checkpoint", assignee="builder", triage=True,
        workspace_kind="worktree", workspace_path=str(tmp_path),
        branch_name="fix/existing-checkpoint",
    )
    comment_id = kb.add_comment(board, tid, "builder", "https://github.com/example/project/pull/1")
    now += 2
    routing = decomp._Routing("operator", "builder", True, [], {"operator", "builder"})
    # Real fanout=false persistence, including its separate connection and promotion.
    task = kb.get_task(board, tid)
    assert task is not None
    outcome = decomp._apply_single(
        task, {"body": "Finish the existing PR"}, routing, author,
    )
    assert outcome.ok
    task = kb.get_task(board, tid)
    assert task is not None
    assert (task.status, task.assignee, task.workspace_path, task.branch_name) == (
        "ready", "builder", str(tmp_path), "fix/existing-checkpoint",
    )
    if author != "operator":
        assert kbd.check_respawn_guard(board, tid) == "active_pr"
        # A subsequent explicit lifecycle decision resumes this same task.
        now += 2
        assert kb.block_task(board, tid, reason="confirm checkpoint continuation", kind="needs_input")
        assert kb.promote_task(board, tid, actor="operator")[0]
    assert kbd.check_respawn_guard(board, tid) is None
    claimed = kb.claim_task(board, tid)
    assert claimed is not None and claimed.id == tid
    assert kbd.check_respawn_guard(board, tid) == "active_pr"
    assert board.execute("SELECT body FROM task_comments WHERE id = ?", (comment_id,)).fetchone()[0].endswith("/pull/1")


@pytest.mark.parametrize("payload", [None, {"changed_fields": ["body"]}])
def test_legacy_specification_cannot_prove_explicit_continuation(board, monkeypatch, payload):
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    tid = kb.create_task(board, title="legacy checkpoint", assignee="builder", triage=True)
    kb.add_comment(board, tid, "builder", "https://github.com/example/project/pull/1")
    now += 2
    # Retained pre-provenance event shapes are indistinguishable from automation.
    with kb.write_txn(board):
        kb._append_event(board, tid, "specified", payload)
    assert kbd.check_respawn_guard(board, tid) == "active_pr"
    assert kb.specify_triage_task(board, tid, author="operator")
    assert kbd.check_respawn_guard(board, tid) is None
