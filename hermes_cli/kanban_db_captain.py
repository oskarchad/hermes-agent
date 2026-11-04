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


logger = logging.getLogger(__name__)

def _normalize_captain_profile(profile: Optional[str]) -> str:
    """Canonicalize a Captain owner profile.

    Uses the same :func:`hermes_cli.profiles.normalize_profile_name` canonical
    id used on disk and in ``-p`` argv, so mixed-case / title-cased inputs
    (``Otto``, ``Default``) resolve to one owner (``otto``, ``default``) across
    registration, materialization, lease/read, filters, and probes. An empty
    profile maps to ``default``.
    """
    name = str(profile or '').strip()
    if not name:
        return 'default'
    try:
        from hermes_cli.profiles import normalize_profile_name
        return normalize_profile_name(name)
    except Exception:
        logger.debug('runtime boundary failed', exc_info=True)
        return name.lower()


def resolve_captain_profile() -> str:
    """Resolve the configured logical Captain for a newly rooted task tree."""
    try:
        from hermes_cli.config import load_config
        from hermes_cli import profiles as profiles_mod
        cfg = load_config() or {}
        kanban_cfg = cfg.get('kanban', {}) if isinstance(cfg, dict) else {}
        explicit = str(kanban_cfg.get('orchestrator_profile') or '').strip()
        if explicit and profiles_mod.profile_exists(explicit):
            return _kb._normalize_captain_profile(explicit)
    except Exception:
        logger.debug('runtime boundary failed', exc_info=True)
    return 'default'


def _resolve_captain_ownership(conn: sqlite3.Connection, parents: tuple[str, ...], *, requested_profile: Optional[str], requested_origin: Optional[str], tenant: Optional[str]) -> tuple[str, Optional[str], Optional[str]]:
    """Resolve one immutable Captain owner for a root or descendant task."""
    parent_rows: list[sqlite3.Row] = []
    if parents:
        placeholders = ','.join('?' * len(parents))
        parent_rows = conn.execute(f'SELECT task_id, profile, origin_session_key, tenant FROM kanban_captain_registry WHERE task_id IN ({placeholders}) ORDER BY task_id', parents).fetchall()
    if parent_rows:
        profiles = {str(row['profile']) for row in parent_rows}
        origins = {str(row['origin_session_key']) for row in parent_rows if row['origin_session_key']}
        tenants = {str(row['tenant']) for row in parent_rows if row['tenant']}
        if len(profiles) != 1:
            raise ValueError('parent tasks have conflicting Captain profiles')
        if len(origins) > 1:
            raise ValueError('parent tasks have conflicting Captain origins')
        if len(tenants) > 1:
            raise ValueError('parent tasks have conflicting Captain tenants')
        inherited_tenant = next(iter(tenants), None)
        return (next(iter(profiles)), next(iter(origins), None), str(tenant) if tenant else inherited_tenant)
    profile = requested_profile or _kb.resolve_captain_profile()
    return (_kb._normalize_captain_profile(profile), requested_origin, tenant)


def _classify_captain_signal(body: str) -> Optional[str]:
    """Recognize a strategic Captain signal header at the start of a comment."""
    stripped = body.strip()
    for header, signal_class in _kb.CAPTAIN_SIGNAL_HEADERS:
        if stripped.startswith(header):
            tail = stripped[len(header):]
            if not tail or tail[0] in (':', '-', '—', ' ', '\n', '\t'):
                return signal_class
    return None


def materialize_captain_signal(conn: sqlite3.Connection, *, task_id: str, comment_id: int, author: str, signal_class: str) -> Optional[int]:
    """Materialize one durable Captain report for an immutable comment source."""
    if signal_class not in _kb.CAPTAIN_SIGNAL_CLASSES:
        raise ValueError(f'unknown Captain signal class {signal_class!r}')
    with _kb.write_txn(conn, allow_nested=True):
        comment = conn.execute('SELECT 1 FROM task_comments WHERE id = ? AND task_id = ?', (int(comment_id), task_id)).fetchone()
        if comment is None:
            raise ValueError(f'unknown comment {comment_id} for task {task_id}')
        if conn.execute('SELECT 1 FROM kanban_captain_registry WHERE task_id = ?', (task_id,)).fetchone() is None:
            return None
        existing = conn.execute('SELECT event_id FROM kanban_captain_inbox WHERE source_comment_id = ?', (int(comment_id),)).fetchone()
        if existing is not None:
            return int(existing['event_id'])
        return _kb._append_event(conn, task_id, 'captain_signal', {'author': author.strip(), 'comment_id': int(comment_id), 'signal_class': signal_class}, source_comment_id=int(comment_id))


