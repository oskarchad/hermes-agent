"""Neutral transactional identity and immutable artifact contracts."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_governance as g
from hermes_cli import kanban_governance_store as store
from hermes_cli.kanban_db_connect import connect, write_txn


class Validator:
    def evaluate(self, conn, task_id, operation):
        return g.TransitionDisposition(True, "fixture neutral contract")

    def reconcile(self, conn, workflow_id):
        return ()


def test_concurrent_issue_and_archive_replay_keep_identity_and_charge(tmp_path, monkeypatch):
    path = tmp_path / "board.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(path))
    conn = connect(path)
    scope = {"workspace": str(tmp_path)}
    with write_txn(conn):
        conn.execute("INSERT INTO kanban_workflows VALUES (?,?,?,?,?,?,?,1,?,?)",
                     ("fixture", "fixture", "fixture/v1", "decision", "lineage", "simulation", "active",
                      store.canonical_bytes(scope).decode(), store.digest(scope)))
        conn.execute("INSERT INTO kanban_lineage_budget VALUES (?,?,?,?,?,?)",
                     ("lineage", "repair", "fixture-source", store.digest("fixture-source"), 1, 0))
        order = g.WorkOrder(g.Binding("fixture", "fixture/v1", "stage", "a|b:日本語", store.digest("input"),
                                     "repair", "simulation"), "repair", "repair:fixture", "bob", (), "repair")
        g.reserve_tx(conn, order)
    barrier = threading.Barrier(2)
    def issue():
        other = connect(path)
        try:
            g.register_validator(other, "fixture/v1", Validator())
            barrier.wait(timeout=5)
            return g.drain(other)
        finally:
            other.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: issue(), range(2)))
    rows = kb.list_tasks(conn)
    assert len(rows) == 1
    assert {task for result in results for task in result} <= {rows[0].id}
    assert conn.execute("SELECT used_count FROM kanban_lineage_budget").fetchone()[0] == 1
    g.register_validator(conn, "fixture/v1", Validator())
    assert kb.archive_task(conn, rows[0].id)
    with write_txn(conn):
        g.reserve_tx(conn, order)
    g.reconcile(conn, now=0)
    assert kb.get_task(conn, rows[0].id).status == "archived"
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    assert conn.execute("SELECT used_count FROM kanban_lineage_budget").fetchone()[0] == 1
    conn.close()


@pytest.mark.parametrize("lane", ["workflows", "dispositions"])
def test_bounded_scans_progress_past_stuck_prefix_after_reconnect(tmp_path, monkeypatch, lane):
    path = tmp_path / "board.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(path))
    scope = {"workspace": str(tmp_path)}
    conn = connect(path)
    with write_txn(conn):
        for number in range(4):
            workflow = f"fixture-{number}"
            conn.execute("INSERT INTO kanban_workflows VALUES (?,?,?,?,?,?,?,1,?,?)",
                         (workflow, "fixture", "fixture/v1", "decision", workflow, "simulation", "active",
                          store.canonical_bytes(scope).decode(), store.digest(scope)))
            if lane == "dispositions":
                order = g.WorkOrder(g.Binding(workflow, "fixture/v1", "stage", workflow, store.digest(number),
                                              "producer", "simulation"), "produce", "fixture work", "bob", ())
                g.reserve_tx(conn, order)
    for _ in range(8):
        # Missing validators intentionally keep normal dispositions pending. A
        # restarted bounded scanner must still issue independent recovery.
        if lane == "workflows":
            g.reconcile(conn, now=0, limit=2)
        else:
            g.drain(conn, limit=2)
        conn.close()
        conn = connect(path)
    recovered = {row[0] for row in conn.execute(
        "SELECT workflow_id FROM kanban_dispositions WHERE kind='recovery' AND state='applied'")}
    assert recovered == {f"fixture-{number}" for number in range(4)}
    conn.close()


@pytest.mark.parametrize("fault", ["path", "symlink", "modified"])
def test_content_objects_reject_escape_and_mutation(tmp_path, fault):
    conn = connect(tmp_path / "board.db")
    task = kb.create_task(conn, title="artifact fixture", assignee="bob")
    sha = store.put_object(conn, task, b"frozen input")
    path = store.object_path(conn, task, sha)
    if fault == "path":
        with pytest.raises(ValueError):
            store.read_object(conn, task, "../" + sha)
    else:
        if fault == "symlink":
            target = tmp_path / "external-object"
            target.write_bytes(b"frozen input")
            path.unlink()
            path.symlink_to(target)
        else:
            path.write_bytes(b"changed input")
        with pytest.raises(ValueError):
            store.read_object(conn, task, sha)
    conn.close()
