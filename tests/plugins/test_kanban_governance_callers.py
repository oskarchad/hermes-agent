"""Actual ASGI single/bulk mutations share the neutral governance boundary."""
import importlib.util
from pathlib import Path
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_governance as g
from hermes_cli import kanban_governance_store as store
from hermes_cli.kanban_db_connect import connect, write_txn


@pytest.fixture
def governed_http(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
    db = home / ".hermes" / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    conn = connect(db)
    with write_txn(conn):
        conn.execute("INSERT INTO kanban_workflows VALUES (?,?,?,?,?,?,?,1,?,?)",
                     ("fixture", "test", "test/v1", "fixture-decision", "fixture", "simulation",
                      "active", store.canonical_bytes({"workspace": str(tmp_path)}).decode(), store.digest("fixture")))
        # Recovery is neutral and executable without any domain bootstrap.
        order = g.WorkOrder(g.Binding("fixture", "test/v1", "recovery", "fixture",
                                     store.digest("fixture"), "recovery", "simulation"),
                            "recovery", "fixture root", "otto", ())
        g.reserve_tx(conn, order)
    parent, = g.drain(conn)
    assert kb.complete_task(conn, parent, summary="fixture intake recovery finished")
    child = kb.create_task(conn, title="unissued forward", assignee="bob", parents=[parent])
    plugin = Path(__file__).resolve().parents[2] / "plugins/kanban/dashboard/plugin_api.py"
    spec = importlib.util.spec_from_file_location("guardian_http_test", plugin)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router)
    with TestClient(app) as client:
        yield client, conn, child
    conn.close()


@pytest.mark.parametrize("bulk", [False, True])
@pytest.mark.parametrize("status", ["done", "ready", "running", "review", "todo", "triage"])
def test_tc07_http_cannot_promote_unissued_child(governed_http, bulk, status):
    client, conn, child = governed_http
    before = kb.get_task(conn, child).status
    payload = {"status": status, "summary": "PASS", "metadata": {}}
    if bulk:
        response = client.post("/tasks/bulk", json=dict(payload, ids=[child]))
        assert response.status_code == 200
        assert response.json()["results"][0]["ok"] is False
    else:
        response = client.patch("/tasks/" + child, json=payload)
        assert response.status_code in {400, 409}, response.text
    assert kb.get_task(conn, child).status == before
    assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (child,)).fetchone()[0] == 0
    assert store.binding(conn, child)["role"] == "unclassified"
    g.drain(conn)
    assert conn.execute("SELECT COUNT(*) FROM kanban_dispositions WHERE kind='recovery' AND task_id IS NOT NULL").fetchone()[0] > 1


def test_http_nongoverned_completion_is_unchanged(governed_http):
    client, conn, _ = governed_http
    ordinary = kb.create_task(conn, title="ordinary", assignee="bob")
    response = client.patch("/tasks/" + ordinary, json={"status": "done", "summary": "normal"})
    assert response.status_code == 200
    assert kb.get_task(conn, ordinary).status == "done"
