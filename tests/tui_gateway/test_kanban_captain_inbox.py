"""Behavioral tests for the durable profile-scoped Captain reporting inbox.

The existing ``kanban_notify_subs`` route delivers a terminal task event to the
*exact* origin TUI/Desktop session (``platform="tui"`` / ``chat_id=session_key``).
When that session is gone, the event is stranded. The Captain inbox is a durable,
profile-scoped ledger that guarantees *exactly one* Captain report across a
profile's TUI/Desktop sessions: the live origin session owns delivery, and if it
is absent/finalized the next active same-profile session may claim.

These tests drive the real DB (temp SQLite from the autouse hermetic HERMES_HOME)
and the real poller collector path in ``tui_gateway/server.py``.
"""

import threading
import time
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
import tui_gateway.server as server
from tui_gateway.server import _collect_kanban_notifications

ORIGIN_KEY = "captain-origin-session"
SIBLING_KEY = "captain-sibling-session"


@pytest.fixture(autouse=True)
def _clear_sessions():
    server._sessions.clear()
    yield
    server._sessions.clear()


def _session(key, profile_home=None):
    s = {"session_key": key}
    if profile_home:
        s["profile_home"] = profile_home
    return s


def _register_live(sid, session):
    server._sessions[sid] = session


def _profile_for(session):
    return server._session_captain_profile(session)


def _inbox_states():
    conn = kb.connect()
    try:
        rows = conn.execute(
            "SELECT state, COUNT(*) AS n FROM kanban_captain_inbox GROUP BY state"
        ).fetchall()
    finally:
        conn.close()
    return {r["state"]: int(r["n"]) for r in rows}


# ── requirement 1: two live same-profile sessions ──────────────────────────
def test_live_origin_owns_report_sibling_gets_none():
    origin = _session(ORIGIN_KEY)
    sibling = _session(SIBLING_KEY)
    _register_live("sid-origin", origin)
    _register_live("sid-sibling", sibling)
    profile = _profile_for(origin)
    assert _profile_for(sibling) == profile  # same profile

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cap1", assignee="worker")
        kb.register_captain_owner(
            conn, tid, profile=profile, origin_session_key=ORIGIN_KEY
        )
        kb.complete_task(conn, tid, summary="done work")
    finally:
        conn.close()

    # Sibling must not claim while the exact origin is live.
    sib_claims = []
    assert _collect_kanban_notifications(sibling, claim_records=sib_claims) == []
    assert sib_claims == []

    orig_claims = []
    texts = _collect_kanban_notifications(origin, claim_records=orig_claims)
    assert len(texts) == 1
    assert tid in texts[0]
    server._settle_kanban_notification_claims(orig_claims, accepted=True)

    # No replay once acked.
    assert _collect_kanban_notifications(origin, claim_records=[]) == []
    assert _inbox_states() == {"acked": 1}


# ── requirement 2: origin gone → another same-profile session claims once ──
def test_origin_gone_sibling_claims_exactly_once():
    profile = _profile_for(_session(ORIGIN_KEY))
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cap2", assignee="worker")
        kb.register_captain_owner(
            conn, tid, profile=profile, origin_session_key=ORIGIN_KEY
        )
        kb.complete_task(conn, tid, summary="finished")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE kanban_captain_receivers SET last_seen = ?",
                (int(time.time()) - server._CAPTAIN_RECEIVER_TTL_SECONDS - 1,),
            )
    finally:
        conn.close()

    # Origin is NOT live. Two live same-profile siblings both poll.
    s1 = _session("live-a")
    s2 = _session("live-b")
    _register_live("sid-a", s1)
    _register_live("sid-b", s2)

    c1, c2 = [], []
    t1 = _collect_kanban_notifications(s1, claim_records=c1)
    t2 = _collect_kanban_notifications(s2, claim_records=c2)
    total = t1 + t2
    assert len(total) == 1
    assert tid in total[0]

    server._settle_kanban_notification_claims(c1, accepted=True)
    server._settle_kanban_notification_claims(c2, accepted=True)
    assert _collect_kanban_notifications(s1, claim_records=[]) == []
    assert _collect_kanban_notifications(s2, claim_records=[]) == []


# ── requirement 3: different profile cannot claim ──────────────────────────
def test_different_profile_cannot_claim():
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="cap3",
            assignee="worker",
            captain_profile="a-totally-other-profile",
        )
        kb.complete_task(conn, tid, summary="foreign")
    finally:
        conn.close()

    s = _session("live-x")
    _register_live("sid-x", s)
    assert _profile_for(s) != "a-totally-other-profile"
    assert _collect_kanban_notifications(s, claim_records=[]) == []
    # The row stays pending for its own profile — never consumed by another.
    assert _inbox_states() == {"pending": 1}


# ── requirement 4: restart between lease and ack → expired lease reclaimed ──
def test_expired_lease_is_reclaimed_and_acked_once():
    profile = _profile_for(_session("live-r"))
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cap4", assignee="worker")
        kb.register_captain_owner(conn, tid, profile=profile, origin_session_key=None)
        kb.complete_task(conn, tid, summary="lease-test")
    finally:
        conn.close()

    s = _session("live-r")
    _register_live("sid-r", s)

    c1 = []
    t1 = _collect_kanban_notifications(s, claim_records=c1)
    assert len(t1) == 1  # leased

    # Simulate a crash BEFORE ack: never settle. A fresh poll must not
    # re-claim the still-valid lease.
    assert _collect_kanban_notifications(s, claim_records=[]) == []

    # Simulate a process restart past the lease expiry.
    conn = kb.connect()
    try:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE kanban_captain_inbox SET lease_expires = ? "
                "WHERE state = 'leased'",
                (int(time.time()) - 1,),
            )
    finally:
        conn.close()

    c2 = []
    t2 = _collect_kanban_notifications(s, claim_records=c2)
    assert len(t2) == 1  # reclaimed
    assert t2[0] == t1[0]
    server._settle_kanban_notification_claims(c2, accepted=True)

    assert _collect_kanban_notifications(s, claim_records=[]) == []
    assert _inbox_states() == {"acked": 1}


def test_live_owner_renews_lease_past_original_expiry(monkeypatch):
    """A controlled slow turn cannot be re-leased by a second live session."""
    clock = [1_000]
    monkeypatch.setattr(server.time, "time", lambda: clock[0])
    profile = _profile_for(_session("slow-owner"))
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="slow-turn", assignee="worker")
        kb.register_captain_owner(conn, tid, profile=profile, origin_session_key=None)
        kb.complete_task(conn, tid, summary="still running")
    finally:
        conn.close()

    owner = _session("slow-owner")
    contender = _session("slow-contender")
    _register_live("slow-owner", owner)
    _register_live("slow-contender", contender)

    claims = []
    assert len(_collect_kanban_notifications(owner, claim_records=claims)) == 1
    original_expiry = clock[0] + server._CAPTAIN_LEASE_SECONDS

    clock[0] = original_expiry - 10
    server._renew_kanban_notification_claims(
        claims,
        lease_seconds=server._CAPTAIN_LEASE_SECONDS,
        now=clock[0],
    )

    # Past the original expiry, the renewed owner still fences the row.
    clock[0] = original_expiry + 1
    contender_claims = []
    assert _collect_kanban_notifications(
        contender, claim_records=contender_claims
    ) == []
    assert contender_claims == []

    server._settle_kanban_notification_claims(claims, accepted=True)
    assert _inbox_states() == {"acked": 1}


