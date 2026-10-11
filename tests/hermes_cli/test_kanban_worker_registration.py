"""Parent/worker publication must reconcile only the same live claimed spawn."""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from tests.hermes_cli import test_kanban_db as native

kanban_home = native.kanban_home


def _claimed(conn):
    task_id = kb.create_task(conn, title='register race', assignee='alice')
    task = kb.claim_task(conn, task_id)
    assert task is not None
    return task


def test_registered_worker_before_parent_is_reconciled(kanban_home, monkeypatch):
    monkeypatch.setattr(kbd, '_process_fingerprint', lambda pid: f'epoch|{pid}')
    with kbc.connect() as conn:
        task = _claimed(conn)
        assert kbd.adopt_worker_pid(conn, task.id, task.current_run_id, 4321)
        assert kbd._set_worker_pid(conn, task.id, 4321,
            expected_run_id=task.current_run_id, expected_claim_lock=task.claim_lock)
        row = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = 'spawned'", (task.id,)).fetchone()
        assert json.loads(row[0])['pid'] == 4321


def test_registered_child_keeps_canonical_pid(kanban_home, monkeypatch):
    monkeypatch.setattr(kbd, '_process_fingerprint', lambda pid: f'epoch|{pid}')
    from hermes_cli import kanban_db_worker_ownership as owner
    monkeypatch.setattr(owner, '_registered_worker_belongs_to_spawn', lambda *args: True, raising=False)
    with kbc.connect() as conn:
        task = _claimed(conn)
        assert kbd.adopt_worker_pid(conn, task.id, task.current_run_id, 4322)
        wrapper = kb._SpawnedWorkerPid(4321, isolation_mode='systemd_scope',
            scope_unit=kb._kanban_worker_scope_unit(task.id, task.current_run_id))
        assert kbd._set_worker_pid(conn, task.id, wrapper,
            expected_run_id=task.current_run_id, expected_claim_lock=task.claim_lock)
        assert kb.get_task(conn, task.id).worker_pid == 4322
        payload = json.loads(conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = 'spawned'", (task.id,)).fetchone()[0])
        assert payload['pid'] == 4322
        assert payload['launcher_pid'] == 4321
        assert payload['scope_unit'] == wrapper.scope_unit


@pytest.mark.parametrize('mismatch', ['run_pid', 'run_claim', 'run_done', 'task_claim', 'missing_receipt'])
def test_registration_publication_refuses_inconsistent_owner(kanban_home, monkeypatch, mismatch):
    monkeypatch.setattr(kbd, '_process_fingerprint', lambda pid: f'epoch|{pid}')
    with kbc.connect() as conn:
        task = _claimed(conn)
        assert kbd.adopt_worker_pid(conn, task.id, task.current_run_id, 4321)
        changes = {
            'run_pid': ("UPDATE task_runs SET worker_pid = 999 WHERE id = ?", task.current_run_id),
            'run_claim': ("UPDATE task_runs SET claim_lock = 'replacement' WHERE id = ?", task.current_run_id),
            'run_done': ("UPDATE task_runs SET status = 'done', ended_at = 12 WHERE id = ?", task.current_run_id),
            'task_claim': ("UPDATE tasks SET claim_lock = 'replacement' WHERE id = ?", task.id),
            'missing_receipt': ("DELETE FROM task_events WHERE kind = 'worker_registered' AND run_id = ?", task.current_run_id),
        }
        sql, value = changes[mismatch]
        conn.execute(sql, (value,))
        conn.commit()
        assert not kbd._set_worker_pid(conn, task.id, 4321,
            expected_run_id=task.current_run_id, expected_claim_lock=task.claim_lock)
        assert not conn.execute("SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'spawned'", (task.id,)).fetchone()


@pytest.mark.parametrize('change', ["status = 'done', ended_at = 12", "claim_lock = 'replacement'", "task_id = 'different'"])
def test_worker_adoption_refuses_stale_or_corrupt_run(kanban_home, monkeypatch, change):
    monkeypatch.setattr(kbd, '_process_fingerprint', lambda pid: f'epoch|{pid}')
    with kbc.connect() as conn:
        task = _claimed(conn)
        if change.startswith('task_id'):
            other = kb.create_task(conn, title='different', assignee='alice')
            conn.execute('UPDATE task_runs SET task_id = ? WHERE id = ?', (other, task.current_run_id))
        else:
            conn.execute(f'UPDATE task_runs SET {change} WHERE id = ?', (task.current_run_id,))
        conn.commit()
        assert not kbd.adopt_worker_pid(conn, task.id, task.current_run_id, 4321)
        assert kb.get_task(conn, task.id).worker_pid is None


def test_non_descendant_and_recycled_launcher_are_not_adopted(monkeypatch):
    from hermes_cli import kanban_db_worker_ownership as owner
    pid = kb._SpawnedWorkerPid(4000, isolation_mode='process_session', scope_unit=None, started_at='epoch|old')
    monkeypatch.setattr(kbd, '_process_fingerprint', lambda value: 'epoch|new' if value == 4000 else 'epoch|child')
    assert not owner._registered_worker_belongs_to_spawn(pid, 4001, 'epoch|child')
    assert not owner._registered_worker_belongs_to_spawn(pid, 4001, 'epoch|wrong')
    # Real host process ancestry proves an unrelated live process is not this launcher's child.
    import os
    pid = kb._SpawnedWorkerPid(os.getpid(), isolation_mode='process_session', scope_unit=None, started_at='parent')
    monkeypatch.setattr(kbd, '_process_fingerprint', lambda value: 'parent' if value == os.getpid() else 'ancestor')
    assert not owner._registered_worker_belongs_to_spawn(pid, os.getppid(), 'ancestor')


def test_recycled_worker_still_requires_owned_scope_cleanup(monkeypatch):
    stopped = []
    monkeypatch.setattr(kb, '_pid_alive', lambda pid: True)
    monkeypatch.setattr(kbd, '_pid_recycled', lambda *a: True)
    monkeypatch.setattr(kb, '_stop_kanban_worker_scope', lambda unit: stopped.append(unit) or False)
    result = kb._terminate_reclaimed_worker(4321, kb._host_prefix() + 'claim',
        started_at='epoch|old', scope_expected=True, scope_unit='owned.scope',
        signal_fn=lambda *a: pytest.fail('recycled PID must not be signalled'))
    assert stopped == ['owned.scope']
    assert result['pid_recycled']
    assert not result['cleanup_verified']
