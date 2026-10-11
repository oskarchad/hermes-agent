"""Retained runtime contracts, separated from the native facade."""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import secrets
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Optional

import hermes_cli.kanban_db as _kb


def _ctx_role_history(lines: list[str], conn: sqlite3.Connection, task: _kb.Task, now: int) -> None:
    """The assignee's 5 most recent completed runs on OTHER tasks — implicit
    role continuity without wiring anything into SOUL.md / MEMORY.md."""
    if not task.assignee:
        return
    role_rows = conn.execute("SELECT t.id, t.title, r.summary, r.ended_at FROM task_runs r JOIN tasks t ON r.task_id = t.id WHERE r.profile = ? AND r.task_id != ?   AND r.outcome = 'completed' ORDER BY r.ended_at DESC LIMIT 5", (task.assignee, task.id)).fetchall()
    if not role_rows:
        return
    lines.append(f'## Recent work by @{task.assignee}')
    for row in role_rows:
        first = _kb._first_line(row['summary'], 200) or '(no summary)'
        lines.append(f"- {row['id']} — {row['title']} ({_kb._ctx_stamp(int(row['ended_at']), now)}): {first}")
    lines.append('')


def _ctx_comments(lines: list[str], comments: list[_kb.Comment], now: int) -> None:
    """Newest ``_CTX_MAX_COMMENTS`` comments. The explicit "comment from
    worker" framing stops an operator-controlled HERMES_PROFILE like
    "hermes-system" being read as a system directive above an
    attacker-influenceable body (defense-in-depth)."""
    shown, omitted_note = _kb._ctx_tail(comments, _kb._CTX_MAX_COMMENTS, 'comment')
    if not shown:
        return
    lines.append('## Comment thread')
    if omitted_note:
        lines.append(omitted_note)
    for c in shown:
        safe_author = (c.author or '').replace('`', '')
        lines.append(f'comment from worker `{safe_author}` at {_kb._ctx_stamp(c.created_at, now)}:')
        lines.append(_kb._ctx_cap(c.body, _kb._CTX_MAX_COMMENT_BYTES))
        lines.append('')


def board_stats(conn: sqlite3.Connection) -> dict:
    """Per-status + per-assignee counts and the oldest ``ready`` age (staleness signal)."""
    by_status: dict[str, int] = {}
    for row in conn.execute("SELECT status, COUNT(*) AS n FROM tasks WHERE status != 'archived' GROUP BY status"):
        by_status[row['status']] = int(row['n'])
    by_assignee = _kb._counts_by_assignee(conn)
    oldest_row = conn.execute("SELECT MIN(created_at) AS ts FROM tasks WHERE status = 'ready'").fetchone()
    now = int(time.time())
    oldest_ready_age = now - int(oldest_row['ts']) if oldest_row and oldest_row['ts'] is not None else None
    cap_row = conn.execute("SELECT COUNT(*) AS n, MIN(created_at) AS oldest FROM kanban_captain_inbox WHERE state != 'acked'").fetchone()
    cap_count = int(cap_row['n']) if cap_row and cap_row['n'] is not None else 0
    cap_oldest = now - int(cap_row['oldest']) if cap_row and cap_row['oldest'] is not None else None
    per_profile: list[dict] = []
    for row in conn.execute("SELECT profile, COUNT(*) AS n, MIN(created_at) AS oldest FROM kanban_captain_inbox WHERE state != 'acked' GROUP BY profile ORDER BY n DESC, oldest ASC, profile ASC"):
        per_profile.append({'profile': row['profile'], 'count': int(row['n']), 'oldest_age_seconds': now - int(row['oldest']) if row['oldest'] is not None else None})
    profiles_truncated = max(0, len(per_profile) - _kb.CAPTAIN_STATS_PROFILE_CAP)
    by_profile = per_profile[:_kb.CAPTAIN_STATS_PROFILE_CAP]
    return {'by_status': by_status, 'by_assignee': by_assignee, 'oldest_ready_age_seconds': oldest_ready_age, 'captain_unreported': {'count': cap_count, 'oldest_age_seconds': cap_oldest, 'by_profile': by_profile, 'profiles_truncated': profiles_truncated}, 'now': now}


def read_worker_log(task_id: str, *, tail_bytes: Optional[int]=None, board: Optional[str]=None) -> Optional[str]:
    """Worker log text (last ``tail_bytes`` when set); None when the file is missing."""
    path = _kb.worker_log_path(task_id, board=board)
    if not path.exists():
        return None
    try:
        if tail_bytes is None:
            return path.read_text(encoding='utf-8-sig', errors='replace')
        size = path.stat().st_size
        with open(path, 'rb') as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
                probe = f.tell()
                if not f.readline().endswith(b'\n') and f.tell() >= size:
                    f.seek(probe)
            return f.read().decode('utf-8', errors='replace')
    except OSError:
        return None


