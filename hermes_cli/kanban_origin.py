"""Exact Captain destination shared by discovery and the atomic lease fence."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path


def compression_destinations(db_path, session_id: str) -> tuple[str, ...]:
    """Resolve only a unique published compression chain in one read snapshot.

    Unlike display navigation, delivery must never pick the newest sibling.
    A not-yet-materialized sidecar can address itself, but grants no aliases.
    """
    if not session_id:
        return ()
    if not db_path or not Path(db_path).exists():
        return (session_id,)
    try:
        with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")

            def get(sid):
                return conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()

            def fork(row):
                config = json.loads(row["model_config"] or "{}")
                return row["source"] == "tool" or bool(config.get("_branched_from") or config.get("_delegate_from"))

            current = get(session_id)
            if current is None:
                return (session_id,)
            seen = set()
            for _ in range(100):
                if current["id"] in seen:
                    return ()
                seen.add(current["id"])
                parent = get(current["parent_session_id"]) if current["parent_session_id"] else None
                if fork(current) or parent is None or parent["end_reason"] != "compression":
                    break
                current = parent
            else:
                return ()
            chain = []
            for _ in range(100):
                if current["id"] in chain:
                    return ()
                chain.append(current["id"])
                if current["end_reason"] != "compression":
                    return tuple(chain) if session_id in chain else ()
                children = [r for r in conn.execute(
                    "SELECT * FROM sessions WHERE parent_session_id=?", (current["id"],)) if not fork(r)]
                if len(children) != 1 or children[0]["profile_name"] != current["profile_name"]:
                    return ()
                current = children[0]
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return ()
    return ()


def captain_origin_key(platform: str, chat_id: str) -> str | None:
    """Preserve Desktop keys; namespace WebUI keys across transport families."""
    if not chat_id:
        return None
    if platform == "webui":
        return f"webui:{chat_id}"
    if platform == "tui":
        return chat_id
    return None


# `r` is the Captain registry row. Legacy rows may use an authoritative,
# unambiguous subscription, but never receiver liveness or the current tab.
# Include *all* destinations in the ambiguity check, not only this receiver's.
CAPTAIN_ORIGIN_SQL = """COALESCE(NULLIF(r.origin_session_key, ''), (
    SELECT CASE WHEN COUNT(DISTINCT n.platform || ':' || n.chat_id || ':' || n.thread_id) = 1
                     AND MIN(n.notifier_profile) = r.profile
                     AND COUNT(n.notifier_profile) = COUNT(*)
                     AND MIN(n.platform) IN ('webui', 'tui')
                     AND MIN(n.chat_id) != ''
                THEN CASE WHEN MIN(n.platform) = 'webui'
                          THEN 'webui:' || MIN(n.chat_id)
                          ELSE MIN(n.chat_id) END END
    FROM kanban_notify_subs n WHERE n.task_id = r.task_id
))"""