def get_comment(conn: sqlite3.Connection, comment_id: int) -> Optional[_kb.Comment]:
    """Return one immutable comment source by its board-local id."""
    row = conn.execute('SELECT id, task_id, author, body, created_at FROM task_comments WHERE id = ?', (int(comment_id),)).fetchone()
    return _kb.Comment.from_row(row) if row else None


def register_captain_owner(conn: sqlite3.Connection, task_id: str, *, profile: str, origin_session_key: Optional[str]=None, tenant: Optional[str]=None) -> None:
    """Register ``task_id`` in the Captain ledger owned by ``profile``.

    Called AFTER create so the task's ``created`` event (already appended)
    never materializes a reportable row. ``origin_session_key`` is the exact
    TUI/Desktop session key when the creator is such a session; ``None`` for
    gateway/CLI/cron/unattached creators (any active same-profile TUI/Desktop
    session may then claim). Idempotent on ``task_id``: the owner profile is
    fixed at first registration; a later call only refreshes an
    origin_session_key that was previously unset.
    """
    prof = _kb._normalize_captain_profile(profile)
    origin = str(origin_session_key).strip() if origin_session_key else None
    if tenant is None:
        task_row = conn.execute('SELECT tenant FROM tasks WHERE id = ?', (task_id,)).fetchone()
        tenant = task_row['tenant'] if task_row is not None else None
    tenant = str(tenant).strip() if tenant else None
    now = int(time.time())
    with _kb.write_txn(conn, allow_nested=True):
        conn.execute('INSERT OR IGNORE INTO kanban_captain_registry (task_id, profile, origin_session_key, tenant, created_at) VALUES (?, ?, ?, ?, ?)', (task_id, prof, origin, tenant, now))
        if origin:
            conn.execute("UPDATE kanban_captain_registry SET origin_session_key = ? WHERE task_id = ? AND (origin_session_key IS NULL OR origin_session_key = '')", (origin, task_id))
        if origin:
            conn.execute('INSERT INTO kanban_captain_receivers (profile, session_key, tenant, last_seen) VALUES (?, ?, ?, ?) ON CONFLICT(profile, session_key) DO UPDATE SET tenant = excluded.tenant, last_seen = excluded.last_seen', (prof, origin, tenant, now))


def touch_captain_receiver(conn: sqlite3.Connection, *, profile: str, session_key: str, tenant: Optional[str], now: Optional[int]=None) -> None:
    """Publish a bounded cross-process heartbeat for one Captain receiver."""
    prof = _kb._normalize_captain_profile(profile)
    key = str(session_key or '').strip()
    if not key:
        return
    tenant_value = str(tenant).strip() if tenant else None
    seen = int(time.time()) if now is None else int(now)
    with _kb.write_txn(conn, allow_nested=True):
        conn.execute('INSERT INTO kanban_captain_receivers (profile, session_key, tenant, last_seen) VALUES (?, ?, ?, ?) ON CONFLICT(profile, session_key) DO UPDATE SET tenant = excluded.tenant, last_seen = excluded.last_seen', (prof, key, tenant_value, seen))


def captain_receiver_is_live(conn: sqlite3.Connection, *, profile: str, session_key: str, now: Optional[int]=None, max_age_seconds: int=15) -> bool:
    """Return whether ``session_key`` has a fresh durable receiver heartbeat."""
    prof = _kb._normalize_captain_profile(profile)
    key = str(session_key or '').strip()
    if not key:
        return False
    current = int(time.time()) if now is None else int(now)
    row = conn.execute('SELECT 1 FROM kanban_captain_receivers WHERE profile = ? AND session_key = ? AND last_seen >= ?', (prof, key, current - max(1, int(max_age_seconds)))).fetchone()
    return row is not None


