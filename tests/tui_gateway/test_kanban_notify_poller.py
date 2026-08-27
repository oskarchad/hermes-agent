"""Tests for the TUI-side kanban notification poller (issue #59890).

``kanban_create`` auto-subscribes TUI/desktop sessions with
``platform="tui"`` / ``chat_id=HERMES_SESSION_KEY``, but no component ever
read those rows back: the gateway notifier skips them (no "tui" messaging
adapter) and the TUI notification poller only watched process completions.
``last_event_id`` stayed 0 forever and no notification was ever delivered.

These tests cover the delivery half that now lives in tui_gateway/server.py:
``_collect_kanban_notifications`` (cursor claim + formatting + archive-only
unsubscribe) and ``_format_kanban_event_text``.
"""

import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_cli import kanban_db as kb
from tui_gateway.server import (
    _collect_kanban_notifications,
    _format_kanban_event_text,
)

SESSION_KEY = "tui-session-key-1"


def _session(key: str = SESSION_KEY) -> dict:
    return {"session_key": key}


def _create_subscribed_task(*, chat_id: str = SESSION_KEY, platform: str = "tui"):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="notify tui", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform=platform, chat_id=chat_id)
        return tid
    finally:
        conn.close()


def _complete(tid: str, summary: str = "all done") -> None:
    conn = kb.connect()
    try:
        kb.complete_task(conn, tid, summary=summary)
    finally:
        conn.close()


def _sub_rows(tid: str) -> list:
    conn = kb.connect()
    try:
        return kb.list_notify_subs(conn, task_id=tid)
    finally:
        conn.close()


