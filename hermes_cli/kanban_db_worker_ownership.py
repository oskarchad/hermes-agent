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

import hermes_cli.kanban_db_dispatch as _kbd
from hermes_cli import kanban_db as _kb
from hermes_cli import kanban_db_connect as _kbc


logger = logging.getLogger(__name__)

def _process_fingerprint(pid: int) -> Optional[str]:
    """Restart-stable identity of a live process: ``"<instantiation epoch>|<start time>"``. The start
    time alone (``/proc/<pid>/stat`` field 22 on Linux) is clock ticks since THIS boot, so a row that
    survives a reboot could match an unrelated process with the same PID and the same tick value;
    ``gateway.drain_control.current_instantiation_epoch`` (``boot_id`` + PID-1 start) changes on every
    reboot / container recreate, so the composed value never survives one. ``None`` when unreadable."""
    from gateway.drain_control import current_instantiation_epoch
    from gateway.status import get_process_start_time
    start = get_process_start_time(int(pid))
    if start is None:
        return None
    return f'{current_instantiation_epoch()}|{start}'


def _worker_alive(pid: Optional[int], started_at) -> bool:
    """True when ``pid`` is live AND is still the worker we spawned. ``started_at`` is the fingerprint
    recorded by ``_set_worker_pid``; after a reboot (or any PID recycle) an unrelated process can own
    the number, so bare existence is never enough to extend a claim or to signal. A legacy row without
    a fingerprint keeps the existence answer: killing it is the pre-fingerprint behaviour and the row is
    rewritten with a fingerprint on its next spawn. An UNVERIFIED spawn also keeps the existence answer
    (a claim is never released beside a possibly-live worker) but ``_terminate_reclaimed_worker``
    refuses to signal it."""
    if not _kb._pid_alive(pid):
        return False
    if started_at == _kbd.UNVERIFIED_WORKER_FINGERPRINT:
        return True
    return not _kbd._pid_recycled(pid, started_at)


def _pid_recycled(pid: Optional[int], started_at) -> bool:
    """True when a live ``pid`` is NOT the process fingerprinted at spawn (or the fingerprint can no
    longer be read). Signalling it would hit a stranger. ``None`` fingerprint = legacy row, never
    recycled; the UNVERIFIED marker is always foreign. An integer fingerprint (rows written before the
    boot witness was added) compares the start time only."""
    if started_at is None or not pid:
        return False
    if started_at == _kbd.UNVERIFIED_WORKER_FINGERPRINT:
        return True
    if isinstance(started_at, str) and '|' in started_at:
        return _kbd._process_fingerprint(int(pid)) != started_at
    from gateway.status import _start_times_agree, get_process_start_time
    current = get_process_start_time(int(pid))
    if current is None:
        return True
    try:
        return not _start_times_agree(current, started_at)
    except (TypeError, ValueError):
        return True


