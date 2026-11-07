"""Legacy task caps survive the move to gateway-owned Kanban workers."""
from contextlib import closing
from dataclasses import asdict
import json
from types import SimpleNamespace

import pytest

from gateway.session_contract import SessionActor
from gateway.session_kanban import build_kanban_policy
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
from hermes_state_runtime import RuntimeStoreError


@pytest.fixture
def task_policy(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    monkeypatch.delenv('HERMES_DELEGATED_CHILD', raising=False)
    monkeypatch.setattr('hermes_cli.profiles.resolve_profile_env', lambda name: str(tmp_path))
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    actor = SessionActor('owner', str(tmp_path), frozenset({'session:create'}), 'transport')
    connection = SimpleNamespace(authority=SimpleNamespace(profile_id=str(tmp_path)), actor=actor, native_owner=True)
    config = {'model': {'default': 'test-model', 'provider': 'custom'},
              'platform_toolsets': {'cli': ['terminal', 'file', 'web']},
              'mcp_servers': {'context7': {'command': 'unused'}, 'unrelated': {'command': 'unused'}},
              'agent': {'disabled_toolsets': ['context7', 'mcp-context7', 'kanban']}}
    with closing(connect()) as conn:
        tid = kb.create_task(conn, title='Bounded task', assignee='default',
                             workspace_kind='dir', workspace_path=str(workspace))
        yield conn, tid, connection, config


def _claim_policy(conn, tid, connection, config, *, review=False):
    if review:
        assert kb.request_review(conn, tid, summary='Ready for review')
        task = kb.claim_review_task(conn, tid)
    else:
        task = kb.claim_task(conn, tid)
    params = dict(board='default', task_id=tid, run_id=task.current_run_id, claim_lock=task.claim_lock)
    return build_kanban_policy(connection, params, config)[0]


@pytest.mark.parametrize('review', [False, True])
@pytest.mark.parametrize('cap', [[], ['terminal'], ['terminal', 'file']])
def test_existing_task_caps_freeze_exact_surface_and_preserve_history(task_policy, cap, review):
    conn, tid, connection, config = task_policy
    conn.execute('ALTER TABLE tasks ADD COLUMN enabled_toolsets TEXT')
    encoded = json.dumps(cap)
    conn.execute('UPDATE tasks SET enabled_toolsets=? WHERE id=?', (encoded, tid))
    conn.commit()
    policy = _claim_policy(conn, tid, connection, config, review=review)
    assert set(policy.toolsets) == set(cap) | {'kanban', 'context7'}
    assert asdict(kb.get_task(conn, tid))['enabled_toolsets'] == cap
    assert 'unrelated' not in policy.toolsets
    assert not ({'kanban', 'context7', 'mcp-context7'} & set(policy.config()['agent']['disabled_toolsets']))
    assert config['agent']['disabled_toolsets'] == ['context7', 'mcp-context7', 'kanban']
    assert conn.execute('SELECT enabled_toolsets FROM tasks WHERE id=?', (tid,)).fetchone()[0] == encoded
    assert json.loads(policy.request_json)['toolsets'] == list(policy.toolsets)
    assert kb.block_task(conn, tid, reason='Operator pause')
    assert kb.unblock_task(conn, tid)
    assert kb.get_task(conn, tid).enabled_toolsets == cap


@pytest.mark.parametrize('has_column', [False, True])
def test_missing_or_null_cap_preserves_native_profile_inheritance(task_policy, has_column):
    conn, tid, connection, config = task_policy
    if has_column:
        conn.execute('ALTER TABLE tasks ADD COLUMN enabled_toolsets TEXT')
    policy = _claim_policy(conn, tid, connection, config)
    assert {'terminal', 'file', 'web', 'kanban', 'unrelated'} <= set(policy.toolsets)
    assert asdict(kb.get_task(conn, tid))['enabled_toolsets'] is None
    assert 'toolsets' not in json.loads(policy.request_json)


@pytest.mark.parametrize('raw', ['not JSON', '{}', '"terminal"', '[3]', '[""]', '["unknown-secret-value"]'])
def test_corrupt_caps_cannot_fall_back_to_profile_tools(task_policy, raw):
    conn, tid, connection, config = task_policy
    task = kb.claim_task(conn, tid)
    conn.execute('ALTER TABLE tasks ADD COLUMN enabled_toolsets TEXT')
    conn.execute('UPDATE tasks SET enabled_toolsets=? WHERE id=?', (raw, tid))
    conn.commit()
    params = dict(board='default', task_id=tid, run_id=task.current_run_id, claim_lock=task.claim_lock)
    with pytest.raises(RuntimeStoreError) as exc:
        build_kanban_policy(connection, params, config)
    assert raw not in str(exc.value)


def test_missing_mandatory_context7_refuses_capped_worker(task_policy):
    conn, tid, connection, config = task_policy
    conn.execute('ALTER TABLE tasks ADD COLUMN enabled_toolsets TEXT')
    conn.execute("UPDATE tasks SET enabled_toolsets='[]' WHERE id=?", (tid,))
    conn.commit()
    config['mcp_servers'].pop('context7')
    with pytest.raises(RuntimeStoreError):
        _claim_policy(conn, tid, connection, config)


@pytest.mark.parametrize('cap', [[], ['terminal']])
def test_real_model_schema_obeys_cap_and_mandatory_tools(task_policy, monkeypatch, cap):
    from model_tools import get_tool_definitions, handle_function_call
    from tools.registry import registry
    from toolsets import resolve_toolset

    conn, tid, connection, config = task_policy
    conn.execute('ALTER TABLE tasks ADD COLUMN enabled_toolsets TEXT')
    conn.execute('UPDATE tasks SET enabled_toolsets=? WHERE id=?', (json.dumps(cap), tid))
    conn.commit()
    probes = {'context7': 'mcp__context7__bounded_probe', 'unrelated': 'mcp__unrelated__bounded_probe'}
    for server, name in probes.items():
        registry.register(name=name, toolset='mcp-' + server,
                          schema={'name': name, 'description': 'Local cap probe',
                                  'parameters': {'type': 'object', 'properties': {}}},
                          handler=lambda *_args, **_kwargs: '{}')
        monkeypatch.setitem(registry._toolset_aliases, server, 'mcp-' + server)
    try:
        policy = _claim_policy(conn, tid, connection, config)
        definitions = get_tool_definitions(enabled_toolsets=list(policy.toolsets),
            disabled_toolsets=policy.config()['agent']['disabled_toolsets'], quiet_mode=True)
        names = {item['function']['name'] for item in definitions}
        allowed = {name for toolset in [*cap, 'kanban', 'context7'] for name in resolve_toolset(toolset)}
        # Native progressive disclosure wraps the same scoped catalog; it must
        # never grant a second path to a profile's otherwise excluded tools.
        assert names <= allowed | {'tool_search', 'tool_describe', 'tool_call'}
        raw = get_tool_definitions(enabled_toolsets=list(policy.toolsets),
            disabled_toolsets=policy.config()['agent']['disabled_toolsets'], quiet_mode=True,
            skip_tool_search_assembly=True)
        raw_names = {item['function']['name'] for item in raw}
        assert raw_names <= allowed
        assert {'kanban_show', 'kanban_complete', probes['context7']} <= raw_names
        assert probes['unrelated'] not in names
        assert ('terminal' in raw_names) == ('terminal' in cap)
        scope = {'enabled_toolsets': list(policy.toolsets),
                 'disabled_toolsets': policy.config()['agent']['disabled_toolsets']}
        described = json.loads(handle_function_call('tool_describe', {'names': list(probes.values())}, **scope))
        assert probes['context7'] in described['tools']
        assert probes['unrelated'] not in described['tools']
        rejected = json.loads(handle_function_call('tool_call',
            {'calls': [{'name': probes['unrelated'], 'arguments': {}}]}, **scope))
        assert 'error' in rejected
        assert json.loads(handle_function_call('tool_call',
            {'calls': [{'name': probes['context7'], 'arguments': {}}]}, **scope)) == {}
    finally:
        for name in probes.values():
            registry.deregister(name)
