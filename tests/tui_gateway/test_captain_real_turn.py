"""Captain reports cross the real prompt dispatcher, persistence and projection."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from tui_gateway import server


@pytest.mark.parametrize("outcome", ["complete", "blocked", "failed", "missing_persistence"])
def test_captain_terminal_is_bound_to_durable_visible_reply(tmp_path, monkeypatch, outcome):
    successful = outcome in {"complete", "blocked"}
    db = SessionDB(tmp_path / "state.db")
    db.create_session("origin", source="tui")
    if outcome in {"complete", "blocked"}:
        db.create_session("ancestor", source="tui")
        for parent, child in (("ancestor", "middle"), ("middle", "current")):
            db.publish_compression_child(
                parent_session_id=parent, child_session_id=child, source="tui",
                messages=[{"role": "user", "content": "handoff"}],
                require_compression_lease=False)
    emitted, terminals = [], []
    finished = threading.Event()
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(server, "_wire_callbacks", lambda sid: None)
    monkeypatch.setattr(server, "_sync_agent_model_with_config", lambda *a: None)
    monkeypatch.setattr(server, "_session_cwd", lambda s: str(tmp_path))
    monkeypatch.setattr(server, "_register_session_cwd", lambda s: None)
    monkeypatch.setattr(server, "_tts_stream_begin", lambda: None)
    monkeypatch.setattr(server, "_sync_session_key_after_compress", lambda *a, **k: None)
    monkeypatch.setattr(server, "_get_usage", lambda a: {})
    monkeypatch.setattr(server, "_emit", lambda event, sid, payload=None: emitted.append((event, sid, payload)))
    monkeypatch.setattr(server, "_run_post_turn_followups", lambda *a: finished.set())

    def run(message, *, conversation_history, stream_callback, persist_user_message,
            persist_user_display_kind=None, persist_user_display_metadata=None):
        user = {"role": "user", "content": persist_user_message,
                "display_kind": persist_user_display_kind,
                "display_metadata": persist_user_display_metadata}
        target = agent.session_id
        db.append_message(target, **user)
        stream_callback("draft must not escape")
        reply = {"role": "assistant", "content": "Worker result explained"}
        if outcome != "missing_persistence":
            db.append_message(target, **reply)
        return {"messages": [*conversation_history, user, reply],
                "final_response": reply["content"], "failed": outcome == "failed"}

    agent = SimpleNamespace(session_id="origin", _session_db=db,
                            run_conversation=run, clear_interrupt=lambda: None)
    session = {"agent": agent, "session_key": "origin", "history": [],
               "history_lock": threading.Lock(), "history_version": 0, "running": True,
               "attached_images": [], "cols": 80, "slash_worker": None,
               "inflight_turn": None, "show_reasoning": False, "tool_progress_mode": "all"}

    def terminal(receipt):
        if receipt["status"] == "settled":
            rows = db.get_messages_as_conversation("origin")
            assert rows[-1]["display_metadata"]["captain_completion_id"] == "report-test"
            assert not any(event in {"message.delta", "message.complete"} for event, _, _ in emitted)
        terminals.append(receipt)

    expected_id = "report-test"
    if successful:
        from hermes_cli import kanban_db as kb
        from hermes_cli.kanban_db_connect import connect
        monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "boards"))
        monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
        session["profile_home"] = str(tmp_path / "otto")
        monkeypatch.setattr(server, "_KANBAN_POLL_SECONDS", 0.01)
        monkeypatch.setattr(server, "_maybe_fire_tui_loop_tick", lambda *a: None)
        session["running"] = False
        agent.session_id = session["session_key"] = "current"
        conn = connect(board="default")
        tid = kb.create_task(conn, title="real Desktop boundary", assignee="worker",
                             captain_profile="otto", captain_origin_session_key="ancestor")
        if outcome == "complete":
            kb.complete_task(conn, tid, summary="worker completed")
        else:
            kb.block_task(conn, tid, reason="needs input", kind="needs_input")
        event = conn.execute("SELECT event_id FROM kanban_captain_inbox WHERE task_id=?", (tid,)).fetchone()[0]
        expected_id = server._captain_completion_id(
            [{"board": "default", "deliveries": [{"id": f"kanban:default:{event}"}]}], [])
        stop = threading.Event()
        poller = threading.Thread(target=server._notification_poller_loop, args=(stop, "ui-origin", session))
        poller.start()
        try:
            assert finished.wait(10), emitted
        finally:
            stop.set()
            poller.join(5)
        assert kb.captain_unreported_for_task(conn, tid) == 0
        conn.close()
        print({"task": tid, "event": event, "report": expected_id, "origin": "origin", "terminal": outcome})
    else:
        assert server._run_prompt_submit("rid", "ui-origin", session, "worker done",
                                         completion_id=expected_id, terminal_callback=terminal)
        assert finished.wait(10), emitted
        assert len(terminals) == 1
        assert terminals[0]["status"] == "failed"
    session["_run_thread"].join(10)
    rows = db.get_messages_as_conversation(agent.session_id)
    typed_user = next(row for row in reversed(rows) if row.get("role") == "user")
    assert typed_user["display_kind"] == "hidden"
    assert typed_user["display_metadata"]["authorship"] == "system"
    assert not any(event == "message.delta" for event, _, _ in emitted)
    complete = [p for event, _, p in emitted if event == "message.complete" and p.get("status") == "complete"]
    if successful:
        assert len(complete) == 1
        assert complete[0]["id"] == expected_id
        projected = server._history_to_messages(session["history"])
        assert not any(m["role"] == "user" for m in projected)
        assert projected[-1]["id"] == expected_id
    else:
        assert complete == []
    db.close()
