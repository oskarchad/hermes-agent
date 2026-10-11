"""Profile-local durable Captain transcript receipts."""
from __future__ import annotations
import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("hermes_state")

class CaptainMessagesMixin:
    def rehome_captain_report(
        self,
        completion_id: str,
        destination_session_id: str,
    ) -> Optional[dict[str, Any]]:
        """Return one profile-local Captain receipt, rehoming its turn if needed."""
        if not completion_id or not destination_session_id:
            return None

        def _do(conn):
            destination = conn.execute(
                "SELECT 1 FROM sessions WHERE id = ?",
                (destination_session_id,),
            ).fetchone()
            if destination is None:
                raise RuntimeError("Captain receipt destination session is missing")

            receipt_rows = conn.execute(
                f"SELECT session_id, {self._CONVERSATION_ROW_COLUMNS} "
                "FROM messages "
                "WHERE role = 'assistant' AND active = 1 "
                "AND json_valid(display_metadata) = 1 "
                "AND json_extract(display_metadata, '$.captain_completion_id') = ? "
                "ORDER BY CASE WHEN session_id = ? THEN 0 ELSE 1 END, id",
                (completion_id, destination_session_id),
            ).fetchall()
            if not receipt_rows:
                return None

            canonical = receipt_rows[0]
            canonical_id = int(canonical["id"])
            source_session_id = str(canonical["session_id"])

            def _turn_start(session_id: str, assistant_id: int) -> int:
                row = conn.execute(
                    "SELECT id FROM messages "
                    "WHERE session_id = ? AND active = 1 AND role = 'user' AND id <= ? "
                    "ORDER BY id DESC LIMIT 1",
                    (session_id, assistant_id),
                ).fetchone()
                return int(row["id"]) if row is not None else assistant_id

            turn_start = _turn_start(source_session_id, canonical_id)
            source_turn_rows = conn.execute(
                "SELECT id FROM messages "
                "WHERE session_id = ? AND active = 1 AND id BETWEEN ? AND ? "
                "ORDER BY id",
                (source_session_id, turn_start, canonical_id),
            ).fetchall()
            source_turn_ids = [int(row["id"]) for row in source_turn_rows]
            if not source_turn_ids or canonical_id not in source_turn_ids:
                raise RuntimeError("Captain receipt lost its canonical source rows")

            affected_session_ids = {
                source_session_id,
                destination_session_id,
            }

            for duplicate in receipt_rows[1:]:
                duplicate_session_id = str(duplicate["session_id"])
                affected_session_ids.add(duplicate_session_id)
                duplicate_id = int(duplicate["id"])
                duplicate_start = _turn_start(duplicate_session_id, duplicate_id)
                conn.execute(
                    "UPDATE messages SET active = 0 "
                    "WHERE session_id = ? AND active = 1 AND id BETWEEN ? AND ?",
                    (duplicate_session_id, duplicate_start, duplicate_id),
                )

            moved = source_session_id != destination_session_id
            if moved:
                boundary = conn.execute('SELECT COALESCE(MAX(id), 0) FROM messages').fetchone()[0]
                self._clone_message_rows(conn, source_turn_ids, session_id=destination_session_id)
                appended_turn_ids = [int(row[0]) for row in conn.execute(
                    'SELECT id FROM messages WHERE session_id = ? AND id > ? ORDER BY id',
                    (destination_session_id, boundary)).fetchall()]
                if len(appended_turn_ids) != len(source_turn_ids):
                    raise RuntimeError('Captain receipt lost a canonical source row during rehome')

                placeholders = ",".join("?" for _ in source_turn_ids)
                conn.execute(
                    "UPDATE messages SET active = 0 "
                    f"WHERE active = 1 AND id IN ({placeholders})",
                    source_turn_ids,
                )
                source_turn_ids = appended_turn_ids

            for session_id in sorted(affected_session_ids):
                active_rows = conn.execute(
                    "SELECT tool_calls FROM messages "
                    "WHERE session_id = ? AND active = 1",
                    (session_id,),
                ).fetchall()
                tool_call_count = 0
                for row in active_rows:
                    raw_tool_calls = row["tool_calls"]
                    if not raw_tool_calls:
                        continue
                    try:
                        decoded_tool_calls = json.loads(raw_tool_calls)
                    except (json.JSONDecodeError, TypeError):
                        tool_call_count += 1
                    else:
                        tool_call_count += (
                            len(decoded_tool_calls)
                            if isinstance(decoded_tool_calls, list)
                            else 1
                        )
                conn.execute(
                    "UPDATE sessions SET message_count = ?, tool_call_count = ? "
                    "WHERE id = ?",
                    (len(active_rows), tool_call_count, session_id),
                )

            placeholders = ",".join("?" for _ in source_turn_ids)
            turn_rows = conn.execute(
                f"SELECT {self._CONVERSATION_ROW_COLUMNS} FROM messages "
                f"WHERE active = 1 AND id IN ({placeholders}) "
                "ORDER BY id",
                source_turn_ids,
            ).fetchall()
            messages = self._rows_to_conversation(
                turn_rows,
                session_id=destination_session_id,
                include_ancestors=False,
                repair_alternation=False,
            )
            report = next(
                (
                    message
                    for message in reversed(messages)
                    if message.get("role") == "assistant"
                    and isinstance(message.get("display_metadata"), dict)
                    and message["display_metadata"].get("captain_completion_id")
                    == completion_id
                ),
                None,
            )
            if report is None:
                raise RuntimeError("Captain receipt lost its canonical assistant row")
            return {
                "report": report,
                "messages": messages,
                "source_session_id": source_session_id,
                "destination_session_id": destination_session_id,
                "moved": moved,
            }

        return self._execute_write(_do)

    def stamp_captain_completion(self, session_id: str, row_id: int, content: str, completion_id: str) -> bool:
        """Stamp one structurally selected persisted assistant row before acknowledging its inbox lease."""
        def _do(conn):
            row = conn.execute("SELECT display_metadata FROM messages WHERE id = ? AND session_id = ? "
                "AND role = 'assistant' AND active = 1 AND content = ?",
                (row_id, session_id, self._encode_content(content))).fetchone()
            if row is None:
                return False
            metadata = self._decode_display_metadata(row[0]) or {}
            previous = metadata.get("captain_completion_id")
            if previous and previous != completion_id:
                return False
            metadata["captain_completion_id"] = completion_id
            conn.execute("UPDATE messages SET display_metadata = ? WHERE id = ?",
                (self._encode_display_metadata(metadata), row_id))
            return True
        return self._execute_write(_do)

