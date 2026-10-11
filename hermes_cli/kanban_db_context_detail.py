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


def _ctx_attachments(lines: list[str], attachments: list[_kb.Attachment]) -> None:
    """Absolute on-disk paths so the worker's file tools read them directly
    (remote terminal backends need the attachments dir mounted)."""
    if not attachments:
        return
    lines.append('## Attachments')
    lines.append('Files attached to this task. Read them with the file/terminal tools at the absolute paths below:')
    for att in attachments:
        size_kb = max(1, (att.size + 1023) // 1024) if att.size else 0
        size_str = f', {size_kb} KB' if size_kb else ''
        ctype = f', {att.content_type}' if att.content_type else ''
        lines.append(f'- `{att.filename}`{ctype}{size_str} → `{att.stored_path}`')
    lines.append('')


def _ctx_prior_attempts(lines: list[str], conn: sqlite3.Connection, task_id: str, now: int) -> None:
    """Closed runs on this task (the active run is this worker), newest
    ``_CTX_MAX_PRIOR_ATTEMPTS`` in full, older ones as a one-line marker."""
    all_prior = [r for r in _kb.list_runs(conn, task_id) if r.ended_at is not None]
    shown, omitted_note = _kb._ctx_tail(all_prior, _kb._CTX_MAX_PRIOR_ATTEMPTS, 'attempt')
    if not shown:
        return
    first_shown_idx = len(all_prior) - len(shown) + 1
    lines.append('## Prior attempts on this task')
    if omitted_note:
        lines.append(omitted_note)
    for offset, run in enumerate(shown):
        profile = run.profile or '(unknown)'
        outcome = run.outcome or run.status
        lines.append(f'### Attempt {first_shown_idx + offset} — {outcome} ({profile}, {_kb._ctx_stamp(run.started_at, now)})')
        if run.summary and run.summary.strip():
            lines.append(_kb._ctx_cap(run.summary))
        if run.error and run.error.strip():
            lines.append(f'_error_: {_kb._ctx_cap(run.error)}')
        meta_line = _kb._ctx_metadata_line(run.metadata)
        if meta_line:
            lines.append(meta_line)
        lines.append('')


def _ctx_parent_results(lines: list[str], conn: sqlite3.Connection, task_id: str, now: int) -> None:
    """Done-parent handoffs: newest ``completed`` run's summary+metadata,
    falling back to ``task.result`` for pre-runs-table data. Stamped with a
    relative age so the worker re-verifies stale upstream results."""
    parent_rows = conn.execute('SELECT parent_id FROM task_links WHERE child_id = ? ORDER BY parent_id', (task_id,)).fetchall()
    wrote_header = False
    for pid in (r['parent_id'] for r in parent_rows):
        pt = _kb.get_task(conn, pid)
        if not pt or pt.status != 'done':
            continue
        runs = [r for r in _kb.list_runs(conn, pid) if r.outcome == 'completed']
        runs.sort(key=lambda r: r.started_at, reverse=True)
        run = runs[0] if runs else None
        if not wrote_header:
            lines.append('## Parent task results')
            lines.append("_Handoffs from upstream tasks, captured when each parent completed (see age below). These are point-in-time snapshots, not live state — if a result drives your current work and it's not recent, re-verify against the source before acting on it as current._")
            wrote_header = True
        done_ts = run.ended_at if run is not None and run.ended_at else pt.completed_at or None
        age = _kb._relative_age(done_ts, now)
        lines.append(f'### {pid}' + (f' (completed {age})' if age else ''))
        if run is not None and run.summary and run.summary.strip():
            lines.append(_kb._ctx_cap(run.summary))
        elif pt.result:
            lines.append(_kb._ctx_cap(pt.result))
        else:
            lines.append('(no result recorded)')
        meta_line = _kb._ctx_metadata_line(run.metadata) if run is not None else None
        if meta_line:
            lines.append(meta_line)
        lines.append('')


