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

def _dispatcher_claim_lock(conn: sqlite3.Connection, task_id: str) -> str:
    """Return a host-local claim identity unique to one board task."""
    import hashlib
    database = conn.execute('PRAGMA database_list').fetchall()
    main_path = next((str(row['file']) for row in database if row['name'] == 'main'), '')
    board_identity = str(Path(main_path).resolve()) if main_path else _kb.get_current_board()
    digest = hashlib.sha256(f'{board_identity}\x00{task_id}'.encode()).hexdigest()[:16]
    return f'{_kb._claimer_id()}:{digest}'


def set_enabled_toolsets(conn: sqlite3.Connection, task_id: str, toolsets: Optional[Iterable[str]]) -> bool:
    """Set or clear a task's bounded toolset allowlist."""
    normalized = _kb.normalize_enabled_toolsets(toolsets)
    encoded = json.dumps(normalized) if normalized is not None else None
    with _kb.write_txn(conn):
        cur = conn.execute('UPDATE tasks SET enabled_toolsets = ? WHERE id = ?', (encoded, task_id))
        if cur.rowcount != 1:
            return False
        _kb._append_event(conn, task_id, 'toolsets_override_set', {'enabled_toolsets': normalized}, run_id=None)
        return True


def _worker_identity_guard(task_id: str, expected_identity: Optional[_kb._WorkerIdentity]) -> tuple[str, tuple[Any, ...]]:
    """Build a SQL CAS guard for one snapshotted running owner."""
    if expected_identity is None:
        return ('', ())
    expected_task, run_id, worker_pid, claim_lock = expected_identity
    if expected_task != task_id:
        return (' AND 0', ())
    return (' AND current_run_id IS ? AND worker_pid IS ? AND claim_lock IS ?', (run_id, worker_pid, claim_lock))


def reclaim_task(conn: sqlite3.Connection, task_id: str, *, reason: Optional[str]=None, signal_fn=None) -> bool:
    """Operator reclaim regardless of TTL: release the claim, restore the source
    phase, reset the failure counter. False when the task is not in a
    reclaimable state (not running, or doesn't exist).
    """
    row = conn.execute('SELECT status, claim_lock, worker_pid, worker_started_at, current_run_id FROM tasks WHERE id = ?', (task_id,)).fetchone()
    if not row:
        return False
    if row['status'] != 'running' and row['claim_lock'] is None:
        return False
    if row['claim_lock'] and (not row['worker_pid']):
        return False
    prev_lock = row['claim_lock']
    snapshot_run = row['current_run_id']
    expected_identity: _kb._WorkerIdentity = (task_id, snapshot_run, row['worker_pid'], prev_lock)
    guard_sql, guard_params = _kb._worker_identity_guard(task_id, expected_identity)
    termination = _kb._terminate_reclaimed_worker(row['worker_pid'], prev_lock, signal_fn=signal_fn, started_at=_kb._row_get(row, 'worker_started_at'), scope_expected=_kb._worker_scope_expected(conn, task_id, run_id=snapshot_run), scope_unit=_kb._worker_scope_unit(conn, task_id, run_id=snapshot_run))
    if _kb._worker_survived_termination(termination):
        _kb._defer_reclaim_for_live_worker(conn, task_id, prev_lock, int(time.time()), termination, reason='manual_reclaim_cleanup_incomplete', expected_identity=expected_identity)
        return False
    with _kb.write_txn(conn):
        retry_status = _kb._retry_status_for_run(conn, task_id)
        cur = conn.execute("UPDATE tasks SET status = ?, claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL WHERE id = ? AND status IN ('running', 'ready', 'blocked') AND claim_lock IS ?" + guard_sql, (retry_status, task_id, prev_lock, *guard_params))
        if cur.rowcount != 1:
            return False
        _kb._record_reclaim(conn, task_id, termination, error=f'manual_reclaim: {reason}' if reason else f'manual_reclaim lock={prev_lock}', payload={'manual': True, 'reason': reason, 'prev_lock': prev_lock, 'retry_status': retry_status})
    _kb._clear_failure_counter(conn, task_id)
    return True