def test_expired_owner_cannot_ack_before_a_contender_reclaims(monkeypatch):
    clock = [1_000]
    monkeypatch.setattr(server.time, "time", lambda: clock[0])
    profile = _profile_for(_session("expired-owner"))
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="expired-owner", assignee="worker")
        kb.register_captain_owner(
            conn, tid, profile=profile, origin_session_key=None
        )
        kb.complete_task(conn, tid, summary="expires before settlement")
    finally:
        conn.close()

    claims = []
    assert len(
        _collect_kanban_notifications(_session("expired-owner"), claim_records=claims)
    ) == 1
    clock[0] = 1_121
    with pytest.raises(RuntimeError, match="expected 1 row"):
        server._settle_kanban_notification_claims(claims, accepted=True)

    conn = kb.connect()
    try:
        state = conn.execute(
            "SELECT state FROM kanban_captain_inbox WHERE task_id = ?", (tid,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert state == "pending"


def test_captain_lease_renewer_stops_and_propagates_fence_loss(monkeypatch):
    attempted = threading.Event()

    def lose_fence(_claims):
        attempted.set()
        raise RuntimeError("lease fence lost")

    monkeypatch.setattr(server, "_CAPTAIN_LEASE_RENEW_SECONDS", 0.01)
    monkeypatch.setattr(server, "_renew_kanban_notification_claims", lose_fence)
    handle = server._start_captain_lease_renewer([{"route": "captain"}])
    assert attempted.wait(timeout=1.0)
    with pytest.raises(RuntimeError, match="lease renewal failed"):
        server._stop_captain_lease_renewer(handle)
    assert handle[1].is_alive() is False


def test_ack_exception_releases_claim_for_retry(monkeypatch):
    profile = _profile_for(_session("ack-error"))
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="ack-error", assignee="worker")
        kb.register_captain_owner(conn, tid, profile=profile, origin_session_key=None)
        kb.complete_task(conn, tid, summary="retry after exception")
    finally:
        conn.close()

    session = _session("ack-error")
    _register_live("ack-error", session)
    claims = []
    assert len(_collect_kanban_notifications(session, claim_records=claims)) == 1

    monkeypatch.setattr(
        kb,
        "ack_captain_reports",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("db locked")),
    )
    with pytest.raises(RuntimeError, match="db locked"):
        server._settle_kanban_notification_claims(claims, accepted=True)

    assert _inbox_states() == {"pending": 1}
    retry_claims = []
    assert len(_collect_kanban_notifications(session, claim_records=retry_claims)) == 1


def test_zero_row_ack_is_failure_and_releases_claim_for_retry(monkeypatch):
    profile = _profile_for(_session("zero-ack"))
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="zero-ack", assignee="worker")
        kb.register_captain_owner(conn, tid, profile=profile, origin_session_key=None)
        kb.complete_task(conn, tid, summary="zero rows is not success")
    finally:
        conn.close()

    session = _session("zero-ack")
    _register_live("zero-ack", session)
    claims = []
    assert len(_collect_kanban_notifications(session, claim_records=claims)) == 1
    monkeypatch.setattr(kb, "ack_captain_reports", lambda *_args, **_kwargs: 0)

    with pytest.raises(RuntimeError, match="expected 1 row"):
        server._settle_kanban_notification_claims(claims, accepted=True)

    assert _inbox_states() == {"pending": 1}


def test_partial_multi_board_ack_releases_only_unsettled_token(monkeypatch):
    """A committed first board never makes a failed second-board ack successful."""
    profile = _profile_for(_session("partial-boards"))
    second_board = "captain-partial-second"
    kb.create_board(second_board)
    claims = []
    tokens = []
    for board in (kb.DEFAULT_BOARD, second_board):
        conn = kb.connect(board=board)
        try:
            tid = kb.create_task(conn, title=board, assignee="worker")
            kb.register_captain_owner(
                conn, tid, profile=profile, origin_session_key=None
            )
            kb.complete_task(conn, tid, summary=f"report from {board}")
            candidate = kb.read_captain_candidates(conn, profile=profile)[0]
            token, events = kb.lease_captain_reports(
                conn,
                profile=profile,
                owner="partial-boards",
                event_ids=[candidate["event_id"]],
            )
            assert len(events) == 1
            tokens.append(token)
            claims.append(
                {
                    "route": "captain",
                    "board": board,
                    "token": token,
                    "owner": "partial-boards",
                    "expected_count": 1,
                    "task_ids": {tid},
                    "deliveries": [
                        {
                            "id": f"kanban:{board}:{events[0].id}",
                            "event_id": events[0].id,
                        }
                    ],
                }
            )
        finally:
            conn.close()

    real_ack = kb.ack_captain_reports

    def fail_second_ack(conn, **kwargs):
        if kwargs["token"] == tokens[1]:
            raise RuntimeError("second board locked")
        return real_ack(conn, **kwargs)

    monkeypatch.setattr(kb, "ack_captain_reports", fail_second_ack)
    with pytest.raises(RuntimeError, match="second board locked"):
        server._settle_kanban_notification_claims(claims, accepted=True)

    first = kb.connect(board=kb.DEFAULT_BOARD)
    second = kb.connect(board=second_board)
    try:
        assert first.execute(
            "SELECT state FROM kanban_captain_inbox WHERE lease_token = ?",
            (tokens[0],),
        ).fetchone()[0] == "acked"
        assert second.execute(
            "SELECT state FROM kanban_captain_inbox"
        ).fetchone()[0] == "pending"
    finally:
        first.close()
        second.close()


def test_captain_delivery_uses_one_event_per_turn_across_boards():
    """One stable event identity per turn removes partial-batch UI ambiguity."""
    profile = _profile_for(_session("one-event-turn"))
    second_board = "captain-one-event-second"
    kb.create_board(second_board)
    for board in (kb.DEFAULT_BOARD, second_board):
        conn = kb.connect(board=board)
        try:
            tid = kb.create_task(conn, title=board, assignee="worker")
            kb.register_captain_owner(
                conn, tid, profile=profile, origin_session_key=None
            )
            kb.complete_task(conn, tid, summary=f"one from {board}")
        finally:
            conn.close()

    session = _session("one-event-turn")
    _register_live("one-event-turn", session)
    first_claims = []
    first_texts = _collect_kanban_notifications(session, claim_records=first_claims)
    assert len(first_texts) == 1
    first_ids = [
        delivery["id"]
        for record in first_claims
        for delivery in record.get("deliveries") or ()
    ]
    assert len(first_ids) == 1
    server._settle_kanban_notification_claims(first_claims, accepted=True)

    second_claims = []
    second_texts = _collect_kanban_notifications(session, claim_records=second_claims)
    assert len(second_texts) == 1
    second_ids = [
        delivery["id"]
        for record in second_claims
        for delivery in record.get("deliveries") or ()
    ]
    assert len(second_ids) == 1
    assert second_ids != first_ids


