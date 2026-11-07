"""The canonical cron owner revalidates live delivery admission before execution."""
import json
from types import SimpleNamespace

import pytest
import hermes_yaml as yaml

from gateway import run, session_cron
from gateway.config import Platform, PlatformConfig
from gateway.session_authority import LiveSession, SessionAuthority
from gateway.session_contract import SessionRef
from hermes_state import SessionDB
from hermes_state_runtime import admit_session_input, begin_runtime_epoch, claim_session_input


@pytest.mark.asyncio
@pytest.mark.parametrize('admitted', [True, False, 'rebound'])
async def test_canonical_owner_checks_live_binding_before_job_side_effects(tmp_path, monkeypatch, admitted):
    from cron import scheduler
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    home = tmp_path / 'root'
    other = home / 'profiles' / 'otto'
    other.mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(home))
    (home / 'config.yaml').write_text(yaml.safe_dump({'cron': {'delivery_routes': [
        {'platform': 'discord', 'chat_id': 'channel', 'adapter_profile': 'otto'}]}}))
    adapter = SimpleNamespace(config=PlatformConfig(enabled=True), is_connected=True)
    runner = SimpleNamespace(_draining=False, config=SimpleNamespace(
        multiplex_profiles=True, multiplex_profile_allowlist=['otto'] if admitted else []),
        _profile_adapters={'otto': {Platform.DISCORD: adapter}})
    monkeypatch.setattr(run, '_handoff_watch_scopes', lambda _: [(None, None), ('otto', other)])
    binding = {'platform': 'discord', 'chat_id': 'channel', 'adapter_profile': 'otto', 'available': True}
    if admitted == 'rebound':
        runner.config.multiplex_profile_allowlist.append('other')
        runner._profile_adapters['other'] = {Platform.DISCORD: adapter}
        monkeypatch.setattr(run, '_handoff_watch_scopes', lambda _: [(None, None), ('otto', other), ('other', other)])
        (home / 'config.yaml').write_text(yaml.safe_dump({'cron': {'delivery_routes': [
            {'platform': 'discord', 'chat_id': 'channel', 'adapter_profile': 'other'}]}}))
    side_effects = []

    def prepare(*args):
        side_effects.append('started')
        return (True, 'doc', 'answer', None), None

    monkeypatch.setattr(scheduler, '_prepare_job_prompt', prepare)
    token = set_hermes_home_override(home)
    db = SessionDB(home / 'state.db')
    db.create_session('session', source='cron')
    authority = SessionAuthority(runner, profile_id=str(home), instance_id='test', db=db,
                                 epoch=begin_runtime_epoch(db, instance_id='test'))
    authority.sessions['session'] = LiveSession(SimpleNamespace(platform=None, user_id='cron-owner'), 'route')
    try:
        admit_session_input(db, epoch=authority.epoch, principal_id='cron-owner', session_id='session',
                            request_id='cron:job:fire', payload={'text': ''})
        row = claim_session_input(db, epoch=authority.epoch, session_id='session')
        policy = SimpleNamespace(request_json=json.dumps({
            'extra_prompt': None, 'request_id': 'cron:job:fire',
            'cron_job': {'id': 'job', 'deliver': 'discord:channel', 'prompt': 'do work',
                         '_cron_delivery_route_bindings': [binding]}}))
        if admitted is True:
            assert await session_cron.execute(authority, SessionRef(str(home), 'session'), row, policy) == 'answer'
            assert side_effects == ['started']
            flags = authority.pending_results[row['admission_id']]['result']['cron_job_flags']
            assert flags['_cron_delivery_route_bindings'] == [binding]
        else:
            with pytest.raises(RuntimeError, match='explicit cron delivery route'):
                await session_cron.execute(authority, SessionRef(str(home), 'session'), row, policy)
            assert side_effects == []
    finally:
        db.close()
        reset_hermes_home_override(token)


def test_canonical_admission_freezes_server_owned_route_and_rejects_rpc_override(tmp_path, monkeypatch):
    import asyncio
    from cron import jobs
    from gateway.session import SessionStore
    from gateway.config import GatewayConfig
    from hermes_state_runtime import RuntimeStoreError
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    other = tmp_path / 'profiles' / 'otto'
    other.mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    route = {'platform': 'discord', 'chat_id': 'channel', 'adapter_profile': 'otto'}
    (tmp_path / 'config.yaml').write_text(yaml.safe_dump({'cron': {'delivery_routes': [route]}}))
    monkeypatch.setattr(jobs, 'get_job', lambda jid: {'id': jid, 'prompt': 'work', 'deliver': 'discord:channel', 'model': 'fixture-model'})
    monkeypatch.setattr(run, '_load_gateway_config', lambda: {'model': {}, 'platform_toolsets': {'cli': []}})
    monkeypatch.setattr(run, '_handoff_watch_scopes', lambda _: [(None, None), ('otto', other)])
    adapter = SimpleNamespace(config=PlatformConfig(enabled=True), is_connected=True)
    runner = SimpleNamespace(_draining=False, session_store=SessionStore(tmp_path / 'sessions', GatewayConfig()),
        adapters={}, config=SimpleNamespace(multiplex_profiles=True, multiplex_profile_allowlist=['otto']),
        _profile_adapters={'otto': {Platform.DISCORD: adapter}})
    token = set_hermes_home_override(tmp_path)
    db = SessionDB(tmp_path / 'state.db')
    authority = SessionAuthority(runner, profile_id=str(tmp_path), instance_id='test', db=db,
                                 epoch=begin_runtime_epoch(db, instance_id='test'))
    runner.session_authority = authority
    monkeypatch.setattr(authority, '_schedule', lambda ref: None)

    async def probe():
        import hashlib
        digest = hashlib.sha256(json.dumps([route], sort_keys=True).encode()).hexdigest()
        params = {'job_id': 'job', 'request_id': 'fire', 'extra_prompt': None,
                  'expected_delivery_route_digest': digest}
        with pytest.raises(RuntimeStoreError, match='invalid_params'):
            await session_cron.operation(authority, 'submit', dict(params, delivery_route_preflight=[route]))
        with pytest.raises(RuntimeStoreError, match='changed since dispatch'):
            await session_cron.operation(authority, 'submit', dict(params, expected_delivery_route_digest='0' * 64))
        receipt = await session_cron.operation(authority, 'submit', params)
        policy = runner.adapters[Platform.LOCAL].policies[receipt['session_id']]
        frozen = json.loads(policy.request_json)['cron_job']['_cron_delivery_route_bindings']
        assert frozen == [{**route, 'available': True}]
        (tmp_path / 'config.yaml').write_text('{}')
        assert await session_cron.operation(authority, 'submit', params) == receipt
        assert json.loads(policy.request_json)['cron_job']['_cron_delivery_route_bindings'] == frozen

    try:
        asyncio.run(probe())
    finally:
        db.close()
        reset_hermes_home_override(token)