def _terminate_reclaimed_worker(pid: Optional[int], claim_lock: Optional[str], *, signal_fn=None, started_at=None, scope_expected: Optional[bool]=None, scope_unit: Optional[str]=None) -> dict[str, Any]:
    """Best-effort host-local worker termination for reclaim paths. ``started_at`` is the spawn-time
    fingerprint: when the live process no longer matches it, the PID was recycled and nothing is
    signalled — the worker is gone, which is what the reclaim wanted (``terminated`` = True). An
    UNVERIFIED spawn (fingerprint capture failed) that is still live is never signalled either, but
    it is reported as surviving (``signal_refused``) so the reclaim holds the claim instead of
    spawning a duplicate beside it."""
    info: dict[str, Any] = {'prev_pid': int(pid) if pid else None, 'host_local': False, 'termination_attempted': False, 'terminated': False, 'sigkill': False}
    req_fn = getattr(_kb, '_systemd_worker_scope_required', _kbd._systemd_worker_scope_required)
    info.update(scope_expected=req_fn() if scope_expected is None else bool(scope_expected), scope_stop_attempted=False, scope_stopped=False, scope_unit=scope_unit)
    if not pid or pid <= 0 or (not claim_lock):
        return info
    if not str(claim_lock).startswith(_kb._host_prefix()):
        return info
    info['host_local'] = True
    kill = _kbd._kill_fn(signal_fn)
    if kill is None:
        return info
    if started_at == _kbd.UNVERIFIED_WORKER_FINGERPRINT:
        info['signal_refused'] = True
        info['terminated'] = not _kb._pid_alive(pid)
        if info['terminated']:
            info['termination_attempted'] = True
            return _kbd._finish_worker_scope_cleanup(info, scope_unit)
        info['cleanup_verified'] = False
        return info
    if _kb._pid_alive(pid) and _kbd._pid_recycled(pid, started_at):
        info['terminated'] = True
        info['pid_recycled'] = True
        info['termination_attempted'] = True
        return _kbd._finish_worker_scope_cleanup(info, scope_unit)
    info['termination_attempted'] = True
    if signal_fn is None:
        from hermes_cli.kanban_db_worker_cleanup import _terminate_native_worker_tree
        _terminate_native_worker_tree(int(pid), started_at, info)
        info['terminated'] = not _kbd._worker_alive(pid, started_at)
        return _kbd._finish_worker_scope_cleanup(info, scope_unit)
    try:
        kill(int(pid), signal.SIGTERM)
    except ProcessLookupError:
        info['terminated'] = True
        return _kbd._finish_worker_scope_cleanup(info, scope_unit)
    except OSError:
        return info
    if _kbd._poll_worker_exit(pid, started_at):
        info['terminated'] = True
        return _kbd._finish_worker_scope_cleanup(info, scope_unit)
    if _kbd._worker_alive(pid, started_at):
        if not _kbd._sigkill(kill, pid):
            return info
        info['sigkill'] = True
    info['terminated'] = not _kbd._worker_alive(pid, started_at)
    return _kbd._finish_worker_scope_cleanup(info, scope_unit)


def _finish_worker_scope_cleanup(info: dict, scope_unit: Optional[str]) -> dict:
    """Verify owned descendants after the parent has had its native shutdown grace."""
    if info.get('host_local') and info.get('termination_attempted') and info.get('scope_expected'):
        info['scope_stop_attempted'] = True
        stop = getattr(_kb, '_stop_kanban_worker_scope', _kbd._stop_kanban_worker_scope)
        info['scope_stopped'] = bool(scope_unit) and bool(stop(str(scope_unit)))
        if info['scope_stopped']:
            info['termination_target'] = 'systemd_scope'
            info['terminated'] = True
    info['cleanup_verified'] = bool(info.get('scope_stopped') if info.get('scope_expected') else info.get('terminated') and info.get('group_cleanup_verified', True))
    return info


def _live_worker_owner(conn, task_id, run_id, claim_lock=None):
    row = conn.execute("SELECT t.worker_pid AS task_pid, t.worker_started_at AS task_start, "
        "r.worker_pid AS run_pid, r.worker_started_at AS run_start, t.claim_lock "
        "FROM tasks t JOIN task_runs r ON r.id = t.current_run_id "
        "WHERE t.id = ? AND t.status = 'running' AND t.current_run_id = ? "
        "AND r.task_id = t.id AND r.status = 'running' AND r.ended_at IS NULL "
        "AND t.claim_lock = r.claim_lock", (task_id, run_id)).fetchone()
    if row is None or not row['claim_lock']:
        return None
    if claim_lock is not None and row['claim_lock'] != claim_lock:
        return None
    return row