def test_captain_turn_does_not_batch_an_ordinary_subscription_event():
    profile = _profile_for(_session("captain-plus-ordinary"))
    second_board = "captain-ordinary-second"
    kb.create_board(second_board)

    conn = kb.connect()
    try:
        captain_tid = kb.create_task(conn, title="captain", assignee="worker")
        kb.register_captain_owner(
            conn, captain_tid, profile=profile, origin_session_key=None
        )
        kb.complete_task(conn, captain_tid, summary="captain event")
    finally:
        conn.close()

    conn = kb.connect(board=second_board)
    try:
        ordinary_tid = kb.create_task(conn, title="ordinary", assignee="worker")
        kb.add_notify_sub(
            conn,
            task_id=ordinary_tid,
            platform="tui",
            chat_id="captain-plus-ordinary",
            notifier_profile=profile,
        )
        kb.complete_task(conn, ordinary_tid, summary="ordinary event")
    finally:
        conn.close()

    session = _session("captain-plus-ordinary")
    _register_live("captain-plus-ordinary", session)
    first_claims = []
    first = _collect_kanban_notifications(session, claim_records=first_claims)
    assert len(first) == 1 and "captain event" in first[0]
    server._settle_kanban_notification_claims(first_claims, accepted=True)

    second_claims = []
    second = _collect_kanban_notifications(session, claim_records=second_claims)
    assert len(second) == 1 and "ordinary event" in second[0]


# ── requirement 5: synthetic turn rejection releases and retries ───────────
def test_dispatch_failure_releases_then_accepted_retry_no_replay():
    profile = _profile_for(_session("live-f"))
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cap5", assignee="worker")
        kb.register_captain_owner(conn, tid, profile=profile, origin_session_key=None)
        kb.complete_task(conn, tid, summary="retry-me")
    finally:
        conn.close()

    s = _session("live-f")
    _register_live("sid-f", s)

    c1 = []
    t1 = _collect_kanban_notifications(s, claim_records=c1)
    assert len(t1) == 1
    server._settle_kanban_notification_claims(c1, accepted=False)  # rejected
    assert _inbox_states() == {"pending": 1}  # released

    c2 = []
    t2 = _collect_kanban_notifications(s, claim_records=c2)
    assert len(t2) == 1
    assert t2[0] == t1[0]
    server._settle_kanban_notification_claims(c2, accepted=True)

    assert _collect_kanban_notifications(s, claim_records=[]) == []
    assert _inbox_states() == {"acked": 1}


# ── requirement 6: no duplicate when both routes see the same event ────────
def test_no_duplicate_across_exact_origin_and_captain_routes():
    origin = _session(ORIGIN_KEY)
    _register_live("sid-o", origin)
    profile = _profile_for(origin)

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cap6", assignee="worker")
        kb.add_notify_sub(
            conn, task_id=tid, platform="tui", chat_id=ORIGIN_KEY,
            notifier_profile=profile,
        )
        kb.register_captain_owner(
            conn, tid, profile=profile, origin_session_key=ORIGIN_KEY
        )
        kb.complete_task(conn, tid, summary="single report")
    finally:
        conn.close()

    claims = []
    texts = _collect_kanban_notifications(origin, claim_records=claims)
    assert len(texts) == 1  # deduplicated across both routes
    assert tid in texts[0]
    server._settle_kanban_notification_claims(claims, accepted=True)

    assert _collect_kanban_notifications(origin, claim_records=[]) == []
    # Captain row acked; the exact-origin sub is retained (done is reversible).
    assert _inbox_states() == {"acked": 1}
    conn = kb.connect()
    try:
        assert len(kb.list_notify_subs(conn, task_id=tid)) == 1
    finally:
        conn.close()


