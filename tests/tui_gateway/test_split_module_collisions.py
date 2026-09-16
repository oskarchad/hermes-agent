"""A split module must not silently take over another split module's name.

``tui_gateway/server.py`` composes its namespace by calling ``register()`` on a
list of sibling modules, each of which publishes its own top-level names onto the
server via ``method_ctx.bind_module``. Registration order therefore decides who
wins a shared name, and a retained fork module that carries a *pre-extraction*
copy of an upstream helper silently reverts upstream behaviour for every caller
(the Captain inbox port did exactly this to the notification poller: 12 ready
completions produced 12 autonomous turns instead of one batched turn).

The collision must be loud and every intentional takeover explicit.
"""
import types

import pytest

from tui_gateway import method_ctx


def _module_globals(name, **values):
    g = {"__name__": name}
    for key, fn in values.items():
        g[key] = types.FunctionType(fn.__code__, g, key)
    return g


def _server():
    return types.ModuleType("tui_gateway.server")


def test_undeclared_takeover_of_another_split_modules_name_raises():
    def helper():
        return "first"

    server = _server()
    method_ctx.bind_module(_module_globals("tui_gateway.first", helper=helper), server)
    with pytest.raises(RuntimeError, match="split-module name collision"):
        method_ctx.bind_module(_module_globals("tui_gateway.second", helper=helper), server)


def test_declared_override_is_allowed_and_records_the_new_owner():
    def helper():
        return "first"

    server = _server()
    method_ctx.bind_module(_module_globals("tui_gateway.first", helper=helper), server)
    method_ctx.bind_module(
        _module_globals("tui_gateway.second", helper=helper), server, override=("helper",)
    )
    assert server.helper._hermes_split_module == "tui_gateway.second"


def test_registered_server_keeps_upstream_owners_for_undeclared_names():
    """End-to-end: the real server composition leaves no undeclared takeover behind."""
    from tui_gateway import captain_inbox, server, session_notifications

    for name in ("_notification_poller_loop", "_maybe_fire_tui_loop_tick",
                 "_async_delegation_display_metadata", "_notification_pollers"):
        assert not hasattr(captain_inbox, name), (
            f"captain_inbox re-defines {name}; it belongs to session_notifications"
        )
        assert hasattr(session_notifications, name)
    assert server._notification_poller_loop._hermes_split_module == "tui_gateway.session_notifications"
    # The Captain routes that ARE intentional takeovers stay declared and owned.
    for name in ("_collect_kanban_notifications", "_format_kanban_event_text", "_notif_poll_kanban"):
        assert server.__dict__[name]._hermes_split_module == "tui_gateway.captain_inbox"