class TestCollectKanbanNotifications:
    def test_zero_sub_board_is_never_opened_writable(self):
        conn = kb.connect()
        conn.close()
        kb.create_board("second-board")

        with patch.object(kb, "connect", wraps=kb.connect) as spy_connect:
            texts = _collect_kanban_notifications(_session())

        assert texts == []
        spy_connect.assert_not_called()

    def test_done_reopen_notifies_once_per_event_until_archive(self):
        tid = _create_subscribed_task()
        _complete(tid, summary="shipped the fix")

        first = _collect_kanban_notifications(_session())

        assert len(first) == 1
        assert tid in first[0]
        assert "done" in first[0]
        assert "shipped the fix" in first[0]
        rows = _sub_rows(tid)
        assert len(rows) == 1, "done must retain the originating session"
        first_cursor = rows[0]["last_event_id"]

        # The retained subscription must not replay the completed event.
        assert _collect_kanban_notifications(_session()) == []

        conn = kb.connect()
        try:
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,)
                )
                kb._append_event(conn, tid, "status", {"status": "ready"})
            assert kb.complete_task(conn, tid, summary="review corrections")
        finally:
            conn.close()

        reopened = _collect_kanban_notifications(_session())

        assert len(reopened) == 2
        assert "ready" in reopened[0]
        assert "review corrections" in reopened[1]
        rows = _sub_rows(tid)
        assert len(rows) == 1
        assert rows[0]["chat_id"] == SESSION_KEY
        assert rows[0]["last_event_id"] > first_cursor
        assert _collect_kanban_notifications(_session()) == []

        conn = kb.connect()
        try:
            assert kb.archive_task(conn, tid)
        finally:
            conn.close()

        # Archive is notification-terminal and removes the retained route.
        assert _collect_kanban_notifications(_session()) == []
        assert _sub_rows(tid) == []

    def test_matching_tui_sub_delivers_and_advances_cursor(self):
        tid = _create_subscribed_task()
        pre_cursor = _sub_rows(tid)[0]["last_event_id"]
        conn = kb.connect()
        try:
            kb.block_task(conn, tid, reason="waiting on review")
        finally:
            conn.close()

        with patch.object(kb, "connect", wraps=kb.connect) as spy_connect:
            first = _collect_kanban_notifications(_session())
            second = _collect_kanban_notifications(_session())

        assert len(first) == 1
        assert "blocked" in first[0]
        assert "waiting on review" in first[0]
        assert second == []
        assert spy_connect.called
        # Blocked is not a final status -> subscription stays alive so a
        # respawned task's next terminal event still reaches the user.
        rows = _sub_rows(tid)
        assert len(rows) == 1
        assert rows[0]["last_event_id"] > pre_cursor

    def test_non_tui_subscription_does_not_open_board_writable(self):
        tid = _create_subscribed_task(platform="telegram", chat_id="chat-1")
        # New subs start caught up at creation time (issue #29905); record the
        # pre-completion cursors so we can assert they were never claimed.
        pre_cursor = _sub_rows(tid)[0]["last_event_id"]
        _complete(tid)

        with patch.object(kb, "connect", wraps=kb.connect) as spy_connect:
            texts = _collect_kanban_notifications(_session())

        assert texts == []
        spy_connect.assert_not_called()
        rows = _sub_rows(tid)
        assert len(rows) == 1
        assert rows[0]["last_event_id"] == pre_cursor

    def test_other_tui_session_does_not_open_board_writable(self):
        tid = _create_subscribed_task(chat_id="some-other-session")
        pre_cursor = _sub_rows(tid)[0]["last_event_id"]
        _complete(tid)

        with patch.object(kb, "connect", wraps=kb.connect) as spy_connect:
            texts = _collect_kanban_notifications(_session())

        assert texts == []
        spy_connect.assert_not_called()
        rows = _sub_rows(tid)
        assert len(rows) == 1
        assert rows[0]["last_event_id"] == pre_cursor

    def test_probe_error_falls_back_to_writable_delivery(self, monkeypatch):
        tid = _create_subscribed_task()
        _complete(tid, summary="fallback delivery")

        def fail_probe(*args, **kwargs):
            raise OSError("probe unavailable")

        monkeypatch.setattr(kb, "count_notify_subs", fail_probe)
        with patch.object(kb, "connect", wraps=kb.connect) as spy_connect:
            texts = _collect_kanban_notifications(_session())

        assert len(texts) == 1
        assert tid in texts[0]
        spy_connect.assert_called_once()

    def test_no_session_key_is_a_noop(self):
        tid = _create_subscribed_task()
        _complete(tid)

        assert _collect_kanban_notifications({"session_key": ""}) == []
        assert _collect_kanban_notifications({"session_key": None}) == []
        assert len(_sub_rows(tid)) == 1

    def test_profile_scoped_session_reads_the_shared_board(self, tmp_path):
        """The kanban board is shared across profiles BY DESIGN (see the
        hermes_cli/kanban_db.py module docstring): ``kanban_home()`` anchors on
        ``get_default_hermes_root()``, which resolves the process env and
        ignores context-local profile overrides. A Desktop session bound to a
        non-launch profile (``session["profile_home"]``) must therefore still
        have its subscription claimed from the one shared board — the poller
        needs no per-profile home binding.
        """
        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )

        tid = _create_subscribed_task()
        _complete(tid, summary="cross-profile delivery")

        other_profile_home = tmp_path / "profiles" / "reviewer"
        other_profile_home.mkdir(parents=True)
        session = {
            "session_key": SESSION_KEY,
            "profile_home": str(other_profile_home),
        }
        # Simulate the strictest case: a context-local profile override is
        # active while the poller collects (as a profile-bound RPC would set).
        token = set_hermes_home_override(str(other_profile_home))
        try:
            texts = _collect_kanban_notifications(session)
        finally:
            reset_hermes_home_override(token)

        assert len(texts) == 1
        assert tid in texts[0]
        assert "cross-profile delivery" in texts[0]
        # Completion is reversible, so the shared-board subscription remains
        # owned by this exact Desktop session until the task is archived.
        rows = _sub_rows(tid)
        assert len(rows) == 1
        assert rows[0]["chat_id"] == SESSION_KEY


class TestFormatKanbanEventText:
    SUB = {"task_id": "t_abc123"}
    TASK = SimpleNamespace(title="build the thing", assignee="worker", result=None)

    def test_silent_kinds_return_none(self):
        for kind in ("archived", "unblocked"):
            ev = SimpleNamespace(kind=kind, payload={})
            assert _format_kanban_event_text(self.SUB, self.TASK, ev, "main") is None

    def test_blocked_includes_reason(self):
        ev = SimpleNamespace(kind="blocked", payload={"reason": "needs creds"})
        text = _format_kanban_event_text(self.SUB, self.TASK, ev, "main")
        assert "t_abc123" in text
        assert "blocked" in text
        assert "needs creds" in text
        assert "[main]" in text
        assert "@worker" in text

    def test_completed_prefers_payload_summary(self):
        ev = SimpleNamespace(kind="completed", payload={"summary": "first line\nsecond"})
        text = _format_kanban_event_text(self.SUB, self.TASK, ev, "")
        assert "done" in text
        assert "first line" in text
        assert "second" not in text

    def test_timed_out_with_bad_payload_does_not_raise(self):
        ev = SimpleNamespace(kind="timed_out", payload={"limit_seconds": "not-a-number"})
        text = _format_kanban_event_text(self.SUB, self.TASK, ev, "")
        assert "timed out" in text