# ── requirement 7: unattached orchestrator card → inbox, no invented sub ───
def test_unattached_orchestrator_registers_without_notify_sub():
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="cap7",
            assignee="worker",
            captain_profile="orchestrator",
        )
        assert kb.list_notify_subs(conn, task_id=tid) == []
        kb.complete_task(conn, tid, summary="cron done")
        rows = conn.execute(
            "SELECT i.profile, i.state, r.origin_session_key "
            "FROM kanban_captain_inbox i "
            "JOIN kanban_captain_registry r ON r.task_id = i.task_id "
            "WHERE i.task_id = ?",
            (tid,),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["profile"] == "orchestrator"
        assert rows[0]["state"] == "pending"
        assert rows[0]["origin_session_key"] is None
        # No guessed platform/chat destination was invented.
        assert kb.list_notify_subs(conn, task_id=tid) == []
    finally:
        conn.close()


# ── requirement 8: reopen/new completion → new event, no acked replay ──────
def test_reopen_creates_new_report_without_replaying_acked():
    profile = _profile_for(_session("live-re"))
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cap8", assignee="worker")
        kb.register_captain_owner(conn, tid, profile=profile, origin_session_key=None)
        kb.complete_task(conn, tid, summary="first pass")
    finally:
        conn.close()

    s = _session("live-re")
    _register_live("sid-re", s)

    c1 = []
    t1 = _collect_kanban_notifications(s, claim_records=c1)
    assert len(t1) == 1 and "first pass" in t1[0]
    server._settle_kanban_notification_claims(c1, accepted=True)
    assert _collect_kanban_notifications(s, claim_records=[]) == []

    conn = kb.connect()
    try:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
            kb._append_event(conn, tid, "status", {"status": "ready"})
        assert kb.complete_task(conn, tid, summary="second pass")
    finally:
        conn.close()

    c2 = []
    t2 = _collect_kanban_notifications(s, claim_records=c2)
    assert len(t2) == 1 and "→ ready" in t2[0]
    server._settle_kanban_notification_claims(c2, accepted=True)
    c3 = []
    t3 = _collect_kanban_notifications(s, claim_records=c3)
    joined = "\n".join(t2 + t3)
    assert "second pass" in joined
    assert "first pass" not in joined  # acked cycle never replays
    server._settle_kanban_notification_claims(c3, accepted=True)
    assert _collect_kanban_notifications(s, claim_records=[]) == []


# ── migration / backward compatibility ─────────────────────────────────────
def test_missing_captain_tables_are_recreated_on_connect():
    conn = kb.connect()
    try:
        with kb.write_txn(conn):
            conn.execute("DROP TABLE kanban_captain_inbox")
            conn.execute("DROP TABLE kanban_captain_registry")
    finally:
        conn.close()

    # Force the next connect to re-run the idempotent schema init (as a fresh
    # process would after an upgrade).
    kb._INITIALIZED_PATHS.clear()
    conn = kb.connect()
    try:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "kanban_captain_inbox" in names
        assert "kanban_captain_registry" in names
        # A pre-existing task with no registry row is unaffected.
        tid = kb.create_task(conn, title="legacy", assignee="worker")
        kb.complete_task(conn, tid, summary="ok")
    finally:
        conn.close()


# ── archive cleanup must not drop an unreported pending completion ──────────
def test_archive_keeps_pending_then_purges_after_accepted_report():
    profile = _profile_for(_session("live-arc"))
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cap-arc", assignee="worker")
        kb.register_captain_owner(conn, tid, profile=profile, origin_session_key=None)
        kb.complete_task(conn, tid, summary="before archive")
        assert kb.archive_task(conn, tid)
        # Archive with unreported work retains the pending row + registration.
        unreported = conn.execute(
            "SELECT COUNT(*) FROM kanban_captain_inbox WHERE state != 'acked'"
        ).fetchone()[0]
        assert unreported == 1
        reg = conn.execute(
            "SELECT COUNT(*) FROM kanban_captain_registry WHERE task_id = ?",
            (tid,),
        ).fetchone()[0]
        assert reg == 1
    finally:
        conn.close()

    s = _session("live-arc")
    _register_live("sid-arc", s)
    claims = []
    texts = _collect_kanban_notifications(s, claim_records=claims)
    assert len(texts) == 1
    assert "before archive" in texts[0]
    server._settle_kanban_notification_claims(claims, accepted=True)

    conn = kb.connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM kanban_captain_inbox WHERE task_id = ?", (tid,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM kanban_captain_registry WHERE task_id = ?", (tid,)
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_archive_with_no_unreported_work_purges_directly():
    profile = _profile_for(_session("live-arc2"))
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cap-arc2", assignee="worker")
        kb.register_captain_owner(conn, tid, profile=profile, origin_session_key=None)
        kb.complete_task(conn, tid, summary="clean")
    finally:
        conn.close()

    s = _session("live-arc2")
    _register_live("sid-arc2", s)
    claims = []
    assert len(_collect_kanban_notifications(s, claim_records=claims)) == 1
    server._settle_kanban_notification_claims(claims, accepted=True)

    conn = kb.connect()
    try:
        assert kb.archive_task(conn, tid)
        assert conn.execute(
            "SELECT COUNT(*) FROM kanban_captain_registry WHERE task_id = ?", (tid,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM kanban_captain_inbox WHERE task_id = ?", (tid,)
        ).fetchone()[0] == 0
    finally:
        conn.close()


# ── bounded / privacy-safe formatting ──────────────────────────────────────
def test_captain_report_is_bounded_and_privacy_safe():
    profile = _profile_for(_session("live-p"))
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="cap-priv", assignee="worker",
            body="SECRET BODY LINE\nmore secret material",
        )
        kb.register_captain_owner(conn, tid, profile=profile, origin_session_key=None)
        kb.complete_task(conn, tid, summary="visible first\nhidden second line")
    finally:
        conn.close()

    s = _session("live-p")
    _register_live("sid-p", s)
    texts = _collect_kanban_notifications(s, claim_records=[])
    assert len(texts) == 1
    text = texts[0]
    assert tid in text
    assert "visible first" in text
    assert "hidden second" not in text  # only the first handoff line
    assert "SECRET BODY" not in text  # never the task body


def test_captain_report_omits_raw_error_and_force_redacts_secrets():
    profile = _profile_for(_session("live-secret"))
    fake_secret = "sk-" + "A" * 48
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cap-secret", assignee="worker")
        kb.register_captain_owner(conn, tid, profile=profile, origin_session_key=None)
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                tid,
                "gave_up",
                {"error": f"RAW_TOOL_OUTPUT_SHOULD_NOT_SURFACE {fake_secret}"},
            )
    finally:
        conn.close()

    s = _session("live-secret")
    _register_live("sid-secret", s)
    claims = []
    texts = _collect_kanban_notifications(s, claim_records=claims)
    assert len(texts) == 1
    assert "gave up" in texts[0]
    assert "RAW_TOOL_OUTPUT_SHOULD_NOT_SURFACE" not in texts[0]
    assert fake_secret not in texts[0]
    server._settle_kanban_notification_claims(claims, accepted=True)


def test_event_gc_preserves_unreported_captain_source_until_ack():
    profile = _profile_for(_session("live-gc"))
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cap-gc", assignee="worker")
        kb.register_captain_owner(conn, tid, profile=profile, origin_session_key=None)
        assert kb.complete_task(conn, tid, summary="survives event gc")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_events SET created_at = ? WHERE task_id = ?",
                (int(time.time()) - 3600, tid),
            )

        kb.gc_events(conn, older_than_seconds=60)
        pending_event = conn.execute(
            "SELECT event_id FROM kanban_captain_inbox "
            "WHERE task_id = ? AND state = 'pending'",
            (tid,),
        ).fetchone()
        assert pending_event is not None
        assert conn.execute(
            "SELECT 1 FROM task_events WHERE id = ?", (pending_event["event_id"],)
        ).fetchone() is not None
    finally:
        conn.close()

    s = _session("live-gc")
    _register_live("sid-gc", s)
    claims = []
    texts = _collect_kanban_notifications(s, claim_records=claims)
    assert len(texts) == 1
    assert "survives event gc" in texts[0]
    server._settle_kanban_notification_claims(claims, accepted=True)

    conn = kb.connect()
    try:
        assert kb.gc_events(conn, older_than_seconds=60) >= 1
        assert conn.execute(
            "SELECT 1 FROM task_events WHERE id = ?", (pending_event["event_id"],)
        ).fetchone() is None
    finally:
        conn.close()


# ── stats diagnostics for unreported Captain rows ──────────────────────────
def test_board_stats_reports_unreported_captain_backlog():
    conn = kb.connect()
    try:
        stats0 = kb.board_stats(conn)
        assert stats0["captain_unreported"]["count"] == 0

        tid = kb.create_task(conn, title="cap-stats", assignee="worker")
        kb.register_captain_owner(conn, tid, profile="default", origin_session_key=None)
        kb.complete_task(conn, tid, summary="pending report")
        stats = kb.board_stats(conn)
    finally:
        conn.close()

    cap = stats["captain_unreported"]
    assert cap["count"] == 1
    assert cap["oldest_age_seconds"] is not None
    assert cap["oldest_age_seconds"] >= 0


# ── repair 1: canonical profile normalization (mixed case) ─────────────────
def test_captain_profile_is_canonically_normalized_mixed_case():
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="cap-norm", assignee="worker", captain_profile="Otto"
        )
        kb.complete_task(conn, tid, summary="norm test")

        reg = conn.execute(
            "SELECT profile FROM kanban_captain_registry WHERE task_id = ?", (tid,)
        ).fetchone()
        assert reg["profile"] == "otto"  # normalize_profile_name canonicalization
        inbox = conn.execute(
            "SELECT profile FROM kanban_captain_inbox WHERE task_id = ?", (tid,)
        ).fetchone()
        assert inbox["profile"] == "otto"

        # A differently-cased lookup still resolves to the same owner.
        assert len(kb.read_captain_candidates(conn, profile="OTTO")) == 1
        assert kb.count_captain_pending(board=None, profile="Otto") == 1
    finally:
        conn.close()


def test_mixed_case_profile_session_claims_its_own_rows():
    """A session whose profile differs only in case still claims its reports."""
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="cap-norm2", assignee="worker", captain_profile="otto"
        )
        kb.complete_task(conn, tid, summary="cross-case claim")
    finally:
        conn.close()

    session = _session("live-otto", profile_home=None)
    # Force a mixed-case bound profile via profile_home dir name.
    import tempfile
    home = tempfile.mkdtemp()
    import os
    otto_home = os.path.join(home, "Otto")
    os.makedirs(otto_home)
    session["profile_home"] = otto_home
    _register_live("sid-otto", session)

    assert _profile_for(session) == "otto"
    claims = []
    texts = _collect_kanban_notifications(session, claim_records=claims)
    assert len(texts) == 1
    server._settle_kanban_notification_claims(claims, accepted=True)
    assert _inbox_states() == {"acked": 1}


