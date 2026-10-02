"""Captain inbox → real ``_run_prompt_submit``: no mocked dispatcher signature.

Regression: ``captain_inbox`` called ``_run_prompt_submit(on_terminal=..., completion_id=...,
require_persisted=..., turn_purpose=...)`` against upstream's prompt_turn, which only takes
``terminal_callback``. Every Captain batch raised ``TypeError`` and was released for retry forever.
The reply row is selected structurally (the turn's persisted final assistant message), never by
matching ``final_response`` text: storage redacts secrets and the file-mutation footer is appended
after persistence, so the two legitimately differ.
"""
import threading
from types import SimpleNamespace

import pytest

from agent.context_compressor import _DB_PERSISTED_MARKER
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_state import SessionDB
from tui_gateway import server
from tests.tui_gateway.test_auto_continue import turn_env, marker_home, _session  # noqa: F401
from tests.tui_gateway.test_kanban_captain_inbox import _inbox_states

FINAL = "Captain explains the result"


@pytest.mark.parametrize("stored", [FINAL, "Captain explains the *** (redacted)", None])
def test_captain_report_runs_real_turn_and_acks_only_after_durable_reply(
        turn_env, monkeypatch, stored):  # noqa: F811
    db = SessionDB()
    db.create_session(session_id="captain-real", source="tui", model="test")
    frames, acked_at_complete = [], []

    def write_json(frame):
        params = frame.get("params") or {}
        if params.get("type") == "message.complete":
            acked_at_complete.append(_inbox_states())
        frames.append(params)
        return True

    monkeypatch.setattr(server, "write_json", write_json)
    monkeypatch.setattr(server, "_start_usage_ticker",
                        lambda *a: (threading.Event(), SimpleNamespace(join=lambda: None)))

    def run(message, **kwargs):
        user = {"role": "user", "content": message,
                "_row_id": db.append_message("captain-real", "user", content=message),
                _DB_PERSISTED_MARKER: True}
        messages = [user]
        if stored is not None:
            # The persisted row may differ from final_response (redaction / post-persist footer).
            messages.append({"role": "assistant", "content": stored,
                             "_row_id": db.append_message("captain-real", "assistant", content=stored),
                             _DB_PERSISTED_MARKER: True})
        agent._session_messages = messages
        return {"final_response": FINAL, "messages": messages}

    agent = SimpleNamespace(session_id="captain-real", _session_db=db,
                            clear_interrupt=lambda: None, run_conversation=run)
    session = _session(agent=agent, session_key="captain-real")
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="cap-real-turn", assignee="worker")
        kb.register_captain_owner(conn, tid, profile=server._session_captain_profile(session),
                                  origin_session_key=None)
        kb.complete_task(conn, tid, summary="worker finished")
    finally:
        conn.close()

    server._captain_poll_kanban("sid-captain", session)
    session["_run_thread"].join(timeout=10)
    assert not session["_run_thread"].is_alive()

    statuses = [p["payload"].get("status") for p in frames if p.get("type") == "message.complete"]
    assert session["running"] is False
    if stored is not None:
        # Settled exactly once, durably, and acked before the client saw success.
        assert _inbox_states() == {"acked": 1}
        assert statuses == ["complete"] and acked_at_complete == [{"acked": 1}]
        # The durable receipt reconciliation reads is the stamped assistant row.
        completion_id = (db.get_messages_as_conversation("captain-real")[-1]
                         .get("display_metadata") or {}).get("captain_completion_id")
        receipt = server._persisted_captain_report(session, completion_id)
        assert receipt is not None and receipt["report"]["content"] == stored
    else:
        # No durable reply: no visible success, claim released for a retry.
        assert _inbox_states() == {"pending": 1}
        assert "complete" not in statuses
    db.close()