class TestNotificationPollerLoopKanbanWiring:
    """Drive a real TUI subscription through ``_notification_poller_loop``.

    Covers the wiring above ``_collect_kanban_notifications``: no assistant
    completion before settlement, one stable completion after settlement,
    agent-turn dispatch when the session is idle, durable deferral while it is
    busy, and cursor rewind when dispatch rejects a claimed turn.
    """

    def _start_poller(self, session: dict, monkeypatch):
        import threading
        import tui_gateway.server as server

        emits: list = []
        submits: list = []
        monkeypatch.setattr(server, "_KANBAN_POLL_SECONDS", 0.01)
        monkeypatch.setattr(
            server, "_emit", lambda event, sid, payload=None: emits.append((event, payload))
        )
        def submit_turn(
            _rid,
            _sid,
            _session,
            text,
            *,
            on_terminal=None,
            completion_id=None,
            **_kwargs,
        ):
            submits.append(text)
            assert not any(event == "message.complete" for event, _ in emits)
            if on_terminal is not None:
                on_terminal(True)
            server._emit(
                "message.complete",
                _sid,
                {"id": completion_id, "status": "complete", "text": "settled"},
            )
            return True

        monkeypatch.setattr(server, "_run_prompt_submit", submit_turn)
        stop = threading.Event()
        thread = threading.Thread(
            target=server._notification_poller_loop,
            args=(stop, "sid-poller-test", session),
            daemon=True,
        )
        thread.start()
        return stop, thread, emits, submits

    @staticmethod
    def _wait_for(predicate, timeout: float = 5.0) -> bool:
        import time as _time

        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            if predicate():
                return True
            _time.sleep(0.02)
        return False

    def _poller_session(self, *, running: bool = False) -> dict:
        import threading

        return {
            "session_key": SESSION_KEY,
            "history_lock": threading.Lock(),
            "running": running,
        }

    @staticmethod
    def _run_poller_once(session: dict, monkeypatch, *, emits: list, submit) -> None:
        import threading

        import tui_gateway.server as server
        from tools.process_registry import process_registry

        class _StopAfterOnePoll(threading.Event):
            def __init__(self):
                super().__init__()
                self._checks = 0

            def is_set(self):
                self._checks += 1
                return self._checks > 1

        class _EmptyCompletionQueue:
            def get(self, timeout=None):
                raise RuntimeError("empty")

            def empty(self):
                return True

        monkeypatch.setattr(process_registry, "completion_queue", _EmptyCompletionQueue())
        monkeypatch.setattr(server, "_maybe_fire_tui_loop_tick", lambda *_args: None)
        monkeypatch.setattr(
            server, "_emit", lambda event, sid, payload=None: emits.append((event, payload))
        )
        def submit_turn(
            rid,
            sid,
            current_session,
            text,
            *,
            on_terminal=None,
            completion_id=None,
            **_kwargs,
        ):
            accepted = submit(rid, sid, current_session, text)
            if accepted is not False and on_terminal is not None:
                on_terminal(True)
                server._emit(
                    "message.complete",
                    sid,
                    {"id": completion_id, "status": "complete", "text": "settled"},
                )
            return accepted

        monkeypatch.setattr(server, "_run_prompt_submit", submit_turn)
        server._notification_poller_loop(_StopAfterOnePoll(), "sid-poller-test", session)

    def test_idle_session_gets_one_post_settlement_completion(self, monkeypatch):
        tid = _create_subscribed_task()
        _complete(tid, summary="poller e2e done")
        session = self._poller_session(running=False)

        stop, thread, emits, submits = self._start_poller(session, monkeypatch)
        try:
            assert self._wait_for(lambda: submits), "agent turn was never dispatched"
        finally:
            stop.set()
            thread.join(timeout=5)

        assert not [p for e, p in emits if e == "status.update"]
        assert any(e == "message.start" for e, _ in emits)
        completions = [p for e, p in emits if e == "message.complete"]
        assert len(completions) == 1
        assert completions[0]["id"]
        assert any(tid in text for text in submits), submits
        assert session["running"] is True  # poller claimed the turn
        assert not session.get("_kanban_pending")

    def test_captain_signal_terminal_settlement_posts_reply_exactly_once(
        self, monkeypatch, tmp_path
    ):
        import tui_gateway.server as server
        from tools import kanban_tools as kt

        class ActiveWorker:
            def __init__(self):
                self.steers: list[str] = []

            def steer(self, text: str) -> bool:
                self.steers.append(text)
                return True

        conn = kb.connect()
        try:
            tid = kb.create_task(
                conn,
                title="Captain decision target",
                assignee="worker",
                captain_profile="otto",
                captain_origin_session_key=SESSION_KEY,
            )
        finally:
            conn.close()

        # Seed the existing live-comment bridge before the worker emits its
        # structured request, exactly as a running worker does on its first poll.
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        monkeypatch.setenv("HERMES_PROFILE", "wrench")
        kt._comment_poll_last_attempt = 0.0
        kt._comment_watermark.clear()
        worker = ActiveWorker()
        assert kt.inject_new_comments_from_env(worker) is False

        conn = kb.connect()
        try:
            kb.add_comment(
                conn,
                tid,
                author="wrench",
                body="DECISION REQUIRED — choose the bounded route",
            )
            inbox_count = conn.execute(
                "SELECT COUNT(*) FROM kanban_captain_inbox WHERE task_id = ?",
                (tid,),
            ).fetchone()[0]
        finally:
            conn.close()
        assert inbox_count == 1

        turn_state = {"ran": False}
        receipt = {
            "report": {
                "role": "assistant",
                "content": "ACK — choose route A.",
            },
            "messages": [],
            "moved": False,
        }
        monkeypatch.setattr(
            server,
            "_persisted_captain_report",
            lambda _session, _completion_id: receipt if turn_state["ran"] else None,
        )
        session = self._poller_session(running=False)
        captain_home = tmp_path / "profiles" / "Otto"
        captain_home.mkdir(parents=True)
        session["profile_home"] = str(captain_home)
        emits: list = []
        submits: list[str] = []

        def submit_signal(_rid, _sid, _session, text):
            submits.append(text)
            turn_state["ran"] = True

        self._run_poller_once(
            session,
            monkeypatch,
            emits=emits,
            submit=submit_signal,
        )

        conn = kb.connect()
        try:
            inbox_states = [
                row["state"]
                for row in conn.execute(
                    "SELECT state FROM kanban_captain_inbox WHERE task_id = ?",
                    (tid,),
                ).fetchall()
            ]
            replies = [
                (comment.author, comment.body)
                for comment in kb.list_comments(conn, tid)
                if comment.body == "ACK — choose route A."
            ]
            future_worker_context = kb.build_worker_context(conn, tid)
        finally:
            conn.close()

        assert len(submits) == 1
        assert tid in submits[0]
        assert "DECISION REQUIRED" in submits[0]
        assert inbox_states == ["acked"]
        assert replies == [("otto", "ACK — choose route A.")]

        kt._comment_poll_last_attempt = 0.0
        assert kt.inject_new_comments_from_env(worker) is True
        assert len(worker.steers) == 1
        assert "otto: ACK — choose route A." in worker.steers[0]
        assert "comment from worker `otto`" in future_worker_context
        assert "ACK — choose route A." in future_worker_context

        # Polling both sides again replays neither the Captain turn nor the
        # worker injection, so the response remains exactly-once end to end.
        with session["history_lock"]:
            session["running"] = False
        self._run_poller_once(
            session,
            monkeypatch,
            emits=emits,
            submit=lambda _rid, _sid, _session, text: submits.append(text),
        )
        assert len(submits) == 1
        kt._comment_poll_last_attempt = 0.0
        assert kt.inject_new_comments_from_env(worker) is False
        assert len(worker.steers) == 1

    def test_busy_session_waits_then_dispatches_when_idle(self, monkeypatch):
        import threading

        import tui_gateway.server as server

        tid = _create_subscribed_task()
        initial_cursor = _sub_rows(tid)[0]["last_event_id"]
        _complete(tid, summary="waited while busy")
        session = self._poller_session(running=True)
        busy_poll_seen = threading.Event()
        monkeypatch.setattr(
            server,
            "_maybe_fire_tui_loop_tick",
            lambda *_args: busy_poll_seen.set(),
        )

        stop, thread, emits, submits = self._start_poller(session, monkeypatch)
        try:
            assert busy_poll_seen.wait(2.0), "poller never observed the busy session"
            assert _sub_rows(tid)[0]["last_event_id"] == initial_cursor
            assert submits == []
            assert emits == []
            assert not session.get("_kanban_pending")

            with session["history_lock"]:
                session["running"] = False

            assert self._wait_for(lambda: submits), "durable event was never dispatched"
        finally:
            stop.set()
            thread.join(timeout=5)

        assert any(tid in text for text in submits), submits
        assert _sub_rows(tid)[0]["last_event_id"] > initial_cursor
        assert not session.get("_kanban_pending")
        assert session["running"] is True

    def test_busy_event_survives_poller_restart_and_delivers_once_when_idle(
        self, monkeypatch
    ):
        tid = _create_subscribed_task()
        initial_cursor = _sub_rows(tid)[0]["last_event_id"]
        _complete(tid, summary="survives restart")
        emits: list = []
        submits: list[str] = []

        busy_session = self._poller_session(running=True)
        self._run_poller_once(
            busy_session,
            monkeypatch,
            emits=emits,
            submit=lambda _rid, _sid, _session, text: submits.append(text),
        )

        assert _sub_rows(tid)[0]["last_event_id"] == initial_cursor
        assert submits == []
        assert emits == []
        assert not busy_session.get("_kanban_pending")

        # A fresh session dict simulates the process-local poller buffer being
        # discarded by stop/restart. The durable cursor must still expose the
        # event to the same origin session and only that session.
        restarted_session = self._poller_session(running=False)
        self._run_poller_once(
            restarted_session,
            monkeypatch,
            emits=emits,
            submit=lambda _rid, _sid, _session, text: submits.append(text),
        )

        assert len(submits) == 1
        assert tid in submits[0]
        assert _sub_rows(tid)[0]["last_event_id"] > initial_cursor
        assert not [payload for event, payload in emits if event == "status.update"]
        completions = [payload for event, payload in emits if event == "message.complete"]
        assert len(completions) == 1
        assert completions[0]["id"]

        with restarted_session["history_lock"]:
            restarted_session["running"] = False
        self._run_poller_once(
            restarted_session,
            monkeypatch,
            emits=emits,
            submit=lambda _rid, _sid, _session, text: submits.append(text),
        )
        assert len(submits) == 1
        assert [
            payload["id"] for event, payload in emits if event == "message.complete"
        ] == [completions[0]["id"]]

    def test_dispatch_failure_rewinds_cursor_for_one_retry(self, monkeypatch):
        tid = _create_subscribed_task()
        initial_cursor = _sub_rows(tid)[0]["last_event_id"]
        _complete(tid, summary="retry after dispatch failure")
        conn = kb.connect()
        try:
            assert kb.archive_task(conn, tid)
        finally:
            conn.close()
        session = self._poller_session(running=False)
        emits: list = []
        attempts: list[str] = []

        def fail_once(_rid, _sid, _session, text):
            attempts.append(text)
            if len(attempts) == 1:
                raise RuntimeError("synthetic turn rejected")

        self._run_poller_once(
            session,
            monkeypatch,
            emits=emits,
            submit=fail_once,
        )

        assert len(attempts) == 1
        assert _sub_rows(tid)[0]["last_event_id"] == initial_cursor
        assert session["running"] is False

        self._run_poller_once(
            session,
            monkeypatch,
            emits=emits,
            submit=fail_once,
        )

        assert len(attempts) == 2
        assert attempts[0] == attempts[1]
        assert tid in attempts[1]
        assert _sub_rows(tid) == [], "archive unsubscribe waits for accepted delivery"

        with session["history_lock"]:
            session["running"] = False
        self._run_poller_once(
            session,
            monkeypatch,
            emits=emits,
            submit=fail_once,
        )
        assert len(attempts) == 2

    @pytest.mark.parametrize("outcome", ["empty", "error", "rejection"])
    def test_no_turn_reservation_cannot_strand_waiting_user_prompt(
        self, monkeypatch, outcome
    ):
        import tui_gateway.server as server
        from tools.process_registry import process_registry

        class _WaiterFirstLock:
            """Lock whose queued waiter wins over a same-thread reacquire."""

            def __init__(self):
                self._condition = threading.Condition()
                self._locked = False
                self._waiters = 0
                self.waiter_blocked = threading.Event()

            def __enter__(self):
                with self._condition:
                    was_waiter = self._locked
                    if was_waiter:
                        self._waiters += 1
                        self.waiter_blocked.set()
                        while self._locked:
                            self._condition.wait()
                    else:
                        while self._locked or self._waiters:
                            self._condition.wait()
                    if was_waiter:
                        self._waiters -= 1
                    self._locked = True
                return self

            def __exit__(self, *_args):
                with self._condition:
                    self._locked = False
                    self._condition.notify_all()

        class _StopAfterOnePoll(threading.Event):
            def __init__(self):
                super().__init__()
                self._checks = 0

            def is_set(self):
                self._checks += 1
                return self._checks > 1

        class _EmptyCompletionQueue:
            def get(self, timeout=None):
                raise RuntimeError("empty")

            def empty(self):
                return True

        history_lock = _WaiterFirstLock()
        session = {
            "agent": SimpleNamespace(),
            "session_key": SESSION_KEY,
            "history": [],
            "history_lock": history_lock,
            "history_version": 0,
            "running": False,
            "transport": None,
            "attached_images": [],
        }
        collection_started = threading.Event()
        release_collection = threading.Event()
        prompt_returned = threading.Event()
        user_prompt_started = threading.Event()
        prompt_response: dict = {}

        def collect(_session, *, claim_records):
            collection_started.set()
            assert release_collection.wait(2.0), "test did not release collection"
            if outcome == "error":
                raise RuntimeError("collection failed")
            return ["kanban event"] if outcome == "rejection" else []

        def run_prompt(_rid, _sid, _session, text, **_kwargs):
            if text == "kanban event":
                assert prompt_returned.wait(2.0), "user prompt never observed reservation"
                raise RuntimeError("synthetic turn rejected")
            user_prompt_started.set()
            return True

        monkeypatch.setattr(process_registry, "completion_queue", _EmptyCompletionQueue())
        monkeypatch.setattr(server, "_maybe_fire_tui_loop_tick", lambda *_args: None)
        monkeypatch.setattr(server, "_collect_kanban_notifications", collect)
        monkeypatch.setattr(server, "_emit", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "queue")
        monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *_args: None)
        monkeypatch.setattr(server, "_session_uses_compute_host", lambda *_args: False)
        monkeypatch.setattr(server, "_ensure_session_db_row", lambda *_args: None)
        monkeypatch.setattr(server, "_persist_branch_seed", lambda *_args: None)
        monkeypatch.setattr(server, "_start_agent_build", lambda *_args: None)
        monkeypatch.setattr(server, "_wait_agent_for_prompt", lambda *_args: None)
        monkeypatch.setattr(server, "_run_prompt_submit", run_prompt)

        def submit_user_prompt():
            prompt_response.update(
                server._methods["prompt.submit"](
                    "user-rid",
                    {"session_id": "sid-poller-test", "text": "user prompt"},
                )
            )
            prompt_returned.set()

        server._sessions["sid-poller-test"] = session
        poller = threading.Thread(
            target=server._notification_poller_loop,
            args=(_StopAfterOnePoll(), "sid-poller-test", session),
            daemon=True,
        )
        prompt = threading.Thread(target=submit_user_prompt, daemon=True)
        try:
            poller.start()
            assert collection_started.wait(2.0), "poller did not start collection"
            prompt.start()
            assert history_lock.waiter_blocked.wait(2.0), (
                "prompt.submit did not wait on history_lock"
            )
            release_collection.set()
            assert prompt_returned.wait(2.0), "prompt.submit did not return"
            assert user_prompt_started.wait(2.0), (
                "waiting user prompt was neither started normally nor drained"
            )
        finally:
            release_collection.set()
            prompt.join(timeout=2.0)
            poller.join(timeout=2.0)
            server._sessions.pop("sid-poller-test", None)

        assert "error" not in prompt_response
        assert session.get("queued_prompt") is None