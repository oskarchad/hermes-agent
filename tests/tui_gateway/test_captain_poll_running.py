"""Empty Captain polls must not leak a transient busy flag into turn settlement."""

import threading

import pytest

import tui_gateway.server as server


@pytest.mark.parametrize("collection_fails", [False, True])
def test_empty_poll_keeps_settled_snapshot_idle(monkeypatch, collection_fails):
    session = {"history_lock": threading.Lock(), "running": False}
    emitted = []
    monkeypatch.setattr(server, "_touch_captain_receivers", lambda _: None)
    monkeypatch.setattr(server, "_reconcile_session_cwd_from_terminal", lambda _: None)
    monkeypatch.setattr(server, "_emit", lambda event, sid, payload: emitted.append(payload))

    def collect(current, **kwargs):
        # Admission already excludes a competing submit while the DB is read.
        assert current["history_lock"].locked()
        server._emit_settled_session_info("sid", current, None)
        if collection_fails:
            raise RuntimeError("collector failed before claiming anything")
        return []

    monkeypatch.setattr(server, "_captain_collect_kanban_notifications", collect)
    server._captain_poll_kanban("sid", session)

    assert emitted and emitted[0]["running"] is False
    assert session["running"] is False
