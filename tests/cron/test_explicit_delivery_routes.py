"""Exact-target cross-profile cron transport without credential inheritance."""
from types import SimpleNamespace


import pytest
import yaml

from gateway.config import Platform, PlatformConfig, GatewayConfig
from cron.scheduler_delivery import _resolve_target_transport
from cron.scheduler_provider import InProcessCronScheduler


class OneTick:
    stopped = False

    def is_set(self):
        return self.stopped

    def wait(self, timeout=None):
        self.stopped = True


@pytest.fixture
def route_env(tmp_path, monkeypatch):
    from pathlib import Path
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    home = tmp_path / '.hermes'
    otto = home / 'profiles' / 'otto'
    otto.mkdir(parents=True)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    token = set_hermes_home_override(home)
    config = {'cron': {'delivery_routes': [
        {'platform': 'discord', 'chat_id': '123456789', 'adapter_profile': 'otto'}]}}
    (home / 'config.yaml').write_text(yaml.safe_dump(config))
    adapter = SimpleNamespace(config=PlatformConfig(enabled=True), is_connected=True)
    try:
        yield home, otto, config, adapter
    finally:
        reset_hermes_home_override(token)


def multiplex_view(home, otto, adapter, monkeypatch, *, gate=None):
    import cron.scheduler as scheduler
    views = []
    monkeypatch.setattr(InProcessCronScheduler, 'recover_interrupted', lambda self: 0)
    monkeypatch.setattr(scheduler, 'tick', lambda **kw: views.append(kw['adapters']))
    InProcessCronScheduler().start(
        OneTick(), profile_homes=[('default', home), ('otto', otto)],
        profile_adapters={'otto': {Platform.DISCORD: adapter}},
        adapters={}, default_profile='default', interval=0, profile_gate=gate)
    return views[0]


def test_multiplex_explicit_target_uses_only_selected_active_adapter(route_env, monkeypatch):
    import copy
    import os
    from cron.delivery_routes import delivery_preflight_scope
    from cron.scheduler_preflight import _preflight_check_delivery

    home, otto, cfg, adapter = route_env
    before_env = dict(os.environ)
    gate = {'allowed': True}
    view = multiplex_view(home, otto, adapter, monkeypatch,
                          gate=lambda name, home: name != 'otto' or gate['allowed'])
    job = {'id': 'probe', 'deliver': 'discord:123456789'}
    before_job = copy.deepcopy(job)
    target = {'platform': 'discord', 'chat_id': '123456789'}

    def resolve(target=target):
        return _resolve_target_transport(job, Platform.DISCORD, 'discord', target,
                                         view, GatewayConfig())

    with delivery_preflight_scope(view):
        assert _preflight_check_delivery(job) is None
    resolved, error = resolve()
    assert error is None and resolved[2] is adapter
    for rejected in [dict(target, chat_id='987654321'), dict(target, thread_id='77')]:
        resolved, error = resolve(rejected)
        assert resolved is None and error
    for attr in ['is_connected', 'enabled', 'allowed']:
        owner = adapter if attr == 'is_connected' else adapter.config
        if attr == 'allowed':
            gate['allowed'] = False
        else:
            setattr(owner, attr, False)
        with delivery_preflight_scope(view):
            assert _preflight_check_delivery(job)
        assert resolve()[0] is None
        if attr == 'allowed':
            gate['allowed'] = True
        else:
            setattr(owner, attr, True)
    route = cfg['cron']['delivery_routes'][0]
    for bad in [dict(route, adapter_profile='missing'), dict(route, chat_id='*'),
                dict(route, thread_id='77')]:
        (home / 'config.yaml').write_text(yaml.safe_dump({'cron': {'delivery_routes': [bad]}}))
        assert resolve()[0] is None
        with delivery_preflight_scope(view):
            assert _preflight_check_delivery(job)
    (home / 'config.yaml').write_text(yaml.safe_dump(cfg))
    # A config edit after the one route read must not trigger a second lookup's
    # legacy fallback or select a different profile inside the same attempt.
    import cron.delivery_routes as routing
    real_routes = routing.configured_routes
    other = SimpleNamespace(config=PlatformConfig(enabled=True), is_connected=True)
    view.profiles['other'] = {Platform.DISCORD: other}
    for changed_routes in [[], [dict(route, adapter_profile='other')]]:
        def edit_after_read():
            current = real_routes()
            (home / 'config.yaml').write_text(yaml.safe_dump(
                {'cron': {'delivery_routes': changed_routes}}))
            return current
        with monkeypatch.context() as scoped:
            scoped.setattr(routing, 'configured_routes', edit_after_read)
            resolved, error = resolve()
            assert error is None and resolved[2] is adapter
        (home / 'config.yaml').write_text(yaml.safe_dump(cfg))
    view.profiles['otto'].clear()
    assert resolve()[0] is None
    assert job == before_job and dict(os.environ) == before_env