def _registered_worker_belongs_to_spawn(pid, worker_pid, started_at):
    """Verify a same-PID spawn, its live descendant, or its exact private scope member."""
    if started_at == _kbd.UNVERIFIED_WORKER_FINGERPRINT or _kbd._process_fingerprint(worker_pid) != started_at:
        return False
    spawn_start = getattr(pid, 'started_at', None) or _kbd._process_fingerprint(int(pid))
    if int(pid) == worker_pid:
        return spawn_start == started_at
    if not spawn_start or _kbd._process_fingerprint(int(pid)) != spawn_start:
        return False
    scope = getattr(pid, 'scope_unit', None)
    if scope and sys.platform == 'linux':
        try:
            parts = Path(f'/proc/{worker_pid}/cgroup').read_text(encoding='utf-8').split('/')
            if scope in {part.strip() for part in parts}:
                return True
        except OSError:
            logger.debug('worker scope membership unavailable', exc_info=True)
    try:
        import psutil
        return any(parent.pid == int(pid) for parent in psutil.Process(worker_pid).parents())
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return False


def _published_worker(conn, task_id, run_id, owner, pid):
    """An adopted PID is authoritative only with its exact registration receipt and identity."""
    worker_pid, started_at = owner['task_pid'], owner['task_start']
    if worker_pid != owner['run_pid'] or started_at != owner['run_start'] or not worker_pid:
        return None
    receipt = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND run_id = ? "
        "AND kind = 'worker_registered' ORDER BY id DESC LIMIT 1", (task_id, run_id)).fetchone()
    try:
        payload = json.loads(receipt['payload']) if receipt else {}
    except (TypeError, ValueError):
        return None
    if payload.get('pid') != worker_pid or payload.get('started_at') != started_at:
        return None
    scope = getattr(pid, 'scope_unit', None)
    if scope and scope != _kbd._kanban_worker_scope_unit(task_id, run_id):
        return None
    if not _registered_worker_belongs_to_spawn(pid, worker_pid, started_at):
        return None
    return int(worker_pid), started_at


def _set_worker_pid(conn: sqlite3.Connection, task_id: str, pid: int, *, expected_run_id: Optional[int]=None, expected_claim_lock: Optional[str]=None) -> bool:
    """Publish the spawned PID or reconcile its already registered child under the same ownership CAS."""
    if expected_run_id is None or expected_claim_lock is None:
        row = conn.execute("SELECT current_run_id, claim_lock FROM tasks WHERE id = ? AND status = 'running'", (task_id,)).fetchone()
        if row is None:
            return False
        expected_run_id, expected_claim_lock = row['current_run_id'], row['claim_lock']
    if expected_run_id is None or expected_claim_lock is None:
        return False
    with _kb.write_txn(conn):
        owner = _live_worker_owner(conn, task_id, expected_run_id, expected_claim_lock)
        if owner is None:
            return False
        if owner['task_pid'] is None and owner['run_pid'] is None:
            worker_pid = int(pid)
            started_at = getattr(pid, 'started_at', None) or _kbd._process_fingerprint(worker_pid) or _kbd.UNVERIFIED_WORKER_FINGERPRINT
            conn.execute('UPDATE tasks SET worker_pid = ?, worker_started_at = ? WHERE id = ?', (worker_pid, started_at, task_id))
            conn.execute('UPDATE task_runs SET worker_pid = ?, worker_started_at = ? WHERE id = ?', (worker_pid, started_at, expected_run_id))
        else:
            adopted = _published_worker(conn, task_id, expected_run_id, owner, pid)
            if adopted is None:
                return False
            worker_pid, started_at = adopted
        payload = {'pid': worker_pid, 'started_at': started_at}
        if worker_pid != int(pid):
            payload['launcher_pid'] = int(pid)
        for name in ('isolation_mode', 'scope_unit'):
            if value := getattr(pid, name, None):
                payload[name] = value
        _kb._append_event(conn, task_id, 'spawned', payload, run_id=expected_run_id)
        return True


