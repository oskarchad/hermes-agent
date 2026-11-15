"""The canonical dispatcher admits the delivery-route digest the cron firer sends.

Regression: ``run_canonical_job`` adds ``expected_delivery_route_digest`` to ``cron.submit`` /
``cron.recover`` and ``session_cron`` accepts it, but the declared ``CronRunParams`` did not, so a
restart-safe worker (which reaches the owner over RPC) got ``4001 invalid_params`` for every agent
job, gave the fire up as unknown and exited 1.
"""
from types import SimpleNamespace

import pytest

from gateway.session_authority import SessionAuthority
from gateway.session_controls import AuthorityConnection
from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['cron.submit', 'cron.recover'])
async def test_route_digest_reaches_the_cron_handler(tmp_path, method):
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        epoch = begin_runtime_epoch(db, instance_id='current')
        authority = SessionAuthority(SimpleNamespace(_draining=False), profile_id='owned',
                                     instance_id='current', db=db, epoch=epoch)
        connection = AuthorityConnection(authority, object(), {'user_id': 'owner'})
        params = {'job_id': 'job', 'request_id': 'fire', 'extra_prompt': None,
                  'expected_delivery_route_digest': 'a' * 64}
        try:
            response = await connection.dispatch({'id': 1, 'method': method, 'params': params})
        finally:
            await connection.close()
    fields = ((response.get('error') or {}).get('data') or {}).get('fields') or []
    assert 'expected_delivery_route_digest' not in fields, response
