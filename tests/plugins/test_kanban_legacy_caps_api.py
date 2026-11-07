"""The native task UI preserves legacy caps and refuses unsupported cap writes."""
from contextlib import closing

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
from plugins.kanban.dashboard.plugin_api import router


@pytest.fixture
def capped_card(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    monkeypatch.delenv('HERMES_DELEGATED_CHILD', raising=False)
    with closing(connect()) as conn:
        tid = kb.create_task(conn, title='Existing card', assignee='default')
        conn.execute('ALTER TABLE tasks ADD COLUMN enabled_toolsets TEXT')
        conn.execute("UPDATE tasks SET enabled_toolsets='[]' WHERE id=?", (tid,))
        conn.commit()
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        yield client, tid


@pytest.mark.parametrize('action', ['create', 'update', 'bulk'])
@pytest.mark.parametrize('field', ['enabled_toolsets', 'clear_enabled_toolsets'])
def test_cap_writes_are_rejected_instead_of_ignored(capped_card, action, field):
    client, tid = capped_card
    cap = {field: [] if field == 'enabled_toolsets' else True}
    if action == 'create':
        response = client.post('/tasks', json={'title': 'Unsupported cap', 'assignee': 'default', **cap})
    elif action == 'update':
        response = client.patch(f'/tasks/{tid}', json={'title': 'Must not change', **cap})
    else:
        response = client.post('/tasks/bulk', json={'ids': [tid], 'priority': 10, **cap})
    assert response.status_code == 422
    assert 'read-only' in response.text
    with closing(connect()) as conn:
        assert len(kb.list_tasks(conn)) == 1
        assert kb.get_task(conn, tid).title == 'Existing card'


def test_ordinary_ui_edit_and_readback_preserve_caps(capped_card):
    client, tid = capped_card
    response = client.patch(f'/tasks/{tid}', json={'title': 'Edited title', 'priority': 7})
    assert response.status_code == 200
    assert response.json()['task']['enabled_toolsets'] == []
    assert client.get(f'/tasks/{tid}').json()['task']['enabled_toolsets'] == []
    with closing(connect()) as conn:
        assert kb.get_task(conn, tid).enabled_toolsets == []
        assert conn.execute('SELECT enabled_toolsets FROM tasks WHERE id=?', (tid,)).fetchone()[0] == '[]'