def _delete_legacy_governance_relations(conn: sqlite3.Connection, task_id: str) -> None:
    """Delete legacy governance rows referencing task_id or its runs if tables exist."""
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('kanban_invocations', 'kanban_dispositions', 'kanban_task_bindings')").fetchall()
    existing = {r[0] for r in rows}
    if not existing:
        return
    if 'kanban_invocations' in existing:
        conn.execute('DELETE FROM kanban_invocations WHERE task_id = ? OR run_id IN (SELECT id FROM task_runs WHERE task_id = ?)', (task_id, task_id))
    if 'kanban_dispositions' in existing:
        conn.execute('DELETE FROM kanban_dispositions WHERE task_id = ?', (task_id,))
    if 'kanban_task_bindings' in existing:
        conn.execute('DELETE FROM kanban_task_bindings WHERE task_id = ?', (task_id,))


def _running_worker_identity(conn: sqlite3.Connection, task_id: str) -> Optional[_kb._WorkerIdentity]:
    row = conn.execute("SELECT id, current_run_id, worker_pid, claim_lock FROM tasks WHERE id = ? AND status = 'running'", (task_id,)).fetchone()
    if row is None:
        return None
    return (str(row['id']), int(row['current_run_id']) if row['current_run_id'] is not None else None, int(row['worker_pid']) if row['worker_pid'] is not None else None, str(row['claim_lock']) if row['claim_lock'] is not None else None)


def _worker_cleanup_verified(termination: dict) -> bool:
    from hermes_cli.kanban_db_dispatch import _worker_cleanup_verified
    return _worker_cleanup_verified(termination)


def _prepare_running_worker_cleanup(conn: sqlite3.Connection, task_id: str, *, reason: str) -> tuple[bool, Optional[_kb._WorkerIdentity]]:
    """Stop one snapshotted running owner before a non-worker mutation."""
    identity = _kb._running_worker_identity(conn, task_id)
    if identity is None:
        return (True, None)
    _, run_id, worker_pid, claim_lock = identity
    if worker_pid is None:
        return (False, identity)
    row = conn.execute('SELECT worker_started_at FROM task_runs WHERE id = ? AND task_id = ? AND worker_pid IS ?', (run_id, task_id, worker_pid)).fetchone()
    from hermes_cli.kanban_db_dispatch import UNVERIFIED_WORKER_FINGERPRINT
    started_at = row['worker_started_at'] if row is not None else UNVERIFIED_WORKER_FINGERPRINT
    termination = _kb._terminate_reclaimed_worker(worker_pid, claim_lock, started_at=started_at, scope_expected=_kb._worker_scope_expected(conn, task_id, run_id), scope_unit=_kb._worker_scope_unit(conn, task_id, run_id))
    if _kb._worker_cleanup_verified(termination):
        return (True, identity)
    if termination.get('host_local') and termination.get('termination_attempted'):
        _kb._defer_reclaim_for_live_worker(conn, task_id, claim_lock, int(time.time()), termination, reason=reason, expected_identity=identity)
    return (False, identity)


def gc_events(conn: sqlite3.Connection, *, older_than_seconds: int=30 * 24 * 3600) -> int:
    """Prune old done/archived events, retaining decomposition identity until task deletion.

    ``older_than_seconds=0`` means everything older than now; the CLI maps
    ``--event-retention-days 0`` to "disabled" before calling this.
    """
    cutoff = int(time.time()) - _kb._retention_seconds(older_than_seconds)
    with _kb.write_txn(conn):
        conn.execute("DELETE FROM kanban_captain_inbox WHERE state = 'acked' AND updated_at < ?", (cutoff,))
        cur = conn.execute("DELETE FROM task_events WHERE created_at < ? AND kind != 'decomposed' AND task_id IN (SELECT id FROM tasks WHERE status IN ('done', 'archived')) AND NOT EXISTS (SELECT 1 FROM kanban_captain_inbox i WHERE i.event_id = task_events.id AND i.state != 'acked')", (cutoff,))
        conn.execute('DELETE FROM kanban_captain_receivers WHERE last_seen < ?', (cutoff,))
    return int(cur.rowcount or 0)


