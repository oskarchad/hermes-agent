"""Old tool callers cannot silently create an unbounded task."""
import json

import pytest


@pytest.mark.parametrize('cap', [[], ['terminal'], None])
def test_create_tool_refuses_unsupported_cap_write(tmp_path, monkeypatch, cap):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    from tools import kanban_tools  # registers the real tool handler
    from tools.registry import registry
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing

    output = json.loads(registry.get_entry('kanban_create').handler(
        {'title': 'Unsupported cap', 'assignee': 'default', 'enabled_toolsets': cap}))
    assert 'unknown parameter(s): enabled_toolsets' in output.get('error', '')
    with connect_closing() as conn:
        assert kb.list_tasks(conn) == []