def adopt_worker_pid(conn: sqlite3.Connection, task_id: str, run_id: int, pid: int) -> bool:
    """Register before the first model call only while both task and run still own the same live claim."""
    started_at = _kbd._process_fingerprint(int(pid)) or _kbd.UNVERIFIED_WORKER_FINGERPRINT
    with _kb.write_txn(conn):
        owner = _live_worker_owner(conn, task_id, int(run_id))
        if owner is None or not str(owner['claim_lock']).startswith(_kb._host_prefix()):
            return False
        if owner['task_pid'] is None and owner['run_pid'] is None:
            conn.execute('UPDATE tasks SET worker_pid = ?, worker_started_at = ? WHERE id = ?', (int(pid), started_at, task_id))
            conn.execute('UPDATE task_runs SET worker_pid = ?, worker_started_at = ? WHERE id = ?', (int(pid), started_at, int(run_id)))
            _kb._append_event(conn, task_id, 'worker_registered', {'pid': int(pid), 'started_at': started_at}, run_id=int(run_id))
            return True
        return owner['task_pid'] == owner['run_pid'] and owner['task_start'] == owner['run_start']


def _validate_task_toolsets_for_spawn(raw_value: Any, *, assignee: Optional[str]) -> tuple[Optional[list[str]], Optional[str]]:
    """Validate one persisted allowlist without leaking raw values to events."""
    if raw_value is None:
        return (None, None)
    try:
        parsed = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
    except Exception:
        logger.debug('runtime boundary failed', exc_info=True)
        return (None, 'malformed_json')
    try:
        requested = _kb.normalize_enabled_toolsets(parsed, hermes_home=_kb._profile_home_for_task(assignee))
    except ValueError as exc:
        message = str(exc)
        if 'unknown toolset' in message:
            return (None, 'unknown_toolset')
        if 'required task toolset' in message:
            return (None, 'required_toolset_unavailable')
        return (None, 'malformed_value')
    return (_kb.effective_task_toolsets(requested), None)


def _audit_and_block_invalid_task_toolsets(conn: sqlite3.Connection, task_id: str, *, expected_status: str, expected_assignee: Optional[str], expected_enabled_toolsets: Optional[str], expected_claim_lock: Optional[str], expected_run_id: Optional[int], expected_worker_pid: Optional[int]) -> bool:
    """CAS-block an unchanged invalid task before any process is spawned."""
    reason = 'Invalid enabled_toolsets configuration; edit or clear the task-level toolset override before retrying.'
    with _kb.write_txn(conn):
        current = conn.execute('SELECT status, assignee, enabled_toolsets, claim_lock, current_run_id, worker_pid FROM tasks WHERE id = ?', (task_id,)).fetchone()
        if current is None or any((current['status'] != expected_status, current['assignee'] != expected_assignee, current['enabled_toolsets'] != expected_enabled_toolsets, current['claim_lock'] != expected_claim_lock, current['current_run_id'] != expected_run_id, current['worker_pid'] != expected_worker_pid)):
            return False
        if any(value is not None for value in (expected_claim_lock, expected_run_id, expected_worker_pid)):
            return False
        _effective, reason_code = _kbd._validate_task_toolsets_for_spawn(current['enabled_toolsets'], assignee=current['assignee'])
        if reason_code is None:
            return False
        updated = conn.execute("UPDATE tasks SET status = 'blocked', claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, current_run_id = NULL, block_kind = 'capability', block_recurrences = CASE WHEN block_kind = 'capability' THEN block_recurrences + 1 ELSE 1 END WHERE id = ? AND status = ? AND assignee IS ? AND enabled_toolsets IS ? AND claim_lock IS ? AND current_run_id IS ? AND worker_pid IS ?", (task_id, expected_status, expected_assignee, expected_enabled_toolsets, expected_claim_lock, expected_run_id, expected_worker_pid))
        if updated.rowcount != 1:
            return False
        _kb._append_event(conn, task_id, 'toolsets_validation_failed', {'field': 'enabled_toolsets', 'reason_code': reason_code})
        _kb._append_event(conn, task_id, 'blocked', {'reason': reason, 'kind': 'capability'})
    _kb.notify_task_updated(conn, task_id, ('status', 'block_kind'))
    return True


