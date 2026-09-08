"""Audited operator promotion of existing Kanban tasks."""

import sqlite3
from typing import Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch


def promote_task(
    conn: sqlite3.Connection, task_id: str, *, actor: str, reason: Optional[str] = None,
    force: bool = False, dry_run: bool = False,
) -> tuple[bool, Optional[str]]:
    """Promote todo/blocked, or explicitly continue an idle ready card.

    Ready continuation records authority without rewriting phase or counters.
    Its actor/reason are mandatory and force cannot waive its parent gate.
    All checks and the audit event share the write lock with dispatcher claims.
    """
    with kb.write_txn(conn):
        row = conn.execute(
            "SELECT status, claim_lock, current_run_id, worker_pid FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return False, f"task {task_id} not found"
        cur_status = row["status"]
        if cur_status not in ("todo", "blocked", "ready"):
            return False, f"task {task_id} is {cur_status!r}; promote only applies to 'todo', 'blocked' or 'ready'"

        continuing = cur_status == "ready"
        if continuing:
            if not actor or not actor.strip() or not reason or not reason.strip():
                return False, "ready continuation requires an explicit actor and non-empty reason"
            open_run = conn.execute(
                "SELECT 1 FROM task_runs WHERE task_id = ? AND ended_at IS NULL LIMIT 1",
                (task_id,),
            ).fetchone()
            if any(row[key] is not None for key in ("claim_lock", "current_run_id", "worker_pid")) or open_run:
                return False, "ready continuation refused while a worker claim or run exists"
            if dispatch._handoff_worker_teardown_pending(conn, task_id):
                return False, "ready continuation refused while prior worker teardown is pending"

        if not force or continuing:
            parents = conn.execute(
                "SELECT t.id, t.status FROM tasks t "
                "JOIN task_links l ON l.parent_id = t.id WHERE l.child_id = ?",
                (task_id,),
            ).fetchall()
            unsatisfied = [p["id"] for p in parents if p["status"] not in ("done", "archived")]
            if unsatisfied:
                return False, f"unsatisfied parent dependencies: {', '.join(unsatisfied)}"

        if dry_run:
            return True, None

        payload = {"actor": actor, "reason": reason, "forced": force}
        if continuing:
            payload["source_status"] = "ready"
        else:
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
        kb._append_event(conn, task_id, "promoted_manual", payload)

    return True, None
