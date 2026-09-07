"""Origin identity survives idle periods and competing same-profile receivers."""
import json
from pathlib import Path

import pytest

from gateway.session_context import clear_session_vars, set_session_vars
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
from tools import kanban_tools


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_TENANT", raising=False)
    conn = connect()
    yield conn
    conn.close()


@pytest.mark.parametrize("platform", ["webui", "tui"])
@pytest.mark.parametrize("terminal", ["done", "blocked"])
def test_create_routes_terminal_event_to_bound_origin(board, platform, terminal):
    from tools.registry import registry

    tokens = set_session_vars(platform=platform, chat_id="origin", session_key="origin",
                              session_id="durable-id", profile="captain")
    try:
        result = json.loads(registry.get_entry("kanban_create").handler(
            {"title": "origin delivery", "assignee": "worker"}))
    finally:
        clear_session_vars(tokens)
    tid = result["task_id"]
    owner = dict(board.execute(
        "SELECT profile, origin_session_key FROM kanban_captain_registry WHERE task_id=?",
        (tid,),
    ).fetchone())
    origin_key = "webui:origin" if platform == "webui" else "origin"
    assert owner == {"profile": "captain", "origin_session_key": origin_key}
    if terminal == "done":
        kb.complete_task(board, tid, summary="finished")
    else:
        kb.block_task(board, tid, reason="needs operator")
    assert kb.read_captain_candidates(board, profile="captain", receiver_session_key="sibling") == []
    rows = kb.read_captain_candidates(board, profile="captain", receiver_session_key=origin_key)
    assert len(rows) == 1
    assert rows[0]["task_id"] == tid


@pytest.mark.parametrize("origin", ["origin", None])
@pytest.mark.parametrize("subscriptions", [0, 1, 2])
def test_no_guessing_origin_at_candidate_and_atomic_lease_boundaries(board, origin, subscriptions):
    from hermes_cli.kanban_db_notify import add_notify_sub
    tid = kb.create_task(board, title="queued", assignee="worker",
                         captain_profile="captain", captain_origin_session_key=origin)
    for index in range(subscriptions):
        add_notify_sub(board, task_id=tid, platform="webui", chat_id=f"source-{index}",
                       notifier_profile="captain")
    kb.complete_task(board, tid, summary="done")
    with kb.write_txn(board):
        board.execute("DELETE FROM kanban_captain_receivers")
    event_id = board.execute("SELECT event_id FROM kanban_captain_inbox WHERE task_id=?", (tid,)).fetchone()[0]
    assert kb.read_captain_candidates(board, profile="captain", receiver_session_key="sibling") == []
    _, events = kb.lease_captain_reports(board, profile="captain", owner="sibling",
                                        event_ids=[event_id], receiver_session_key="sibling")
    assert events == []
    assert board.execute("SELECT state FROM kanban_captain_inbox WHERE event_id=?", (event_id,)).fetchone()[0] == "pending"
    target = origin or "webui:source-0"
    _, own_events = kb.lease_captain_reports(board, profile="captain", owner=target,
                                            event_ids=[event_id], receiver_session_key=target)
    assert bool(own_events) == bool(origin or subscriptions == 1)


def test_origin_lineage_requires_unique_published_compression_tip(tmp_path):
    from hermes_state import SessionDB
    from hermes_cli.kanban_origin import compression_destinations

    db = SessionDB(tmp_path / "state.db")
    db.create_session("root", source="tui")
    for parent, child in (("root", "middle"), ("middle", "tip")):
        db.publish_compression_child(
            parent_session_id=parent, child_session_id=child, source="tui",
            messages=[{"role": "user", "content": "compressed handoff"}],
            require_compression_lease=False)
    assert compression_destinations(db.db_path, "tip") == ("root", "middle", "tip")
    assert compression_destinations(db.db_path, "root") == ("root", "middle", "tip")
    db.create_session("sibling", source="tui", parent_session_id="middle")
    assert compression_destinations(db.db_path, "tip") == ()
    assert compression_destinations(db.db_path, "sibling") == ()
    db.close()