def _signal_owned_worker(pid: int, sig: int, *, signal_fn=None) -> str:
    """Signal one worker's owned process group, falling back safely to its PID."""
    if signal_fn is not None:
        signal_fn(int(pid), sig)
        return 'pid'
    if os.name != 'nt' and hasattr(os, 'killpg'):
        try:
            pgid = os.getpgid(int(pid))
            if pgid == int(pid):
                os.killpg(pgid, sig)  # windows-footgun: ok
                return 'process_group'
        except (ProcessLookupError, PermissionError, OSError):
            pass
    os.kill(int(pid), sig)
    return 'pid'


def _worker_cleanup_verified(termination: dict) -> bool:
    """Return whether one exact host-local worker boundary is inactive."""
    cleanup_verified = termination.get('cleanup_verified')
    if cleanup_verified is None:
        if termination.get('scope_expected'):
            return bool(termination.get('scope_stopped'))
        return bool(termination.get('terminated'))
    return bool(cleanup_verified)


def _handoff_worker_teardown_pending(conn: sqlite3.Connection, task_id: str) -> bool:
    """Return whether the source worker from a same-card handoff still exists."""
    prior = conn.execute('SELECT id, outcome FROM task_runs WHERE task_id = ? AND ended_at IS NOT NULL ORDER BY id DESC LIMIT 1', (task_id,)).fetchone()
    if prior is None or prior['outcome'] not in {'review_requested', 'changes_requested'}:
        return False
    run_id = int(prior['id'])
    spawned = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND run_id = ? AND kind = 'spawned' ORDER BY id DESC LIMIT 1", (task_id, run_id)).fetchone()
    try:
        payload = json.loads(spawned['payload']) if spawned is not None and spawned['payload'] else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    exp_fn = getattr(_kb, '_worker_scope_expected', _kbd._worker_scope_expected)
    if exp_fn(conn, task_id, run_id=run_id):
        unit_fn = getattr(_kb, '_worker_scope_unit', _kbd._worker_scope_unit)
        scope_unit = unit_fn(conn, task_id, run_id=run_id)
        if scope_unit is None:
            return True
        unloaded_fn = getattr(_kb, '_systemd_worker_scope_unloaded', _kbd._systemd_worker_scope_unloaded)
        return not unloaded_fn(scope_unit)
    worker_pid = payload.get('pid')
    if not isinstance(worker_pid, int) or worker_pid <= 0:
        return False
    pid_alive_fn = getattr(_kb, '_pid_alive', _kb._pid_alive)
    return pid_alive_fn(worker_pid)


def _worker_identity_guard(task_id: str, expected_identity: Optional[_kbd._WorkerIdentity]) -> tuple[str, tuple[Any, ...]]:
    """Build a SQL CAS guard for one snapshotted running owner."""
    guard_fn = getattr(_kb, '_worker_identity_guard', None)
    if guard_fn is not None:
        return guard_fn(task_id, expected_identity)
    if expected_identity is None:
        return ('', ())
    expected_task, run_id, worker_pid, claim_lock = expected_identity
    if expected_task != task_id:
        return (' AND 0', ())
    return (' AND current_run_id IS ? AND worker_pid IS ? AND claim_lock IS ?', (run_id, worker_pid, claim_lock))


def _worker_identity_from_row(row: sqlite3.Row) -> _kbd._WorkerIdentity:
    return (str(row['id']), int(row['current_run_id']) if row['current_run_id'] is not None else None, int(row['worker_pid']) if row['worker_pid'] is not None else None, str(row['claim_lock']) if row['claim_lock'] is not None else None)