@pytest.mark.parametrize('revoke_before_drain', [None, 'gate', 'mapping', 'adapter'])
def test_real_worker_handoff_preflight_queue_and_fresh_gateway_drain(
        route_env, monkeypatch, tmp_path, revoke_before_drain):
    import asyncio

    import os
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor
    from agent.secret_scope import is_multiplex_active, set_multiplex_active
    from cron import scheduler, delivery_queue, executions
    from cron.jobs import create_job, get_job, use_cron_store
    from gateway.run_cron_delivery import _drain_restart_safe_cron_deliveries
    from tools import process_registry

    home, otto, cfg, adapter = route_env
    # Fixture-only secret: no real token is loaded or needed by this network-free test.
    (otto / '.env').write_text('DISCORD_BOT_TOKEN=fixture-otto-not-for-worker\n')
    (otto / 'config.yaml').write_text('{}\n')
    cfg['platforms'] = {'discord': {'enabled': False}}
    (home / 'config.yaml').write_text(yaml.safe_dump(cfg))
    script_dir = home / 'scripts'
    script_dir.mkdir()
    ran = tmp_path / 'ran'
    (script_dir / 'probe.py').write_text(
        'import os, pathlib\n'
        'assert not os.getenv("DISCORD_BOT_TOKEN")\n'
        f'pathlib.Path({str(ran)!r}).write_text("once")\n'
        'print("exact-route-completed")\n')
    view = multiplex_view(home, otto, adapter, monkeypatch)
    original_env = dict(os.environ)
    previous_multiplex = is_multiplex_active()
    set_multiplex_active(True)
    # Replace only the OS supervisor wrapper, not launch/payload/adoption/preflight/delivery.
    # Tuple keeps the actual argv, while exercising the managed-worker branch's distinct value.
    monkeypatch.setattr(process_registry, 'restart_safe_gateway_child_argv',
                        lambda argv, **kw: tuple(argv))
    sent = []

    async def send(chat_id, text, metadata=None):
        sent.append((chat_id, text, metadata))
        return {'success': True, 'message_id': 'fixture-message'}

    adapter.send = send
    runner = SimpleNamespace(
        config=SimpleNamespace(multiplex_profiles=True, multiplex_profile_allowlist=['otto']),
        _profile_adapters={'otto': {Platform.DISCORD: adapter}})
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    loop.call_soon(ready.set)
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    assert ready.wait(5)
    try:
        with use_cron_store(home), ThreadPoolExecutor(max_workers=1) as pool:
            job = create_job(prompt=None, schedule='every 1h', name='route probe',
                             script='probe.py', no_agent=True, deliver='discord:123456789')
            before = get_job(job['id'])
            future = pool.submit(scheduler.run_one_job, job, adapters=view, loop=loop)
            deadline = time.monotonic() + 30
            queued = None
            while time.monotonic() < deadline:
                execution = executions.latest_execution(job['id'])
                if execution:
                    queued = delivery_queue.get_status(execution['id'])
                    if queued and queued['status'] == 'pending':
                        break
                if future.done():
                    pytest.fail(f'worker exited before queue: {future.result()}, {execution}')
                time.sleep(0.05)
            assert queued and queued['status'] == 'pending'
            assert ran.read_text() == 'once', 'real worker preflight must accept dispatch evidence'
            if revoke_before_drain == 'gate':
                runner.config.multiplex_profile_allowlist = []
            elif revoke_before_drain == 'mapping':
                (home / 'config.yaml').write_text('{}\n')
            elif revoke_before_drain == 'adapter':
                adapter.is_connected = False
            fallback = SimpleNamespace(config=PlatformConfig(enabled=True), send=send)
            _drain_restart_safe_cron_deliveries({Platform.DISCORD: fallback}, loop, runner)
            assert future.result(timeout=15) is True
            status = delivery_queue.get_status(execution['id'])['status']
            if revoke_before_drain:
                assert status != 'delivered' and sent == []
            else:
                assert status == 'delivered'
                assert len(sent) == 1 and sent[0][0] == '123456789'
                assert 'exact-route-completed' in sent[0][1]
                assert not (sent[0][2] or {}).get('thread_id')
            after = get_job(job['id'])
            for field in ['id', 'schedule', 'prompt', 'deliver', 'script', 'no_agent']:
                assert after[field] == before[field]
            assert not (otto / 'cron' / 'jobs.json').exists()
            assert executions.latest_execution(job['id'])['pid'] != os.getpid()
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()
        set_multiplex_active(previous_multiplex)
    assert all(os.environ.get(key) == value for key, value in original_env.items())
    assert "DISCORD_BOT_TOKEN" not in os.environ


def test_mandatory_route_block_alerts_once_then_rearms_after_recovery(route_env, monkeypatch):
    from cron import scheduler
    from cron.delivery_routes import delivery_preflight_scope
    from cron.jobs import create_job, get_job, use_cron_store
    from cron.scheduler_preflight import BLOCKED_CONFIG_MARKER, BLOCKED_CONFIG_SILENT_MARKER

    home, otto, cfg, adapter = route_env
    cfg['cron']['preflight'] = False
    (home / 'config.yaml').write_text(yaml.safe_dump(cfg))
    scripts = home / 'scripts'
    scripts.mkdir()
    (scripts / 'probe.py').write_text('print("recovered")\n')
    view = multiplex_view(home, otto, adapter, monkeypatch)
    with use_cron_store(home):
        job = create_job(prompt=None, schedule='every 1h', script='probe.py', no_agent=True,
                         deliver='discord:123456789', failure_deliver='local')
        assert scheduler.run_job(job)[3].startswith(BLOCKED_CONFIG_MARKER)
        assert scheduler.run_job(get_job(job['id']))[3].startswith(BLOCKED_CONFIG_SILENT_MARKER)
        with delivery_preflight_scope(view):
            assert scheduler.run_job(get_job(job['id']))[0] is True
        assert not get_job(job['id']).get('preflight_alerted')
        assert scheduler.run_job(get_job(job['id']))[3].startswith(BLOCKED_CONFIG_MARKER)