# ── repair 2: bounded per-profile stats breakdown ──────────────────────────
def test_board_stats_captain_per_profile_breakdown():
    conn = kb.connect()
    try:
        for i, prof in enumerate(["alpha", "beta", "beta"]):
            tid = kb.create_task(
                conn, title=f"s{i}", assignee="worker", captain_profile=prof
            )
            kb.complete_task(conn, tid, summary="x")
        stats = kb.board_stats(conn)
    finally:
        conn.close()

    cap = stats["captain_unreported"]
    assert cap["count"] == 3
    assert cap["oldest_age_seconds"] is not None
    by = cap["by_profile"]
    assert isinstance(by, list)
    assert len(by) <= 20
    profiles = {r["profile"]: r for r in by}
    assert profiles["beta"]["count"] == 2
    assert profiles["alpha"]["count"] == 1
    for r in by:
        # Bounded, ownership-only shape — never task/event payloads.
        assert set(r.keys()) == {"profile", "count", "oldest_age_seconds"}
        assert r["oldest_age_seconds"] is not None and r["oldest_age_seconds"] >= 0
    assert cap["profiles_truncated"] == 0


def test_board_stats_captain_per_profile_truncates_at_20():
    conn = kb.connect()
    try:
        for i in range(23):
            tid = kb.create_task(
                conn,
                title=f"t{i}",
                assignee="worker",
                captain_profile=f"prof{i:02d}",
            )
            kb.complete_task(conn, tid, summary="x")
        stats = kb.board_stats(conn)
    finally:
        conn.close()

    cap = stats["captain_unreported"]
    assert cap["count"] == 23
    assert len(cap["by_profile"]) == 20
    assert cap["profiles_truncated"] == 3


# ── repair 3: no ack without a settlement handshake ────────────────────────
def test_collector_without_claim_records_never_consumes_pending():
    profile = _profile_for(_session("live-none"))
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cap-none", assignee="worker")
        kb.register_captain_owner(conn, tid, profile=profile, origin_session_key=None)
        kb.complete_task(conn, tid, summary="must stay pending")
    finally:
        conn.close()

    s = _session("live-none")
    _register_live("sid-none", s)

    # No settlement list supplied → the helper must not lease/ack the row.
    _collect_kanban_notifications(s)  # claim_records defaults to None
    assert _inbox_states() == {"pending": 1}

    # Direct helper call with claim_records=None is likewise inert.
    conn = kb.connect()
    try:
        server._collect_captain_reports(
            conn, kb.DEFAULT_BOARD, s, profile, set(), None, []
        )
    finally:
        conn.close()
    assert _inbox_states() == {"pending": 1}

    # A properly settled poll still delivers exactly once afterwards.
    claims = []
    texts = _collect_kanban_notifications(s, claim_records=claims)
    assert len(texts) == 1
    server._settle_kanban_notification_claims(claims, accepted=True)
    assert _inbox_states() == {"acked": 1}


# ── repair 4: loop-level reject→release→accept→ack→no-replay ────────────────
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


def test_poller_loop_reject_releases_then_accept_acks_no_replay(monkeypatch):
    from tools.process_registry import process_registry

    loop_key = "captain-loop-session"
    session = {
        "session_key": loop_key,
        "history_lock": threading.Lock(),
        "running": False,
    }
    profile = _profile_for(session)

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cap-loop", assignee="worker")
        kb.register_captain_owner(conn, tid, profile=profile, origin_session_key=None)
        kb.complete_task(conn, tid, summary="loop delivery")
    finally:
        conn.close()

    attempts: list[str] = []
    completion_ids: list[str | None] = []
    turn_purposes: list[str | None] = []

    def submit(_rid, _sid, _session, text, **kwargs):
        attempts.append(text)
        completion_ids.append(kwargs.get("completion_id"))
        turn_purposes.append(kwargs.get("turn_purpose"))
        if len(attempts) == 1:
            return False
        kwargs["on_terminal"](True)
        return True

    def run_once():
        monkeypatch.setattr(
            process_registry, "completion_queue", _EmptyCompletionQueue()
        )
        monkeypatch.setattr(server, "_maybe_fire_tui_loop_tick", lambda *_a: None)
        monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
        monkeypatch.setattr(server, "_run_prompt_submit", submit)
        server._notification_poller_loop(_StopAfterOnePoll(), "sid-loop", session)

    # Poll 1 — rejection releases the row to pending and clears the reservation.
    run_once()
    assert len(attempts) == 1
    assert tid in attempts[0]
    assert _inbox_states() == {"pending": 1}
    assert session["running"] is False

    # Poll 2 — accepted retry acks the same event (no new/replayed event).
    run_once()
    assert len(attempts) == 2
    assert attempts[1] == attempts[0]
    assert completion_ids[0]
    assert completion_ids[1] == completion_ids[0]
    assert turn_purposes == ["captain_report", "captain_report"]
    assert _inbox_states() == {"acked": 1}

    # Turn finishes; a later idle poll must not replay the acked report.
    with session["history_lock"]:
        session["running"] = False
    run_once()
    assert len(attempts) == 2
    assert _inbox_states() == {"acked": 1}


def test_poller_keeps_lease_when_failed_turn_could_not_rollback_input(monkeypatch):
    from tools.process_registry import process_registry

    loop_key = "captain-rollback-failed"
    session = {
        "session_key": loop_key,
        "history_lock": threading.Lock(),
        "running": False,
    }
    profile = _profile_for(session)
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cap-rollback", assignee="worker")
        kb.register_captain_owner(conn, tid, profile=profile, origin_session_key=None)
        kb.complete_task(conn, tid, summary="rollback failed")
    finally:
        conn.close()

    terminal_values = []

    def submit(_rid, _sid, _session, _text, **kwargs):
        terminal_values.append(None)
        kwargs["on_terminal"](None)
        return True

    monkeypatch.setattr(process_registry, "completion_queue", _EmptyCompletionQueue())
    monkeypatch.setattr(server, "_maybe_fire_tui_loop_tick", lambda *_a: None)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_run_prompt_submit", submit)

    server._notification_poller_loop(_StopAfterOnePoll(), "sid-rollback", session)

    assert terminal_values == [None]
    assert _inbox_states() == {"leased": 1}


