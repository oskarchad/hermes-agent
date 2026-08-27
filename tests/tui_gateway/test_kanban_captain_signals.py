"""Contracts for profile-scoped Captain report persistence."""

import json

import pytest

from hermes_cli import kanban_db as kb


def _captain_owner(conn, task_id: str):
    return conn.execute(
        "SELECT profile, origin_session_key, tenant "
        "FROM kanban_captain_registry WHERE task_id = ?",
        (task_id,),
    ).fetchone()


def test_connect_installs_captain_persistence_schema(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        names = {
            str(row["name"])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        inbox_columns = {
            str(row["name"])
            for row in conn.execute(
                "PRAGMA table_info(kanban_captain_inbox)"
            ).fetchall()
        }
    finally:
        conn.close()

    assert {
        "kanban_captain_registry",
        "kanban_captain_inbox",
        "kanban_captain_receivers",
    } <= names
    assert "source_comment_id" in inbox_columns


def test_root_creation_registers_configured_orchestrator_as_captain(
    tmp_path, monkeypatch
):
    from hermes_cli import config, profiles

    monkeypatch.setattr(
        config,
        "load_config",
        lambda: {"kanban": {"orchestrator_profile": "Otto"}},
    )
    monkeypatch.setattr(profiles, "profile_exists", lambda name: name.lower() == "otto")
    monkeypatch.setenv("HERMES_SESSION_KEY", "otto-origin")

    conn = kb.connect(tmp_path / "kanban.db")
    try:
        task_id = kb.create_task(
            conn,
            title="root",
            assignee="worker",
            tenant="shop",
            captain_origin_session_key="otto-origin",
        )
        owner = _captain_owner(conn, task_id)
    finally:
        conn.close()

    assert dict(owner) == {
        "profile": "otto",
        "origin_session_key": "otto-origin",
        "tenant": "shop",
    }


def test_child_creation_inherits_parent_captain_owner_and_origin(
    tmp_path, monkeypatch
):
    from hermes_cli import config, profiles

    monkeypatch.setattr(
        config,
        "load_config",
        lambda: {"kanban": {"orchestrator_profile": "otto"}},
    )
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    monkeypatch.setenv("HERMES_SESSION_KEY", "root-origin")

    conn = kb.connect(tmp_path / "kanban.db")
    try:
        parent_id = kb.create_task(
            conn,
            title="parent",
            assignee="planner",
            captain_origin_session_key="root-origin",
        )
        monkeypatch.setenv("HERMES_SESSION_KEY", "worker-origin")
        child_id = kb.create_task(
            conn,
            title="child",
            assignee="worker",
            parents=[parent_id],
            captain_profile="worker",
            captain_origin_session_key="worker-origin",
        )
        owner = _captain_owner(conn, child_id)
    finally:
        conn.close()

    assert dict(owner) == {
        "profile": "otto",
        "origin_session_key": "root-origin",
        "tenant": None,
    }


@pytest.mark.parametrize(
    ("header", "signal_class"),
    [
        ("METHOD DELTA", "method_delta"),
        ("DECISION REQUIRED —", "decision_required"),
        ("CAPTAIN NOTE:", "captain_note"),
    ],
)
def test_recognized_comment_signal_materializes_only_reference_metadata(
    tmp_path, header, signal_class
):
    conn = kb.connect(tmp_path / f"{signal_class}.db")
    try:
        task_id = kb.create_task(conn, title="signal target", assignee="worker")
        comment_id = kb.add_comment(
            conn,
            task_id,
            author="wrench",
            body=f"\n{header}\ninspect the durable comment body",
        )
        rows = conn.execute(
            "SELECT id, payload FROM task_events "
            "WHERE task_id = ? AND kind = 'captain_signal'",
            (task_id,),
        ).fetchall()
        inbox = conn.execute(
            "SELECT event_id, source_comment_id, kind, state "
            "FROM kanban_captain_inbox WHERE task_id = ? AND kind = 'captain_signal'",
            (task_id,),
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert payload == {
        "author": "wrench",
        "comment_id": comment_id,
        "signal_class": signal_class,
    }
    assert "durable comment body" not in rows[0]["payload"]
    assert [dict(row) for row in inbox] == [
        {
            "event_id": rows[0]["id"],
            "source_comment_id": comment_id,
            "kind": "captain_signal",
            "state": "pending",
        }
    ]


@pytest.mark.parametrize(
    "body",
    [
        "normal progress update",
        "CAPTAIN SIGNAL: legacy header must stay ordinary",
        "CAPTAIN STEER: legacy header must stay ordinary",
        "CAPTAIN NEEDS DECISION: legacy header must stay ordinary",
        "METHOD DELTAX must not prefix-match",
        "Method delta is unchanged from the plan",
        "Decision required for next sprint",
        "Captain note was added to the runbook",
    ],
)
def test_ordinary_comment_stays_on_existing_comment_path(tmp_path, body):
    conn = kb.connect(tmp_path / "ordinary.db")
    try:
        task_id = kb.create_task(conn, title="ordinary", assignee="worker")
        kb.add_comment(conn, task_id, author="wrench", body=body)
        signal_events = conn.execute(
            "SELECT COUNT(*) FROM task_events "
            "WHERE task_id = ? AND kind = 'captain_signal'",
            (task_id,),
        ).fetchone()[0]
        signal_inbox = conn.execute(
            "SELECT COUNT(*) FROM kanban_captain_inbox "
            "WHERE task_id = ? AND kind = 'captain_signal'",
            (task_id,),
        ).fetchone()[0]
    finally:
        conn.close()

    assert signal_events == 0
    assert signal_inbox == 0


def test_captain_signal_materialization_is_idempotent_by_source_comment(tmp_path):
    materialize = getattr(kb, "materialize_captain_signal", None)
    assert callable(materialize)

    conn = kb.connect(tmp_path / "dedupe.db")
    try:
        task_id = kb.create_task(conn, title="dedupe", assignee="worker")
        comment_id = kb.add_comment(
            conn,
            task_id,
            author="wrench",
            body="CAPTAIN NOTE: retry this durable source",
        )
        first = materialize(
            conn,
            task_id=task_id,
            comment_id=comment_id,
            author="wrench",
            signal_class="captain_note",
        )
        second = materialize(
            conn,
            task_id=task_id,
            comment_id=comment_id,
            author="wrench",
            signal_class="captain_note",
        )
        event_count = conn.execute(
            "SELECT COUNT(*) FROM task_events "
            "WHERE task_id = ? AND kind = 'captain_signal'",
            (task_id,),
        ).fetchone()[0]
        inbox_count = conn.execute(
            "SELECT COUNT(*) FROM kanban_captain_inbox WHERE source_comment_id = ?",
            (comment_id,),
        ).fetchone()[0]
    finally:
        conn.close()

    assert first == second
    assert event_count == 1
    assert inbox_count == 1


def test_root_creation_does_not_infer_captain_origin_from_process_env(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_SESSION_KEY", "wrench-process-session")
    conn = kb.connect(tmp_path / "no-inferred-origin.db")
    try:
        task_id = kb.create_task(
            conn,
            title="captain fallback root",
            assignee="worker",
            captain_profile="otto",
        )
        owner = _captain_owner(conn, task_id)
    finally:
        conn.close()

    assert owner["profile"] == "otto"
    assert owner["origin_session_key"] is None


def test_captain_signal_delivery_reads_bounded_redacted_authoritative_comment(
    tmp_path,
):
    from tui_gateway import server

    conn = kb.connect(tmp_path / "delivery.db")
    try:
        task_id = kb.create_task(conn, title="decision target", assignee="worker")
        secret = "«redacted:sk-…»"
        comment_id = kb.add_comment(
            conn,
            task_id,
            author="wrench",
            body=(
                "DECISION REQUIRED — approve bounded handling\n"
                f"OPENAI_API_KEY={secret}\n"
                + ("x" * 5000)
                + "TAIL-MUST-BE-TRUNCATED"
            ),
        )
        claims: list[dict] = []
        texts: list[str] = []
        leased = server._collect_captain_reports(
            conn,
            "signals",
            {"session_key": "captain-live"},
            "default",
            set(),
            claims,
            texts,
        )
    finally:
        conn.close()

    assert leased == 1
    assert len(claims) == 1
    assert len(texts) == 1
    text = texts[0]
    assert task_id in text
    assert "decision target" in text
    assert f"comment #{comment_id}" in text
    assert "wrench" in text
    assert "decision required" in text.lower()
    assert "approve bounded handling" in text
    assert secret not in text
    assert "TAIL-MUST-BE-TRUNCATED" not in text
    assert len(text.encode("utf-8")) <= 4096


def test_captain_ack_and_reply_commit_atomically_once(tmp_path):
    settle_with_reply = getattr(kb, "ack_captain_reports_with_reply", None)
    assert callable(settle_with_reply)

    conn = kb.connect(tmp_path / "reply.db")
    try:
        task_id = kb.create_task(conn, title="reply target", assignee="worker")
        kb.add_comment(
            conn,
            task_id,
            author="wrench",
            body="CAPTAIN NOTE: tell the Captain what the worker needs next",
        )
        event_id = conn.execute(
            "SELECT event_id FROM kanban_captain_inbox "
            "WHERE task_id = ? AND kind = 'captain_signal'",
            (task_id,),
        ).fetchone()["event_id"]
        token, events = kb.lease_captain_reports(
            conn,
            profile="default",
            owner="captain-live",
            event_ids=[event_id],
        )
        assert len(events) == 1

        settled = settle_with_reply(
            conn,
            token=token,
            owner="captain-live",
            reply_author="default",
            reply_body="CAPTAIN ACK:\nProceed with the bounded fix.",
            reply_task_ids={task_id},
        )
        replayed = settle_with_reply(
            conn,
            token=token,
            owner="captain-live",
            reply_author="default",
            reply_body="CAPTAIN ACK:\nProceed with the bounded fix.",
            reply_task_ids={task_id},
        )
        comments = kb.list_comments(conn, task_id)
        state = conn.execute(
            "SELECT state FROM kanban_captain_inbox WHERE event_id = ?",
            (event_id,),
        ).fetchone()["state"]
    finally:
        conn.close()

    assert settled == 1
    assert replayed == 0
    assert state == "acked"
    assert [comment.body for comment in comments].count(
        "CAPTAIN ACK:\nProceed with the bounded fix."
    ) == 1


def test_captain_reply_settlement_rejects_a_mismatched_signal_task(tmp_path):
    conn = kb.connect(tmp_path / "reply-mismatch.db")
    try:
        task_id = kb.create_task(conn, title="reply mismatch", assignee="worker")
        kb.add_comment(
            conn,
            task_id,
            author="wrench",
            body="CAPTAIN NOTE: preserve the fenced task identity",
        )
        event_id = conn.execute(
            "SELECT event_id FROM kanban_captain_inbox WHERE task_id = ?",
            (task_id,),
        ).fetchone()["event_id"]
        token, events = kb.lease_captain_reports(
            conn,
            profile="default",
            owner="captain-live",
            event_ids=[event_id],
        )
        assert len(events) == 1

        with pytest.raises(RuntimeError, match="signal task identity"):
            kb.ack_captain_reports_with_reply(
                conn,
                token=token,
                owner="captain-live",
                reply_author="default",
                reply_body="ACK — preserve identity.",
                reply_task_ids={"t_wrong"},
            )

        state = conn.execute(
            "SELECT state FROM kanban_captain_inbox WHERE event_id = ?",
            (event_id,),
        ).fetchone()["state"]
        replies = [
            comment.body
            for comment in kb.list_comments(conn, task_id)
            if comment.body == "ACK — preserve identity."
        ]
    finally:
        conn.close()

    assert state == "leased"
    assert replies == []


def test_gateway_settlement_posts_persisted_otto_reply_on_signal_task_once(
    tmp_path, monkeypatch
):
    from tui_gateway import server

    db_path = tmp_path / "gateway-reply.db"
    conn = kb.connect(db_path)
    try:
        task_id = kb.create_task(
            conn,
            title="gateway reply target",
            assignee="worker",
            captain_profile="otto",
        )
        kb.add_comment(
            conn,
            task_id,
            author="wrench",
            body="DECISION REQUIRED — choose the bounded route",
        )
        event_id = conn.execute(
            "SELECT event_id FROM kanban_captain_inbox "
            "WHERE task_id = ? AND kind = 'captain_signal'",
            (task_id,),
        ).fetchone()["event_id"]
        token, events = kb.lease_captain_reports(
            conn,
            profile="otto",
            owner="otto-live",
            event_ids=[event_id],
        )
        assert len(events) == 1
    finally:
        conn.close()

    real_connect = kb.connect
    monkeypatch.setattr(kb, "connect", lambda *args, **kwargs: real_connect(db_path))
    monkeypatch.setattr(
        server,
        "_persisted_captain_report",
        lambda _session, _completion_id: {
            "report": {"role": "assistant", "content": "ACK — use route A."},
            "messages": [],
            "moved": False,
        },
    )
    claims = [
        {
            "route": "captain",
            "board": "signals",
            "token": token,
            "owner": "otto-live",
            "expected_count": 1,
            "task_ids": {task_id},
            "signal_task_ids": {task_id},
            "deliveries": [{"id": f"kanban:signals:{event_id}"}],
        }
    ]

    server._settle_captain_turn_claims(
        {},
        claims,
        completion_id="kanban-report:signal-reply",
        captain_profile="otto",
        succeeded=True,
    )

    conn = real_connect(db_path)
    try:
        state = conn.execute(
            "SELECT state FROM kanban_captain_inbox WHERE event_id = ?",
            (event_id,),
        ).fetchone()["state"]
        comments = kb.list_comments(conn, task_id)
    finally:
        conn.close()

    assert state == "acked"
    assert [(comment.author, comment.body) for comment in comments].count(
        ("otto", "ACK — use route A.")
    ) == 1

