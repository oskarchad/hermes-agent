"""Profile admission at Kanban write/dispatch seams; no profile lifecycle changes."""
from __future__ import annotations

import sqlite3

from hermes_cli.profiles import profile_dispatch_error


def require_dispatch_enabled(assignee: str | None) -> None:
    error = profile_dispatch_error(assignee)
    if error:
        raise ValueError(error)


def block_ineligible_dispatch(conn: sqlite3.Connection, row: sqlite3.Row, assignee: str) -> bool:
    """Park an unchanged unclaimed row once, preserving its owner and source phase."""
    from hermes_cli import kanban_db as kb

    with kb.write_txn(conn):
        current = conn.execute(
            "SELECT status, assignee, claim_lock, current_run_id, worker_pid FROM tasks WHERE id = ?",
            (row["id"],),
        ).fetchone()
        if current is None or current["status"] != row["status"] or current["assignee"] != assignee:
            return False
        if any(current[key] is not None for key in ("claim_lock", "current_run_id", "worker_pid")):
            return False
        reason = profile_dispatch_error(assignee)
        if not reason:
            return False
        conn.execute(
            "UPDATE tasks SET status = 'blocked', block_kind = 'needs_input' WHERE id = ?",
            (row["id"],),
        )
        kb._append_event(conn, row["id"], "blocked", {
            "reason": reason, "kind": "needs_input", "source_status": current["status"],
        })
    kb.notify_task_updated(conn, row["id"], ("status", "block_kind"))
    return True