def read_captain_candidates(conn: sqlite3.Connection, *, profile: str, now: Optional[int]=None, limit: int=32, receiver_session_key: Optional[str]=None, receiver_max_age_seconds: int=15) -> list[dict]:
    """Return claimable Captain rows for ``profile`` (pending or lease-expired).

    Each row carries the registered ``origin_session_key`` for formatting and
    diagnostics. Cross-process liveness is read from the durable receiver
    table here and rechecked inside ``lease_captain_reports`` so a stale
    candidate read cannot steal from an origin that refreshed meanwhile.
    """
    prof = _kb._normalize_captain_profile(profile)
    now = int(time.time()) if now is None else int(now)
    eligibility_sql = ''
    eligibility_params: tuple = ()
    if receiver_session_key is not None:
        session_key = str(receiver_session_key)
        eligibility_sql = " AND (   r.origin_session_key = ?    OR (i.tenant IS NULL AND (       r.origin_session_key IS NULL OR r.origin_session_key = ''        OR (r.origin_session_key != ? AND NOT EXISTS (           SELECT 1 FROM kanban_captain_receivers cr            WHERE cr.profile = i.profile              AND cr.session_key = r.origin_session_key              AND cr.last_seen >= ?       )))   ) )"
        eligibility_params = (session_key, session_key, now - max(1, int(receiver_max_age_seconds)))
    rows = conn.execute("SELECT i.event_id, i.task_id, i.kind, i.state, i.lease_expires,        r.origin_session_key, i.tenant FROM kanban_captain_inbox i LEFT JOIN kanban_captain_registry r ON r.task_id = i.task_id WHERE i.profile = ? AND (  i.state = 'pending' OR   (i.state = 'leased' AND (i.lease_expires IS NULL OR i.lease_expires < ?)))" + eligibility_sql + ' ORDER BY i.event_id ASC LIMIT ?', (prof, now, *eligibility_params, max(1, min(int(limit), 128)))).fetchall()
    return [dict(r) for r in rows]


def lease_captain_reports(conn: sqlite3.Connection, *, profile: str, owner: str, event_ids: Iterable[int], now: Optional[int]=None, lease_seconds: int=120, receiver_session_key: Optional[str]=None, receiver_max_age_seconds: int=15) -> tuple[str, list[_kb.Event]]:
    """Atomically lease the given ``event_ids`` for ``profile`` under ``owner``.

    Returns ``(token, events)``. Only rows currently ``pending`` or holding an
    expired lease flip to ``leased`` under a freshly minted opaque token, so
    concurrent same-profile pollers never both win the same row. When a
    receiver key is supplied, origin liveness and tenant fail-closed rules are
    rechecked in the same write transaction as the lease. The returned events
    are joined against ``task_events`` so the caller can format them.
    """
    prof = _kb._normalize_captain_profile(profile)
    ids = list(dict.fromkeys(int(e) for e in event_ids))[:128]
    token = secrets.token_hex(16)
    if not ids:
        return (token, [])
    now = int(time.time()) if now is None else int(now)
    expires = now + int(lease_seconds)
    placeholders = ','.join('?' * len(ids))
    eligibility_sql = ''
    eligibility_params: tuple = ()
    if receiver_session_key is not None:
        receiver = str(receiver_session_key)
        eligibility_sql = " AND EXISTS (   SELECT 1 FROM kanban_captain_registry r    WHERE r.task_id = kanban_captain_inbox.task_id AND (     r.origin_session_key = ?      OR (kanban_captain_inbox.tenant IS NULL AND (       r.origin_session_key IS NULL OR r.origin_session_key = '' OR (         r.origin_session_key != ? AND NOT EXISTS (           SELECT 1 FROM kanban_captain_receivers cr            WHERE cr.profile = kanban_captain_inbox.profile              AND cr.session_key = r.origin_session_key              AND cr.last_seen >= ?         )       )     ))   ) )"
        eligibility_params = (receiver, receiver, now - max(1, int(receiver_max_age_seconds)))
    with _kb.write_txn(conn):
        conn.execute(f"UPDATE kanban_captain_inbox SET state = 'leased', lease_token = ?, lease_owner = ?,     lease_expires = ?, updated_at = ? WHERE profile = ? AND event_id IN ({placeholders}) AND (  state = 'pending' OR   (state = 'leased' AND (lease_expires IS NULL OR lease_expires < ?)))" + eligibility_sql, (token, owner, expires, now, prof, *ids, now, *eligibility_params))
        rows = conn.execute('SELECT i.event_id, e.task_id, e.kind, e.payload, e.created_at, e.run_id FROM kanban_captain_inbox i JOIN task_events e ON e.id = i.event_id WHERE i.lease_token = ? ORDER BY i.event_id ASC', (token,)).fetchall()
    events: list[_kb.Event] = []
    for r in rows:
        try:
            payload = json.loads(r['payload']) if r['payload'] else None
        except Exception:
            logger.debug('runtime boundary failed', exc_info=True)
            payload = None
        events.append(_kb.Event(id=int(r['event_id']), task_id=r['task_id'], kind=r['kind'], payload=payload, created_at=r['created_at'], run_id=int(r['run_id']) if r['run_id'] is not None else None))
    return (token, events)