def _record_spawn_cleanup_failure(conn: sqlite3.Connection, task: _kb.Task, pid: int, termination: dict[str, Any]) -> None:
    """Attach a failed post-CAS cleanup diagnostic to the displaced run."""
    run_id = task.current_run_id
    if run_id is None:
        return
    with _kb.write_txn(conn):
        original = conn.execute('SELECT 1 FROM task_runs WHERE id = ? AND task_id = ?', (int(run_id), task.id)).fetchone()
        current_task = conn.execute('SELECT 1 FROM tasks WHERE id = ?', (task.id,)).fetchone()
        if original is None or current_task is None:
            return
        payload: dict[str, Any] = {'pid': int(pid)}
        payload.update(termination)
        _kb._append_event(conn, task.id, 'spawn_cleanup_failed', payload, run_id=int(run_id))


def _stop_spawn_after_pid_persistence_loss(conn: sqlite3.Connection, task: _kb.Task, pid: int) -> bool:
    """Stop a displaced spawn without touching its replacement owner."""
    try:
        isolation_mode = getattr(pid, 'isolation_mode', None)
        req_fn = getattr(_kb, '_systemd_worker_scope_required', _kbd._systemd_worker_scope_required)
        scope_expected = isolation_mode == 'systemd_scope' if isolation_mode in {'systemd_scope', 'process_session'} else req_fn()
        term_fn = getattr(_kb, '_terminate_reclaimed_worker', _kbd._terminate_reclaimed_worker)
        termination = term_fn(int(pid), task.claim_lock, started_at=getattr(pid, 'started_at', None) or _kbd.UNVERIFIED_WORKER_FINGERPRINT, scope_expected=scope_expected, scope_unit=getattr(pid, 'scope_unit', None))
        if _kbd._worker_cleanup_verified(termination):
            return True
    except Exception as exc:
        logger.debug('runtime boundary failed', exc_info=True)
        termination = {'cleanup_verified': False, 'cleanup_error': str(exc)[:500]}
    _kb._log.error('kanban dispatch: spawned worker cleanup could not be verified after PID persistence CAS loss for task %s run %s pid %s', task.id, task.current_run_id, int(pid))
    try:
        _kbd._record_spawn_cleanup_failure(conn, task, pid, termination)
    except Exception:
        logger.debug('runtime boundary failed', exc_info=True)
        _kb._log.exception('kanban dispatch: failed to persist PID CAS-loss cleanup diagnostic for task %s run %s', task.id, task.current_run_id)
    return False


def _systemd_worker_scope_required() -> bool:
    """Return whether this dispatcher is owned by a systemd service cgroup."""
    if _kb._IS_WINDOWS or sys.platform != 'linux' or (not os.environ.get('INVOCATION_ID')):
        return False
    from tools.process_registry import _is_supervised_gateway_process
    return bool(_is_supervised_gateway_process())


def _kanban_worker_scope_unit(task_id: str, run_id: Optional[int]) -> str:
    """Name of the transient scope a managed worker runs in."""
    return f'hermes-worker-kanban-{task_id}-run-{run_id}.scope'


def _stop_kanban_worker_scope(unit_name: str) -> bool:
    """Stop and verify the exact transient scope an owned worker runs in."""
    from tools.process_registry import _stop_systemd_unit
    inactive_fn = getattr(_kb, '_systemd_worker_scope_inactive', _kbd._systemd_worker_scope_inactive)
    return bool(_stop_systemd_unit(unit_name) and inactive_fn(unit_name))


