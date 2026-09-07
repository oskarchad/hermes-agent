"""Bind a Captain terminal frame to the serialized turn's durable reply."""


def persist_report_identity(agent, result: dict, completion_id: str) -> None:
    db = getattr(agent, "_session_db", None)
    session_id = getattr(agent, "session_id", None)
    text = result.get("final_response")
    if db is None or not session_id or not isinstance(text, str) or not text.strip():
        raise RuntimeError("Captain reply is not durable")
    rows = db.get_messages_as_conversation(session_id)
    if not rows or rows[-1].get("role") != "assistant" or rows[-1].get("content") != text:
        raise RuntimeError("Captain final reply is not persisted")
    user = next((row for row in reversed(rows) if row.get("role") == "user"), {})
    if (user.get("display_metadata") or {}).get("captain_completion_id") != completion_id:
        raise RuntimeError("Captain reply does not belong to the admitted report")
    metadata = {"captain_completion_id": completion_id, "authorship": "assistant"}
    if not db.set_latest_matching_message_display_kind(
        session_id, role="assistant", content=text, display_kind="captain_report",
        display_metadata=metadata,
    ):
        raise RuntimeError("Captain reply identity was not persisted")
    for row in reversed(result.get("messages") or []):
        if row.get("role") == "assistant" and row.get("content") == text:
            row["display_kind"] = "captain_report"
            row["display_metadata"] = metadata
            break