def renew_captain_reports(conn: sqlite3.Connection, *, token: str, owner: str, lease_seconds: int=120, now: Optional[int]=None) -> int:
    """Extend a live lease only while ``token`` still belongs to ``owner``.

    Expired leases are never resurrected: once the owner's fence lapses, a
    contender may already have replaced the opaque token. The caller must treat
    a zero rowcount as lost ownership and must not publish that turn's result.
    """
    current = int(time.time()) if now is None else int(now)
    expires = current + max(1, int(lease_seconds))
    with _kb.write_txn(conn):
        cur = conn.execute("UPDATE kanban_captain_inbox SET lease_expires = ?, updated_at = ? WHERE lease_token = ? AND lease_owner = ? AND state = 'leased' AND lease_expires >= ?", (expires, current, token, owner, current))
    return int(cur.rowcount or 0)


def ack_captain_reports(conn: sqlite3.Connection, *, token: str, owner: Optional[str]=None) -> int:
    """Mark a leased batch ``acked`` after verified transcript persistence."""
    now = int(time.time())
    owner_sql = ' AND lease_owner = ?' if owner is not None else ''
    params = (now, token, now, owner) if owner is not None else (now, token, now)
    with _kb.write_txn(conn):
        cur = conn.execute("UPDATE kanban_captain_inbox SET state = 'acked', updated_at = ? WHERE lease_token = ? AND state = 'leased' AND lease_expires >= ?" + owner_sql, params)
    return int(cur.rowcount or 0)


def ack_captain_reports_with_reply(conn: sqlite3.Connection, *, token: str, owner: str, reply_author: str, reply_body: str, reply_task_ids: Iterable[str]) -> int:
    """Atomically append a Captain reply and ack its fenced signal lease.

    A lost response after commit is safe: replaying the old token sees no leased
    rows and appends no second comment. Any late comment or ack failure rolls
    the whole operation back, keeping the inbox row retryable.
    """
    if not owner:
        raise ValueError('Captain reply settlement requires a lease owner')
    if not reply_author or not reply_author.strip():
        raise ValueError('Captain reply author is required')
    if not reply_body or not reply_body.strip():
        raise ValueError('Captain reply body is required')
    allowed_tasks = {str(task_id) for task_id in reply_task_ids if task_id}
    now = int(time.time())
    with _kb.write_txn(conn):
        rows = conn.execute("SELECT task_id, kind FROM kanban_captain_inbox WHERE lease_token = ? AND lease_owner = ? AND state = 'leased' AND lease_expires >= ? ORDER BY event_id", (token, owner, now)).fetchall()
        if not rows:
            return 0
        leased_signal_tasks = {str(row['task_id']) for row in rows if row['kind'] == 'captain_signal'}
        if not leased_signal_tasks.issubset(allowed_tasks):
            raise RuntimeError('Captain reply signal task identity does not match lease')
        signal_tasks = {str(row['task_id']) for row in rows if row['kind'] == 'captain_signal' and row['task_id'] in allowed_tasks}
        for task_id in sorted(signal_tasks):
            _kb.add_comment(conn, task_id, author=reply_author.strip(), body=reply_body.strip(), _materialize_captain_signal=False)
        cur = conn.execute("UPDATE kanban_captain_inbox SET state = 'acked', updated_at = ? WHERE lease_token = ? AND lease_owner = ? AND state = 'leased' AND lease_expires >= ?", (now, token, owner, now))
        if int(cur.rowcount or 0) != len(rows):
            raise RuntimeError('Captain reply settlement lost its fenced lease')
        return int(cur.rowcount or 0)


