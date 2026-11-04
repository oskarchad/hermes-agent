"""Captain's real prompt dispatcher retains durable, fenced terminal semantics."""
from types import SimpleNamespace

from tests.tui_gateway import test_failed_turn_retention as native
_session = native._session
emits = native.emits
turn_env = native.turn_env
from tui_gateway import server


def test_captain_prompt_missing_store_releases_claim_without_success(emits, turn_env):
    terminal = []
    agent = SimpleNamespace(session_id="session-key", clear_interrupt=lambda: None,
        run_conversation=lambda *a, **k: {"final_response": "report", "messages": []})
    session = _session(agent=agent, running=True)
    server._run_prompt_submit("rid", "sid", session, "durable report",
        on_terminal=terminal.append, completion_id="kanban-report:stable",
        require_persisted=True, turn_purpose="captain_report")
    assert terminal == [False]
    assert not any(e == "message.complete" and p.get("status") == "complete" for e, _, p in emits)
    assert session["running"] is False


def test_captain_turn_does_not_advance_goal_or_loop(emits, turn_env, monkeypatch):
    terminal, goals = [], []
    monkeypatch.setattr(server, "_goal_followup_after_turn", lambda *a: goals.append("goal"))
    monkeypatch.setattr(server, "_after_complete_turn", lambda *a: goals.append("loop"))
    agent = SimpleNamespace(session_id="session-key", clear_interrupt=lambda: None,
        run_conversation=lambda *a, **k: {"final_response": "report", "messages": []})
    session = _session(agent=agent, running=True)
    server._run_prompt_submit("rid", "sid", session, "report", on_terminal=terminal.append,
        completion_id="kanban-report:stable", turn_purpose="captain_report")
    assert terminal == [True]
    assert goals == []
    assert next(p for e, _, p in emits if e == "message.complete")["id"] == "kanban-report:stable"


def test_captain_persisted_receipt_precedes_terminal_ack(emits, turn_env, tmp_path):
    from agent.context_compressor import _DB_PERSISTED_MARKER
    from hermes_state import SessionDB
    from tests.tui_gateway.test_queued_prompt_persistence import _flush_agent

    db = SessionDB(db_path=tmp_path / 'state.db')
    db.create_session('session-key', 'desktop')
    agent = _flush_agent(db, 'session-key')
    agent.clear_interrupt = lambda: None
    agent.context_compressor = SimpleNamespace(compression_count=0)
    agent._persist_user_message_idx = 0
    terminal = []

    def run(*args, **kwargs):
        messages = [{'role': 'user', 'content': 'report'}, {'role': 'assistant', 'content': 'durable'}]
        agent._flush_messages_to_session_db(messages, [])
        assert all(m.get(_DB_PERSISTED_MARKER) for m in messages)
        return {'final_response': 'durable', 'messages': messages}

    def settled(success):
        receipt = db.rehome_captain_report('kanban-report:durable', 'session-key')
        terminal.append((success, receipt['report']['content'] if receipt else None))
        assert not any(e == 'message.complete' for e, _, _ in emits)

    agent.run_conversation = run
    try:
        server._run_prompt_submit('rid', 'sid', _session(agent=agent, running=True), 'report',
            on_terminal=settled, completion_id='kanban-report:durable', require_persisted=True,
            turn_purpose='captain_report')
        assert terminal == [(True, 'durable')]
        assert next(p for e, _, p in emits if e == 'message.complete')['persisted_turn']['complete']
    finally:
        db.close()


def test_captain_stamp_addresses_exact_row_and_rehome_preserves_identity(tmp_path):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / 'state.db')
    db.create_session('source', 'desktop')
    db.create_session('destination', 'desktop')
    try:
        db.append_message('source', 'user', content='ordinary')
        ordinary = db.append_message('source', 'assistant', content='same report')
        db.append_message('source', 'user', content='Captain')
        selected = db.append_message('source', 'assistant', content='same report')
        assert not db.stamp_captain_completion('destination', selected, 'same report', 'receipt')
        assert not db.stamp_captain_completion('source', selected, 'other text', 'receipt')
        assert db.stamp_captain_completion('source', selected, 'same report', 'receipt')
        assert not db.stamp_captain_completion('source', selected, 'same report', 'other-receipt')
        conn = db._conn
        before = conn.execute('SELECT message_uid, display_identity FROM messages WHERE id = ?', (selected,)).fetchone()
        receipt = db.rehome_captain_report('receipt', 'destination')
        assert receipt['moved']
        after = conn.execute("SELECT message_uid, display_identity FROM messages WHERE session_id = 'destination' AND role = 'assistant' AND active = 1").fetchone()
        assert tuple(after) == tuple(before)
        assert conn.execute('SELECT active FROM messages WHERE id = ?', (ordinary,)).fetchone()[0] == 1
        assert not db.rehome_captain_report('receipt', 'destination')['moved']
    finally:
        db.close()


def test_captain_poll_respects_stop_latch_and_native_diagnostic_partition(monkeypatch, tmp_path):
    import threading
    from gateway.warning_notifications import DiagnosticText

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    (tmp_path / 'config.yaml').write_text('display: {suppress_warning_notifications: true}\n', encoding='utf-8')
    monkeypatch.setattr(server, '_touch_captain_receivers', lambda session: None)
    polls, submits = [], []

    def collect(session, **kwargs):
        polls.append(True)
        return [DiagnosticText('worker failure'), 'requested result']

    monkeypatch.setattr(server, '_collect_kanban_notifications', collect)
    monkeypatch.setattr(server, '_emit', lambda *a: None)
    monkeypatch.setattr(server, '_notif_submit', lambda *a, **k: submits.append((a, k)))
    session = {'profile_home': str(tmp_path), 'history_lock': threading.RLock(), '_turn_cancel_requested': True}
    server._notif_poll_kanban('sid', session)
    assert polls == []
    session['_turn_cancel_requested'] = False
    server._notif_poll_kanban('sid', session)
    assert submits[0][0][3] == 'worker failure'
    assert submits[0][1]['display_metadata']['notification_category'] == 'diagnostic'
    assert session['_kanban_pending'] == ['requested result']
    session['running'] = False
    server._notif_poll_kanban('sid', session)
    assert submits[1][0][3] == 'requested result'
    assert submits[1][1] == {}
    assert len(polls) == 1