def gc_worker_logs(*, older_than_seconds: int=30 * 24 * 3600, board: Optional[str]=None) -> int:
    """Delete worker log files older than the cutoff on one board; returns the count.

    ``older_than_seconds=0`` means everything older than now; the CLI maps
    ``--log-retention-days 0`` to "disabled" before calling this.
    """
    older_than_seconds = _kb._retention_seconds(older_than_seconds)
    log_dir = _kb.worker_logs_dir(board=board)
    if not log_dir.exists():
        return 0
    cutoff = time.time() - older_than_seconds
    removed = 0
    for p in log_dir.iterdir():
        with contextlib.suppress(OSError):
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
    return removed


def _profile_home_for_task(assignee: Optional[str]) -> Optional[str]:
    """Best-effort profile home used to validate task-scoped toolsets."""
    if assignee:
        try:
            from hermes_cli.profiles import resolve_profile_env
            return resolve_profile_env(assignee)
        except (FileNotFoundError, ValueError):
            pass
    try:
        from hermes_constants import get_hermes_home
        return str(get_hermes_home())
    except Exception:
        logger.debug('runtime boundary failed', exc_info=True)
        return None


def _available_task_toolset_names(hermes_home: Optional[str]=None) -> set[str]:
    """Return built-in/plugin names plus only the target profile's MCP aliases."""
    names = set(_kb.get_toolset_names())
    try:
        from tools.registry import registry
        aliases = registry.get_registered_toolset_aliases()
        live_mcp_names = {alias for alias, target in aliases.items() if str(target).startswith('mcp-')}
        live_mcp_names.update(str(target) for target in aliases.values() if str(target).startswith('mcp-'))
        names.difference_update(live_mcp_names)
        names = {name for name in names if not str(name).startswith('mcp-')}
    except Exception as exc:
        logger.debug('runtime boundary failed', exc_info=True)
        _kb._log.debug('kanban task toolsets: live MCP inventory unavailable (%s)', exc)
    try:
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        from hermes_cli.config import load_config_readonly
        from hermes_cli.tools_config import enabled_mcp_server_names
        token = set_hermes_home_override(hermes_home) if hermes_home else None
        try:
            names.update(enabled_mcp_server_names(load_config_readonly()))
        finally:
            if token is not None:
                reset_hermes_home_override(token)
    except Exception as exc:
        logger.debug('runtime boundary failed', exc_info=True)
        _kb._log.debug('kanban task toolsets: profile inventory unavailable (%s)', exc)
    return names


def normalize_enabled_toolsets(value: Any, *, hermes_home: Optional[str]=None) -> Optional[list[str]]:
    """Validate and normalize a bounded task-level toolset allowlist."""
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        raise ValueError('enabled_toolsets must be a list of toolset names')
    if len(value) > _kb.MAX_TASK_ENABLED_TOOLSETS:
        raise ValueError(f'enabled_toolsets may contain at most {_kb.MAX_TASK_ENABLED_TOOLSETS} entries')
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, str):
            raise ValueError('enabled_toolsets must contain only strings')
        name = raw.strip()
        if not name:
            raise ValueError('enabled_toolsets entries must not be empty')
        if len(name) > _kb.MAX_TASK_TOOLSET_NAME_CHARS:
            raise ValueError(f'enabled_toolsets names may contain at most {_kb.MAX_TASK_TOOLSET_NAME_CHARS} characters')
        if any(ord(char) < 32 or char == ',' for char in name):
            raise ValueError('enabled_toolsets names contain an invalid character')
        if name in seen:
            continue
        seen.add(name)
        cleaned.append(name)
    available = _kb._available_task_toolset_names(hermes_home)
    unknown = [name for name in cleaned if name not in available]
    if unknown:
        raise ValueError('unknown toolset name(s) in enabled_toolsets: ' + ', '.join(repr(name) for name in unknown))
    missing_required = [name for name in _kb.MANDATORY_TASK_TOOLSETS if name not in available]
    if missing_required:
        raise ValueError('required task toolset(s) unavailable in assignee profile: ' + ', '.join(missing_required))
    return cleaned


def effective_task_toolsets(requested: Optional[Iterable[str]]) -> Optional[list[str]]:
    """Return exact requested order plus stable mandatory worker minimums."""
    if requested is None:
        return None
    effective = list(requested)
    for name in _kb.MANDATORY_TASK_TOOLSETS:
        if name not in effective:
            effective.append(name)
    return effective