def test_poller_defers_captain_claim_for_codex_app_server(monkeypatch):
    """Unsupported Codex runtime leaves the durable report retryable."""
    from tools.process_registry import process_registry

    loop_key = "captain-codex-deferred"
    session = {
        "session_key": loop_key,
        "history_lock": threading.Lock(),
        "running": False,
        "agent": SimpleNamespace(api_mode="codex_app_server"),
    }
    profile = _profile_for(session)

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cap-codex", assignee="worker")
        kb.register_captain_owner(conn, tid, profile=profile, origin_session_key=None)
        kb.complete_task(conn, tid, summary="retry on supported runtime")
    finally:
        conn.close()

    monkeypatch.setattr(process_registry, "completion_queue", _EmptyCompletionQueue())
    monkeypatch.setattr(server, "_maybe_fire_tui_loop_tick", lambda *_a: None)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    submit_calls = []

    def submit(*args, **kwargs):
        submit_calls.append((args, kwargs))
        raise AssertionError("Captain turn must be deferred")

    monkeypatch.setattr(server, "_run_prompt_submit", submit)

    server._notification_poller_loop(_StopAfterOnePoll(), "sid-codex", session)

    assert submit_calls == []
    assert _inbox_states() == {"pending": 1}
    assert session["running"] is False


def test_captain_completion_identity_namespaces_board_local_delivery_ids():
    board_a = [{"board": "alpha", "deliveries": [{"id": "7"}]}]
    board_b = [{"board": "beta", "deliveries": [{"id": "7"}]}]

    assert server._captain_completion_id(board_a, ["same report"]) != (
        server._captain_completion_id(board_b, ["same report"])
    )
    assert server._captain_completion_id(
        [
            {"board": "alpha", "deliveries": [{"id": "8"}]},
            {"board": "alpha", "deliveries": [{"id": "7"}]},
        ],
        [],
    ) == server._captain_completion_id(
        [
            {"board": "alpha", "deliveries": [{"id": "7"}]},
            {"board": "alpha", "deliveries": [{"id": "8"}]},
        ],
        [],
    )


@pytest.mark.parametrize("ack_failure", ["exception", "zero_rows"])
def test_persisted_captain_report_reconciles_across_same_profile_sessions(
    monkeypatch, ack_failure
):
    """A profile-wide receipt moves to the fallback session without a new turn."""
    from hermes_state import SessionDB
    from tools.process_registry import process_registry

    first_session_id = "captain-reconcile-session-a"
    fallback_session_id = "captain-reconcile-session-b"
    db = SessionDB()
    db.create_session(session_id=first_session_id, source="tui", model="test")
    db.create_session(session_id=fallback_session_id, source="tui", model="test")
    profile = _profile_for(_session(first_session_id))

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="captain-reconcile", assignee="worker")
        kb.register_captain_owner(conn, tid, profile=profile, origin_session_key=None)
        kb.complete_task(conn, tid, summary="durable once")
    finally:
        conn.close()

    attempts = []
    submitted_prompts = []
    real_ack = kb.ack_captain_reports
    ack_calls = 0

    def fail_first_ack(conn, **kwargs):
        nonlocal ack_calls
        ack_calls += 1
        if ack_calls == 1:
            if ack_failure == "exception":
                raise RuntimeError("ack connection lost")
            return 0
        return real_ack(conn, **kwargs)

    def submit(_rid, _sid, active_session, text, **kwargs):
        completion_id = kwargs["completion_id"]
        attempts.append(completion_id)
        submitted_prompts.append(text)
        active_session_id = active_session["agent"].session_id
        # Persist the source turn across separate writes so ordinary rows from
        # the fallback session interleave in the global AUTOINCREMENT id range.
        # Reconciliation must append only the exact source row ids, not every
        # destination row whose id happens to lie between them.
        db.append_message(active_session_id, "user", content=text)
        db.append_messages_batch(
            fallback_session_id,
            [
                {"role": "user", "content": "fallback ordinary question"},
                {"role": "assistant", "content": "fallback ordinary answer"},
            ],
        )
        db.append_message(
            active_session_id,
            "assistant",
            content="Captain durable report",
            display_metadata={"captain_completion_id": completion_id},
        )
        kwargs["on_terminal"](True)
        return True

    monkeypatch.setattr(kb, "ack_captain_reports", fail_first_ack)
    monkeypatch.setattr(process_registry, "completion_queue", _EmptyCompletionQueue())
    monkeypatch.setattr(server, "_maybe_fire_tui_loop_tick", lambda *_a: None)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_run_prompt_submit", submit)

    first = {
        **_session(first_session_id),
        "agent": SimpleNamespace(_session_db=db, session_id=first_session_id),
        "history": [],
        "history_lock": threading.Lock(),
        "running": False,
    }
    _register_live("first-runtime", first)
    server._notification_poller_loop(_StopAfterOnePoll(), "first-runtime", first)
    assert len(attempts) == 1
    assert _inbox_states() == {"pending": 1}

    # Session A disappears after committing the receipt but before ack. A
    # distinct same-profile session B must reconcile that receipt profile-wide.
    server._sessions.clear()
    fallback = {
        **_session(fallback_session_id),
        "agent": SimpleNamespace(_session_db=db, session_id=fallback_session_id),
        "history": db.get_messages_as_conversation(fallback_session_id),
        "history_lock": threading.Lock(),
        "running": False,
    }
    _register_live("fallback-runtime", fallback)
    server._notification_poller_loop(
        _StopAfterOnePoll(), "fallback-runtime", fallback
    )

    assert len(attempts) == 1
    assert _inbox_states() == {"acked": 1}
    first_rows = db.get_messages_as_conversation(first_session_id)
    fallback_rows = db.get_messages_as_conversation(fallback_session_id)
    captain_rows = [
        row
        for row in [*first_rows, *fallback_rows]
        if row.get("role") == "assistant"
        and (row.get("display_metadata") or {}).get("captain_completion_id")
        == attempts[0]
    ]
    assert [row["content"] for row in captain_rows] == ["Captain durable report"]
    assert all(row.get("content") != "Captain durable report" for row in first_rows)
    assert db.get_session(first_session_id)["message_count"] == 0
    assert db.get_session(fallback_session_id)["message_count"] == 4
    assert [row.get("content") for row in fallback["history"]] == [
        "fallback ordinary question",
        "fallback ordinary answer",
        submitted_prompts[0],
        "Captain durable report",
    ]
    visible = server._history_to_messages(fallback["history"])
    assert [
        row["text"]
        for row in visible
        if row.get("role") == "assistant"
        and row.get("text") == "Captain durable report"
    ] == ["Captain durable report"]
    db.close()