def release_captain_reports(conn: sqlite3.Connection, *, token: str, owner: Optional[str]=None) -> int:
    """Release a leased batch back to ``pending`` after a failed delivery."""
    now = int(time.time())
    owner_sql = ' AND lease_owner = ?' if owner is not None else ''
    params = (now, token, owner) if owner is not None else (now, token)
    with _kb.write_txn(conn):
        cur = conn.execute("UPDATE kanban_captain_inbox SET state = 'pending', lease_token = NULL, lease_owner = NULL,     lease_expires = NULL, updated_at = ? WHERE lease_token = ? AND state = 'leased'" + owner_sql, params)
    return int(cur.rowcount or 0)


def captain_unreported_for_task(conn: sqlite3.Connection, task_id: str) -> int:
    """Count not-yet-acked (pending or leased) Captain rows for a task."""
    row = conn.execute("SELECT COUNT(*) FROM kanban_captain_inbox WHERE task_id = ? AND state != 'acked'", (task_id,)).fetchone()
    return int(row[0]) if row else 0


def purge_captain_task(conn: sqlite3.Connection, task_id: str) -> None:
    """Drop a task's Captain registration and all its inbox rows."""
    with _kb.write_txn(conn, allow_nested=True):
        conn.execute('DELETE FROM kanban_captain_inbox WHERE task_id = ?', (task_id,))
        conn.execute('DELETE FROM kanban_captain_registry WHERE task_id = ?', (task_id,))


def captain_gc_task_if_settled(conn: sqlite3.Connection, task_id: str) -> bool:
    """Purge a task's Captain rows once it is archived AND fully reported.

    Archive must not discard an earlier pending completion: while unreported
    work remains the registration/inbox rows are kept so the poller can still
    deliver them. Returns True when a purge happened.
    """
    with _kb.write_txn(conn):
        task = conn.execute('SELECT status FROM tasks WHERE id = ?', (task_id,)).fetchone()
        if task is None or task['status'] != 'archived':
            return False
        if conn.execute("SELECT 1 FROM kanban_captain_inbox WHERE task_id = ? AND state != 'acked' LIMIT 1", (task_id,)).fetchone() is not None:
            return False
        conn.execute('DELETE FROM kanban_captain_inbox WHERE task_id = ?', (task_id,))
        conn.execute('DELETE FROM kanban_captain_registry WHERE task_id = ?', (task_id,))
        return True


def count_captain_pending(db_path: Optional[Path]=None, *, board: Optional[str]=None, profile: str) -> int:
    """Read-only probe: not-yet-acked Captain rows for ``profile`` on a board.

    Mirrors :func:`count_notify_subs` — never creates the DB, never opens it
    writable, and treats a missing DB or legacy schema (no inbox table) as
    zero. Lets the TUI poller skip opening a board writable when its profile
    has no Captain work waiting there.
    """
    prof = _kb._normalize_captain_profile(profile)
    path = db_path if db_path is not None else _kb.kanban_db_path(board=board)
    if not path.exists():
        return 0
    conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        try:
            row = conn.execute("SELECT COUNT(*) FROM kanban_captain_inbox WHERE profile = ? AND state != 'acked'", (prof,)).fetchone()
        except sqlite3.OperationalError as exc:
            if 'no such table' in str(exc).lower():
                return 0
            raise
        return int(row[0]) if row else 0
    finally:
        conn.close()
