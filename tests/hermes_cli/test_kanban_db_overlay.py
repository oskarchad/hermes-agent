"""Preserved Kanban overlay lifecycle and mutation contracts."""
from __future__ import annotations
import contextlib
import json
import os
import sqlite3
import time
from pathlib import Path
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from tests.hermes_cli import test_kanban_db as native
kanban_home = native.kanban_home

def test_delete_task_and_archived_task_clean_legacy_governance_foreign_keys(kanban_home):
    """F1: Legacy governance tables reference tasks(id) and task_runs(id) without
    ON DELETE CASCADE. delete_task and delete_archived_task must clean those
    dependent rows atomically without failing with sqlite3.IntegrityError, while
    preserving unrelated governance rows."""
    with kbc.connect() as conn:
        # Create legacy governance schema
        conn.execute(
            "CREATE TABLE IF NOT EXISTS kanban_workflows ("
            "workflow_id TEXT PRIMARY KEY, intake_kind TEXT NOT NULL, contract TEXT, "
            "decision_id TEXT NOT NULL, lineage_id TEXT NOT NULL, evidence_mode TEXT NOT NULL, "
            "state TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, scope_json TEXT NOT NULL, "
            "intake_sha256 TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS kanban_task_bindings ("
            "task_id TEXT PRIMARY KEY REFERENCES tasks(id), "
            "workflow_id TEXT NOT NULL REFERENCES kanban_workflows(workflow_id), "
            "binding_json TEXT NOT NULL, source_action TEXT, issued_revision INTEGER NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS kanban_dispositions ("
            "action_key TEXT PRIMARY KEY, "
            "workflow_id TEXT NOT NULL REFERENCES kanban_workflows(workflow_id), "
            "kind TEXT NOT NULL, target_key TEXT NOT NULL, payload_json TEXT NOT NULL, "
            "task_id TEXT REFERENCES tasks(id), "
            "state TEXT NOT NULL, UNIQUE(workflow_id, kind, target_key))"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS kanban_invocations ("
            "invocation_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), "
            "run_id INTEGER NOT NULL REFERENCES task_runs(id), "
            "workflow_id TEXT NOT NULL REFERENCES kanban_workflows(workflow_id), "
            "binding_json TEXT NOT NULL, profile TEXT NOT NULL, claim_lock TEXT NOT NULL, "
            "evidence_mode TEXT NOT NULL, request_sha256 TEXT NOT NULL, artifacts_json TEXT NOT NULL, "
            "response_sha256 TEXT, runner_version TEXT NOT NULL, verdict TEXT, "
            "state TEXT NOT NULL, UNIQUE(task_id, run_id))"
        )
        conn.execute(
            "INSERT INTO kanban_workflows VALUES ('wf1', 'k', 'c', 'd', 'l', 'real', 'active', 1, '{}', 'sha')"
        )
        conn.execute(
            "INSERT INTO kanban_workflows VALUES ('wf_other', 'k', 'c', 'd', 'l', 'real', 'active', 1, '{}', 'sha')"
        )

        # 1. Test delete_task on ready task with legacy governance rows
        t1 = kb.create_task(conn, title="task-1", assignee="alice")
        conn.execute("INSERT INTO task_runs (task_id, status, started_at) VALUES (?, 'done', 100)", (t1,))
        run1 = conn.execute("SELECT id FROM task_runs WHERE task_id = ?", (t1,)).fetchone()[0]

        # Populate legacy governance referencing t1
        conn.execute("INSERT INTO kanban_task_bindings VALUES (?, 'wf1', '{}', 'act', 1)", (t1,))
        conn.execute("INSERT INTO kanban_dispositions VALUES ('act1', 'wf1', 'kind1', 'tgt1', '{}', ?, 'pending')", (t1,))
        conn.execute("INSERT INTO kanban_invocations VALUES ('inv1', ?, ?, 'wf1', '{}', 'prof', 'lock', 'real', 'req', 'art', 'resp', 'v1', 'PASS', 'finished')", (t1, run1))

        # Populate unrelated task and governance rows as preservation controls
        t_unrelated = kb.create_task(conn, title="unrelated", assignee="bob")
        conn.execute("INSERT INTO task_runs (task_id, status, started_at) VALUES (?, 'done', 100)", (t_unrelated,))
        run_unrelated = conn.execute("SELECT id FROM task_runs WHERE task_id = ?", (t_unrelated,)).fetchone()[0]
        conn.execute("INSERT INTO kanban_task_bindings VALUES (?, 'wf_other', '{}', 'act', 1)", (t_unrelated,))
        conn.execute("INSERT INTO kanban_dispositions VALUES ('act_unrelated', 'wf_other', 'kind1', 'tgt2', '{}', ?, 'pending')", (t_unrelated,))
        conn.execute("INSERT INTO kanban_invocations VALUES ('inv_unrelated', ?, ?, 'wf_other', '{}', 'prof', 'lock', 'real', 'req', 'art', 'resp', 'v1', 'PASS', 'finished')", (t_unrelated, run_unrelated))
        # Disposition with NULL task_id
        conn.execute("INSERT INTO kanban_dispositions VALUES ('act_null_task', 'wf_other', 'kind2', 'tgt3', '{}', NULL, 'pending')")
        conn.commit()

        # Delete t1
        assert kb.delete_task(conn, t1) is True
        assert kb.get_task(conn, t1) is None

        # Verify t1 governance rows were deleted
        assert conn.execute("SELECT COUNT(*) FROM kanban_task_bindings WHERE task_id = ?", (t1,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM kanban_dispositions WHERE task_id = ?", (t1,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM kanban_invocations WHERE task_id = ?", (t1,)).fetchone()[0] == 0

        # Verify unrelated governance rows remain intact
        assert conn.execute("SELECT COUNT(*) FROM kanban_task_bindings WHERE task_id = ?", (t_unrelated,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM kanban_dispositions WHERE task_id = ?", (t_unrelated,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM kanban_invocations WHERE task_id = ?", (t_unrelated,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM kanban_dispositions WHERE action_key = 'act_null_task'").fetchone()[0] == 1

        # 2. Test delete_archived_task on archived task with legacy governance rows
        t2 = kb.create_task(conn, title="task-2", assignee="alice")
        conn.execute("INSERT INTO task_runs (task_id, status, started_at) VALUES (?, 'done', 100)", (t2,))
        run2 = conn.execute("SELECT id FROM task_runs WHERE task_id = ?", (t2,)).fetchone()[0]
        kb.complete_task(conn, t2, result="done")
        assert kb.archive_task(conn, t2) is True

        conn.execute("INSERT INTO kanban_task_bindings VALUES (?, 'wf1', '{}', 'act', 1)", (t2,))
        conn.execute("INSERT INTO kanban_dispositions VALUES ('act2', 'wf1', 'kind1', 'tgt4', '{}', ?, 'applied')", (t2,))
        conn.execute("INSERT INTO kanban_invocations VALUES ('inv2', ?, ?, 'wf1', '{}', 'prof', 'lock', 'real', 'req', 'art', 'resp', 'v1', 'PASS', 'finished')", (t2, run2))
        conn.commit()

        assert kb.delete_archived_task(conn, t2) is True
        assert kb.get_task(conn, t2) is None
        assert conn.execute("SELECT COUNT(*) FROM kanban_task_bindings WHERE task_id = ?", (t2,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM kanban_dispositions WHERE task_id = ?", (t2,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM kanban_invocations WHERE task_id = ?", (t2,)).fetchone()[0] == 0

        # Unrelated rows still remain
        assert conn.execute("SELECT COUNT(*) FROM kanban_task_bindings WHERE task_id = ?", (t_unrelated,)).fetchone()[0] == 1


@pytest.mark.parametrize("mismatch_kind", ["status", "running_worker"])
def test_delete_task_cas_refusal_preserves_all_relations_and_task(kanban_home, mismatch_kind, monkeypatch):
    """F1-CAS-HIGH: delete_task must validate CAS under write lock BEFORE relation cleanup.
    If status or running worker identity mismatches, delete_task must return False,
    the task must survive, and all core and legacy relations must remain completely intact."""
    with kbc.connect() as conn1:
        # Create legacy governance schema
        conn1.execute(
            "CREATE TABLE IF NOT EXISTS kanban_workflows ("
            "workflow_id TEXT PRIMARY KEY, intake_kind TEXT NOT NULL, contract TEXT, "
            "decision_id TEXT NOT NULL, lineage_id TEXT NOT NULL, evidence_mode TEXT NOT NULL, "
            "state TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, scope_json TEXT NOT NULL, "
            "intake_sha256 TEXT NOT NULL)"
        )
        conn1.execute(
            "CREATE TABLE IF NOT EXISTS kanban_task_bindings ("
            "task_id TEXT PRIMARY KEY REFERENCES tasks(id), "
            "workflow_id TEXT NOT NULL REFERENCES kanban_workflows(workflow_id), "
            "binding_json TEXT NOT NULL, source_action TEXT, issued_revision INTEGER NOT NULL)"
        )
        conn1.execute(
            "CREATE TABLE IF NOT EXISTS kanban_dispositions ("
            "action_key TEXT PRIMARY KEY, "
            "workflow_id TEXT NOT NULL REFERENCES kanban_workflows(workflow_id), "
            "kind TEXT NOT NULL, target_key TEXT NOT NULL, payload_json TEXT NOT NULL, "
            "task_id TEXT REFERENCES tasks(id), "
            "state TEXT NOT NULL, UNIQUE(workflow_id, kind, target_key))"
        )
        conn1.execute(
            "CREATE TABLE IF NOT EXISTS kanban_invocations ("
            "invocation_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), "
            "run_id INTEGER NOT NULL REFERENCES task_runs(id), "
            "workflow_id TEXT NOT NULL REFERENCES kanban_workflows(workflow_id), "
            "binding_json TEXT NOT NULL, profile TEXT NOT NULL, claim_lock TEXT NOT NULL, "
            "evidence_mode TEXT NOT NULL, request_sha256 TEXT NOT NULL, artifacts_json TEXT NOT NULL, "
            "response_sha256 TEXT, runner_version TEXT NOT NULL, verdict TEXT, "
            "state TEXT NOT NULL, UNIQUE(task_id, run_id))"
        )
        conn1.execute(
            "INSERT INTO kanban_workflows VALUES ('wf_cas', 'k', 'c', 'd', 'l', 'real', 'active', 1, '{}', 'sha')"
        )
        conn1.commit()

        # Seed target task
        t = kb.create_task(conn1, title="cas-target", assignee="alice")
        t_unrelated = kb.create_task(conn1, title="cas-unrelated", assignee="bob")

        if mismatch_kind == "running_worker":
            # Set to running with worker identity and mock worker termination cleanup
            monkeypatch.setattr(
                kb,
                "_prepare_running_worker_cleanup",
                lambda _c, _tid, reason="": (True, (_tid, 1, 999999, "lock_old")),
            )
            conn1.execute(
                "UPDATE tasks SET status = 'running', current_run_id = 1, worker_pid = 999999, claim_lock = 'lock_old' "
                "WHERE id = ?",
                (t,),
            )
        conn1.commit()

        # Seed core relation families: comments, events, runs, links, subscriptions
        kb.add_comment(conn1, t, "author1", "comment1")
        kb.add_comment(conn1, t, "author2", "comment2")
        conn1.execute("INSERT INTO task_runs (task_id, status, started_at) VALUES (?, 'running', 100)", (t,))
        run_id = conn1.execute("SELECT id FROM task_runs WHERE task_id = ?", (t,)).fetchone()[0]
        conn1.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'custom_ev', '{}', 100)", (t,))
        kb.link_tasks(conn1, t, t_unrelated)
        conn1.execute("INSERT INTO kanban_notify_subs (task_id, platform, chat_id, created_at) VALUES (?, 'cli', 'c1', 100)", (t,))

        # Seed legacy governance rows
        conn1.execute("INSERT INTO kanban_task_bindings VALUES (?, 'wf_cas', '{\"k\":\"v\"}', 'act', 1)", (t,))
        conn1.execute("INSERT INTO kanban_dispositions VALUES ('act_cas', 'wf_cas', 'kind1', 'tgt', '{\"p\":1}', ?, 'pending')", (t,))
        conn1.execute("INSERT INTO kanban_invocations VALUES ('inv_cas', ?, ?, 'wf_cas', '{}', 'prof', 'lock_old', 'real', 'req', 'art', 'resp', 'v1', 'PASS', 'finished')", (t, run_id))

        # Seed unrelated control relations
        kb.add_comment(conn1, t_unrelated, "author_u", "comment_u")
        conn1.execute("INSERT INTO task_runs (task_id, status, started_at) VALUES (?, 'done', 200)", (t_unrelated,))
        run_u = conn1.execute("SELECT id FROM task_runs WHERE task_id = ?", (t_unrelated,)).fetchone()[0]
        conn1.execute("INSERT INTO kanban_task_bindings VALUES (?, 'wf_cas', '{}', 'act', 1)", (t_unrelated,))
        conn1.execute("INSERT INTO kanban_dispositions VALUES ('act_u', 'wf_cas', 'kind1', 'tgt_u', '{}', ?, 'pending')", (t_unrelated,))
        conn1.execute("INSERT INTO kanban_invocations VALUES ('inv_u', ?, ?, 'wf_cas', '{}', 'prof', 'lock', 'real', 'req', 'art', 'resp', 'v1', 'PASS', 'finished')", (t_unrelated, run_u))
        conn1.commit()

        # Capture counts / records before delete attempt
        counts_before = {
            "task_comments": conn1.execute("SELECT COUNT(*) FROM task_comments WHERE task_id = ?", (t,)).fetchone()[0],
            "task_events": conn1.execute("SELECT COUNT(*) FROM task_events WHERE task_id = ?", (t,)).fetchone()[0],
            "task_runs": conn1.execute("SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (t,)).fetchone()[0],
            "task_links": conn1.execute("SELECT COUNT(*) FROM task_links WHERE parent_id = ? OR child_id = ?", (t, t)).fetchone()[0],
            "kanban_notify_subs": conn1.execute("SELECT COUNT(*) FROM kanban_notify_subs WHERE task_id = ?", (t,)).fetchone()[0],
            "kanban_task_bindings": conn1.execute("SELECT COUNT(*) FROM kanban_task_bindings WHERE task_id = ?", (t,)).fetchone()[0],
            "kanban_dispositions": conn1.execute("SELECT COUNT(*) FROM kanban_dispositions WHERE task_id = ?", (t,)).fetchone()[0],
            "kanban_invocations": conn1.execute("SELECT COUNT(*) FROM kanban_invocations WHERE task_id = ?", (t,)).fetchone()[0],
        }
        assert all(c > 0 for c in counts_before.values()), f"Setup incomplete: {counts_before}"

        def fake_write_txn(c, *args, **kwargs):
            @contextlib.contextmanager
            def _txn():
                # Simulate concurrent commit happening right before delete_task begins its locked write txn
                if mismatch_kind == "status":
                    c.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (t,))
                else:
                    c.execute(
                        "UPDATE tasks SET claim_lock = 'lock_new_concurrent', worker_pid = 888888 WHERE id = ?",
                        (t,),
                    )
                # Now enter the real write_txn
                with orig_write_txn(c, *args, **kwargs):
                    yield c
            return _txn()

        orig_write_txn = kbc.write_txn
        monkeypatch.setattr(kbc, "write_txn", fake_write_txn)
        monkeypatch.setattr(kb, "write_txn", fake_write_txn)

        res = kb.delete_task(conn1, t)

        # Assertions
        assert res is False, "delete_task must return False on CAS mismatch"
        task_row = conn1.execute("SELECT status, claim_lock, worker_pid FROM tasks WHERE id = ?", (t,)).fetchone()
        assert task_row is not None, "task must survive"
        if mismatch_kind == "status":
            assert task_row["status"] == "blocked"
        else:
            assert task_row["status"] == "running"
            assert task_row["claim_lock"] == "lock_new_concurrent"
            assert task_row["worker_pid"] == 888888

        # All relations of task t must remain completely intact
        for table, count_before in counts_before.items():
            if table == "task_links":
                count_after = conn1.execute(
                    "SELECT COUNT(*) FROM task_links WHERE parent_id = ? OR child_id = ?",
                    (t, t),
                ).fetchone()[0]
            else:
                count_after = conn1.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE task_id = ?",
                    (t,),
                ).fetchone()[0]
            assert count_after == count_before, (
                f"Table {table} for task {t} was mutated despite failed CAS: before={count_before}, after={count_after}"
            )

        # Unrelated controls remain untouched
        assert conn1.execute("SELECT COUNT(*) FROM task_comments WHERE task_id = ?", (t_unrelated,)).fetchone()[0] == 1
        assert conn1.execute("SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (t_unrelated,)).fetchone()[0] == 1
        assert conn1.execute("SELECT COUNT(*) FROM kanban_task_bindings WHERE task_id = ?", (t_unrelated,)).fetchone()[0] == 1
        assert conn1.execute("SELECT COUNT(*) FROM kanban_dispositions WHERE task_id = ?", (t_unrelated,)).fetchone()[0] == 1
        assert conn1.execute("SELECT COUNT(*) FROM kanban_invocations WHERE task_id = ?", (t_unrelated,)).fetchone()[0] == 1