def test_reconciliation_ack_failures_do_not_stop_poller_or_receiver_heartbeats(
    monkeypatch,
):
    """Two recoverable receipt-ack failures stay inside one live poll loop."""
    from hermes_state import SessionDB
    from tools.process_registry import process_registry

    session_id = "captain-reconcile-survives"
    db = SessionDB()
    db.create_session(session_id=session_id, source="tui", model="test")
    profile = _profile_for(_session(session_id))
    session = {
        **_session(session_id),
        "agent": SimpleNamespace(_session_db=db, session_id=session_id),
        "history": [],
        "history_lock": threading.Lock(),
        "running": False,
    }
    _register_live("surviving-runtime", session)

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="captain-survives", assignee="worker")
        kb.register_captain_owner(conn, tid, profile=profile, origin_session_key=None)
        kb.complete_task(conn, tid, summary="retry ack twice")
    finally:
        conn.close()

    seed_claims = []
    seed_texts = _collect_kanban_notifications(session, claim_records=seed_claims)
    completion_id = server._captain_completion_id(seed_claims, seed_texts)
    server._settle_kanban_notification_claims(seed_claims, accepted=False)
    db.append_messages_batch(
        session_id,
        [
            {"role": "user", "content": seed_texts[0]},
            {
                "role": "assistant",
                "content": "Captain survives transient ack failures",
                "display_metadata": {"captain_completion_id": completion_id},
            },
        ],
    )

    real_ack = kb.ack_captain_reports
    ack_calls = 0
    heartbeat_calls = 0

    def fail_two_acks(conn, **kwargs):
        nonlocal ack_calls
        ack_calls += 1
        if ack_calls <= 2:
            raise RuntimeError(f"transient ack failure {ack_calls}")
        return real_ack(conn, **kwargs)

    def record_heartbeat(_session):
        nonlocal heartbeat_calls
        heartbeat_calls += 1

    class _StopAfterThreePolls(threading.Event):
        def __init__(self):
            super().__init__()
            self.polls = 0

        def is_set(self):
            self.polls += 1
            return self.polls > 3

    monkeypatch.setattr(kb, "ack_captain_reports", fail_two_acks)
    monkeypatch.setattr(process_registry, "completion_queue", _EmptyCompletionQueue())
    monkeypatch.setattr(server, "_KANBAN_POLL_SECONDS", 0.0)
    monkeypatch.setattr(server, "_maybe_fire_tui_loop_tick", lambda *_a: None)
    monkeypatch.setattr(server, "_touch_captain_receivers", record_heartbeat)
    monkeypatch.setattr(
        server,
        "_run_prompt_submit",
        lambda *_a, **_k: pytest.fail("persisted receipt must not invoke the model"),
    )
    monkeypatch.setattr(server, "_emit", lambda *_a, **_k: None)

    server._notification_poller_loop(
        _StopAfterThreePolls(), "surviving-runtime", session
    )

    assert ack_calls == 3
    assert heartbeat_calls == 3
    assert _inbox_states() == {"acked": 1}
    assert session["running"] is False
    db.close()


# ── Gauge repair: cross-process ownership / tenant / bounds / retention ────
def test_cross_process_live_origin_wins_shared_captain_claim():
    """Durable receiver heartbeats, not process-local ``_sessions``, pick origin."""
    origin = _session(ORIGIN_KEY)
    sibling = _session(SIBLING_KEY)
    profile = _profile_for(origin)

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cross-process", assignee="worker")
        kb.add_notify_sub(
            conn, task_id=tid, platform="tui", chat_id=ORIGIN_KEY,
            notifier_profile=profile,
        )
        kb.register_captain_owner(
            conn, tid, profile=profile, origin_session_key=ORIGIN_KEY
        )
        kb.touch_captain_receiver(
            conn, profile=profile, session_key=ORIGIN_KEY, tenant=None
        )
        kb.touch_captain_receiver(
            conn, profile=profile, session_key=SIBLING_KEY, tenant=None
        )
        kb.complete_task(conn, tid, summary="one delivery")
    finally:
        conn.close()

    # Process B sees only itself. Durable origin liveness must still prevent
    # it from taking the fallback route.
    server._sessions.clear()
    _register_live("sid-sibling", sibling)
    sibling_claims = []
    assert _collect_kanban_notifications(sibling, claim_records=sibling_claims) == []
    assert sibling_claims == []

    # Process A likewise sees only itself and wins the ONE shared Captain lease.
    server._sessions.clear()
    _register_live("sid-origin", origin)
    origin_claims = []
    texts = _collect_kanban_notifications(origin, claim_records=origin_claims)
    assert len(texts) == 1
    assert tid in texts[0]
    assert sum(len(r.get("deliveries") or []) for r in origin_claims) == 1
    server._settle_kanban_notification_claims(origin_claims, accepted=True)
    assert _collect_kanban_notifications(origin, claim_records=[]) == []


def test_busy_origin_heartbeat_stays_fresh_before_any_event(monkeypatch):
    """A long-running origin publishes liveness without pending Captain work."""
    from tools.process_registry import process_registry

    origin = {
        **_session(ORIGIN_KEY),
        "history_lock": threading.Lock(),
        "running": True,
    }
    profile = _profile_for(origin)
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="idle-before-event", assignee="worker")
        kb.register_captain_owner(
            conn, tid, profile=profile, origin_session_key=ORIGIN_KEY
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE kanban_captain_receivers SET last_seen = ?",
                (int(time.time()) - server._CAPTAIN_RECEIVER_TTL_SECONDS - 1,),
            )
    finally:
        conn.close()

    monkeypatch.setattr(process_registry, "completion_queue", _EmptyCompletionQueue())
    monkeypatch.setattr(server, "_maybe_fire_tui_loop_tick", lambda *_a: None)
    server._notification_poller_loop(_StopAfterOnePoll(), "sid-origin", origin)

    conn = kb.connect()
    try:
        assert kb.captain_receiver_is_live(
            conn,
            profile=profile,
            session_key=ORIGIN_KEY,
            max_age_seconds=server._CAPTAIN_RECEIVER_TTL_SECONDS,
        )
    finally:
        conn.close()


def test_lease_rechecks_origin_liveness_atomically_after_candidate_read(monkeypatch):
    """A sibling cannot lease after the origin refreshes during its claim."""
    origin = _session(ORIGIN_KEY)
    sibling = _session(SIBLING_KEY)
    profile = _profile_for(origin)
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="atomic-origin", assignee="worker")
        kb.register_captain_owner(
            conn, tid, profile=profile, origin_session_key=ORIGIN_KEY
        )
        kb.complete_task(conn, tid, summary="origin still owns this")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE kanban_captain_receivers SET last_seen = ?",
                (int(time.time()) - server._CAPTAIN_RECEIVER_TTL_SECONDS - 1,),
            )
    finally:
        conn.close()

    real_lease = kb.lease_captain_reports

    def refresh_origin_then_lease(conn, **kwargs):
        if kwargs.get("owner") == SIBLING_KEY:
            kb.touch_captain_receiver(
                conn,
                profile=profile,
                session_key=ORIGIN_KEY,
                tenant=None,
            )
        return real_lease(conn, **kwargs)

    monkeypatch.setattr(kb, "lease_captain_reports", refresh_origin_then_lease)
    server._sessions.clear()
    _register_live("sid-sibling", sibling)
    assert _collect_kanban_notifications(sibling, claim_records=[]) == []
    assert _inbox_states() == {"pending": 1}

    server._sessions.clear()
    _register_live("sid-origin", origin)
    claims = []
    texts = _collect_kanban_notifications(origin, claim_records=claims)
    assert len(texts) == 1 and "origin still owns this" in texts[0]
    server._settle_kanban_notification_claims(claims, accepted=True)


def test_tenant_tagged_unattached_report_has_no_fallback_without_session_identity():
    profile = _profile_for(_session("tenant-origin"))
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="tenant-a", assignee="worker", tenant="tenant-a"
        )
        kb.register_captain_owner(
            conn, tid, profile=profile, origin_session_key=None, tenant="tenant-a"
        )
        kb.complete_task(conn, tid, summary="tenant private")
    finally:
        conn.close()

    unscoped = _session("unscoped")
    wrong = {**_session("tenant-b"), "tenant": "tenant-b"}
    fabricated_matching = {**_session("tenant-a"), "tenant": "tenant-a"}
    for sid, candidate in (("u", unscoped), ("b", wrong), ("a", fabricated_matching)):
        server._sessions.clear()
        _register_live(sid, candidate)
        assert _collect_kanban_notifications(candidate, claim_records=[]) == []
        assert _inbox_states() == {"pending": 1}