def _systemd_worker_scope_state(unit_name: str) -> Optional[dict[str, str]]:
    """Read a transient user scope's load/active state, if determinable."""
    import shutil
    binary = shutil.which('systemctl')
    if binary is None:
        return None
    try:
        result = subprocess.run([binary, '--user', 'show', unit_name, '--property=LoadState', '--property=ActiveState'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15, check=False)
    except Exception:
        logger.debug('runtime boundary failed', exc_info=True)
        return None
    if result.returncode != 0:
        error = f'{result.stdout}\n{result.stderr}'.lower()
        if any(marker in error for marker in ('not loaded', 'not found', 'does not exist')):
            return {'LoadState': 'not-found', 'ActiveState': 'inactive'}
        return None
    states = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition('=')
        if separator:
            states[key] = value
    return states


def _systemd_worker_scope_inactive(unit_name: str) -> bool:
    """Verify that a transient user scope no longer owns live processes."""
    states = _kbd._systemd_worker_scope_state(unit_name)
    if states is None:
        return False
    return states.get('LoadState') == 'not-found' or states.get('ActiveState') in {'inactive', 'failed'}


def _systemd_worker_scope_unloaded(unit_name: str) -> bool:
    """Return whether systemd has released a transient worker scope name."""
    states = _kbd._systemd_worker_scope_state(unit_name)
    if states is None:
        return False
    return states.get('LoadState') == 'not-found'


def _worker_scope_expected(conn: sqlite3.Connection, task_id: str, run_id: Optional[int]=None) -> bool:
    """Resolve one run's persisted worker-isolation contract."""
    if run_id is None:
        row = conn.execute("SELECT e.payload FROM tasks t LEFT JOIN task_events e ON e.task_id = t.id   AND e.run_id = t.current_run_id AND e.kind = 'spawned' WHERE t.id = ? ORDER BY e.id DESC LIMIT 1", (task_id,)).fetchone()
    else:
        row = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND run_id = ? AND kind = 'spawned' ORDER BY id DESC LIMIT 1", (task_id, int(run_id))).fetchone()
    if row is not None and row['payload']:
        try:
            isolation_mode = json.loads(row['payload']).get('isolation_mode')
        except (TypeError, ValueError, json.JSONDecodeError):
            isolation_mode = None
        if isolation_mode == 'systemd_scope':
            return True
        if isolation_mode == 'process_session':
            return False
    req_fn = getattr(_kb, '_systemd_worker_scope_required', _kbd._systemd_worker_scope_required)
    return bool(req_fn())


def _worker_scope_unit(conn: sqlite3.Connection, task_id: str, run_id: Optional[int]=None) -> Optional[str]:
    """Resolve the transient scope unit for one run, or None when no scope is expected."""
    exp_fn = getattr(_kb, '_worker_scope_expected', _kbd._worker_scope_expected)
    if not exp_fn(conn, task_id, run_id=run_id):
        return None
    if run_id is None:
        row = conn.execute("SELECT e.payload AS payload, t.current_run_id AS run_id FROM tasks t LEFT JOIN task_events e ON e.task_id = t.id   AND e.run_id = t.current_run_id AND e.kind = 'spawned' WHERE t.id = ? ORDER BY e.id DESC LIMIT 1", (task_id,)).fetchone()
    else:
        row = conn.execute("SELECT payload, run_id FROM task_events WHERE task_id = ? AND run_id = ? AND kind = 'spawned' ORDER BY id DESC LIMIT 1", (task_id, int(run_id))).fetchone()
    persisted = None
    resolved_run = run_id
    if row is not None:
        if row['run_id'] is not None:
            resolved_run = int(row['run_id'])
        if row['payload']:
            try:
                persisted = json.loads(row['payload']).get('scope_unit')
            except (TypeError, ValueError, json.JSONDecodeError):
                persisted = None
    if resolved_run is None:
        return None
    unit_fn = getattr(_kb, '_kanban_worker_scope_unit', _kbd._kanban_worker_scope_unit)
    expected = unit_fn(task_id, resolved_run)
    if isinstance(persisted, str) and persisted and (persisted != expected):
        _kb._log.debug('kanban: ignoring persisted scope unit %r for %s run %s (expected %r)', persisted, task_id, resolved_run, expected)
    return expected
