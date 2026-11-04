"""Captain terminal settlement on the native prompt-turn persistence boundary."""
from __future__ import annotations

from .method_ctx import bind_module


def _stamp_captain_turn_receipt(session, st, raw, status, payload):
    if not st.completion_id:
        return
    payload["id"] = st.completion_id
    if status != "complete" or not st.require_persisted:
        return
    receipt = payload.get("persisted_turn") or {}
    row_id = receipt.get("final_assistant_row_id")
    db = getattr(st.agent, "_session_db", None)
    session_id = getattr(st.agent, "session_id", None) or session.get("session_key")
    if db is None or type(row_id) is not int or not db.stamp_captain_completion(
            session_id, row_id, raw, st.completion_id):
        raise RuntimeError("Captain report is not durably persisted")
    for message in st.result.get("messages") or ():
        if message.get("_row_id") == row_id:
            message.setdefault("display_metadata", {})["captain_completion_id"] = st.completion_id


def _rollback_failed_captain_input(session, st):
    """Deactivate only the failed turn's addressed input, preserving its prior transcript."""
    db = getattr(st.agent, "_session_db", None)
    session_id = getattr(st.agent, "session_id", None) or session.get("session_key")
    before = {m.get("_row_id") for m in st.history if isinstance(m, dict)}
    messages = (st.result or {}).get("messages", []) if isinstance(st.result, dict) else []
    candidates = [*messages, *(session.get("history") or ())]
    pending = session.get("_submit_user_row")
    if isinstance(pending, dict):
        candidates.append(pending)
    row_ids = {m.get("_row_id") for m in candidates if isinstance(m, dict)
               and m.get("role") == "user" and m.get("_row_id") not in before
               and type(m.get("_row_id")) is int}
    if row_ids and db is None:
        raise RuntimeError("Captain failed input rollback has no durable store")
    for row_id in row_ids:
        db.deactivate_message(session_id, row_id)
    with session["history_lock"]:
        session["history"] = list(st.history)
        session["history_version"] = int(session.get("history_version", 0)) + 1
        session.pop("_submit_user_row", None)


def _settle_captain_prompt_turn(session, st, succeeded):
    if st.captain_terminal is None or st.captain_attempted:
        return
    if not succeeded:
        try:
            _rollback_failed_captain_input(session, st)
        except Exception:
            st.captain_attempted = True
            st.captain_terminal(None)
            raise
    st.captain_attempted = True
    st.captain_terminal(bool(succeeded))
    st.receipt_committed = True


def register(server):
    bind_module(globals(), server)