def test_exact_origin_preserved_for_tenant_task_without_receiver_binding():
    origin = _session(ORIGIN_KEY)
    profile = _profile_for(origin)
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="tenant-origin", assignee="worker", tenant="tenant-a"
        )
        kb.register_captain_owner(
            conn, tid, profile=profile, origin_session_key=ORIGIN_KEY,
            tenant="tenant-a",
        )
        kb.complete_task(conn, tid, summary="exact origin")
    finally:
        conn.close()

    _register_live("origin", origin)
    claims = []
    assert len(_collect_kanban_notifications(origin, claim_records=claims)) == 1
    server._settle_kanban_notification_claims(claims, accepted=True)


def test_backlog_is_paged_under_strict_row_and_byte_caps():
    profile = _profile_for(_session("paged"))
    total = server._CAPTAIN_POLL_ROW_CAP * 2 + 3
    conn = kb.connect()
    try:
        for i in range(total):
            tid = kb.create_task(conn, title=f"paged-{i:04d}", assignee="worker")
            kb.register_captain_owner(
                conn, tid, profile=profile, origin_session_key=None
            )
            kb.complete_task(conn, tid, summary="x" * 300)
    finally:
        conn.close()

    session = _session("paged")
    _register_live("paged", session)
    delivered = []
    page_sizes = []
    while True:
        claims = []
        texts = _collect_kanban_notifications(session, claim_records=claims)
        if not texts:
            break
        page_sizes.append(len(texts))
        assert len(texts) <= server._CAPTAIN_POLL_ROW_CAP
        assert len("\n".join(texts).encode("utf-8")) <= server._CAPTAIN_POLL_BYTE_CAP
        delivered.extend(texts)
        server._settle_kanban_notification_claims(claims, accepted=True)

    assert len(delivered) == total
    assert len(page_sizes) >= 3
    assert _inbox_states() == {"acked": total}


def test_row_cap_is_global_across_multiple_boards():
    profile = _profile_for(_session("global-cap"))
    second_board = "captain-second"
    kb.create_board(second_board)
    for board in (kb.DEFAULT_BOARD, second_board):
        conn = kb.connect(board=board)
        try:
            for index in range(server._CAPTAIN_POLL_ROW_CAP):
                tid = kb.create_task(
                    conn, title=f"{board}-{index}", assignee="worker"
                )
                kb.register_captain_owner(
                    conn, tid, profile=profile, origin_session_key=None
                )
                kb.complete_task(conn, tid, summary="global page")
        finally:
            conn.close()

    session = _session("global-cap")
    _register_live("global-cap", session)
    claims = []
    texts = _collect_kanban_notifications(session, claim_records=claims)

    assert len(texts) == server._CAPTAIN_TURN_ROW_CAP
    assert sum(
        1 for record in claims if record.get("route") == "captain"
    ) == server._CAPTAIN_TURN_ROW_CAP


def test_ineligible_first_page_cannot_starve_later_fallback_report():
    profile = _profile_for(_session("fallback-reader"))
    conn = kb.connect()
    try:
        for idx in range(server._CAPTAIN_POLL_ROW_CAP):
            tid = kb.create_task(conn, title=f"owned-{idx}", assignee="worker")
            kb.register_captain_owner(
                conn,
                tid,
                profile=profile,
                origin_session_key=f"live-origin-{idx}",
            )
            kb.complete_task(conn, tid, summary="origin owns this")
        fallback_tid = kb.create_task(conn, title="fallback", assignee="worker")
        kb.register_captain_owner(
            conn, fallback_tid, profile=profile, origin_session_key=None
        )
        kb.complete_task(conn, fallback_tid, summary="must not starve")
    finally:
        conn.close()

    fallback = _session("fallback-reader")
    _register_live("fallback-reader", fallback)
    claims = []
    texts = _collect_kanban_notifications(fallback, claim_records=claims)
    assert len(texts) == 1
    assert "must not starve" in texts[0]
    server._settle_kanban_notification_claims(claims, accepted=True)


def test_byte_cap_releases_unrendered_rows_for_next_page(monkeypatch):
    profile = _profile_for(_session("byte-cap"))
    conn = kb.connect()
    try:
        for idx in range(2):
            tid = kb.create_task(conn, title=f"byte-cap-{idx}", assignee="worker")
            kb.register_captain_owner(
                conn, tid, profile=profile, origin_session_key=None
            )
            kb.complete_task(conn, tid, summary=f"report-{idx}")
    finally:
        conn.close()

    monkeypatch.setattr(
        server,
        "_format_kanban_event_text",
        lambda _sub, _task, ev, _slug, **_kwargs: (
            f"event:{ev.id}:" + "x" * server._CAPTAIN_POLL_BYTE_CAP
        ),
    )
    session = _session("byte-cap")
    _register_live("byte-cap", session)

    first_claims = []
    first = _collect_kanban_notifications(session, claim_records=first_claims)
    assert len(first) == 1
    server._settle_kanban_notification_claims(first_claims, accepted=True)

    second_claims = []
    second = _collect_kanban_notifications(session, claim_records=second_claims)
    assert len(second) == 1
    assert second[0] != first[0]
    server._settle_kanban_notification_claims(second_claims, accepted=True)


def test_gc_compacts_old_acked_rows_and_reopen_gets_new_event():
    profile = _profile_for(_session("gc-acked"))
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="gc-acked", assignee="worker")
        kb.register_captain_owner(conn, tid, profile=profile, origin_session_key=None)
        kb.complete_task(conn, tid, summary="first")
    finally:
        conn.close()

    session = _session("gc-acked")
    _register_live("gc-acked", session)
    claims = []
    assert len(_collect_kanban_notifications(session, claim_records=claims)) == 1
    server._settle_kanban_notification_claims(claims, accepted=True)

    conn = kb.connect()
    try:
        old = int(time.time()) - 3600
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE kanban_captain_inbox SET updated_at = ? WHERE task_id = ?",
                (old, tid),
            )
            conn.execute("UPDATE task_events SET created_at = ? WHERE task_id = ?", (old, tid))
        assert kb.gc_events(conn, older_than_seconds=60) >= 1
        assert conn.execute(
            "SELECT COUNT(*) FROM kanban_captain_inbox WHERE task_id = ?", (tid,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM kanban_captain_registry WHERE task_id = ?", (tid,)
        ).fetchone()[0] == 1

        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
            kb._append_event(conn, tid, "status", {"status": "ready"})
        kb.complete_task(conn, tid, summary="second")
    finally:
        conn.close()

    claims = []
    first = _collect_kanban_notifications(session, claim_records=claims)
    assert len(first) == 1 and "→ ready" in first[0]
    server._settle_kanban_notification_claims(claims, accepted=True)
    claims = []
    second = _collect_kanban_notifications(session, claim_records=claims)
    assert len(second) == 1
    assert "second" in second[0]
