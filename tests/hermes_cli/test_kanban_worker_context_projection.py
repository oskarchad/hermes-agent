from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from tools import kanban_tools


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    db_path = home / "kanban.db"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db(db_path)
    return home


def _insert_run(
    conn,
    task_id: str,
    *,
    profile: str,
    outcome: str,
    summary: str,
    metadata: dict | None = None,
    error: str | None = None,
    started_at: int,
) -> int:
    cur = conn.execute(
        "INSERT INTO task_runs "
        "(task_id, profile, status, started_at, ended_at, outcome, summary, metadata, error) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id,
            profile,
            "done" if outcome == "completed" else outcome,
            started_at,
            started_at + 1,
            outcome,
            summary,
            json.dumps(metadata) if metadata is not None else None,
            error,
        ),
    )
    return int(cur.lastrowid)


def _complete_fixture_task(conn, task_id: str, *, completed_at: int) -> None:
    conn.execute(
        "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?",
        (completed_at, task_id),
    )


def _parent_with_run(
    conn,
    *,
    title: str,
    profile: str,
    summary: str,
    metadata: dict,
    started_at: int,
) -> tuple[str, int]:
    task_id = kb.create_task(conn, title=title, assignee=profile)
    run_id = _insert_run(
        conn,
        task_id,
        profile=profile,
        outcome="completed",
        summary=summary,
        metadata=metadata,
        started_at=started_at,
    )
    _complete_fixture_task(conn, task_id, completed_at=started_at + 1)
    return task_id, run_id


def _line_count(text: str) -> int:
    return len(text.splitlines())


def test_spawn_context_selects_one_accepted_parent_but_show_keeps_full_history(
    kanban_home: Path,
) -> None:
    now = int(time.time())
    with kb.connect() as conn:
        rejected_impl, _ = _parent_with_run(
            conn,
            title="implementation v1",
            profile="patch",
            summary="REJECTED IMPLEMENTATION BODY",
            metadata={"commit": "sha-v1", "changed_files": ["old.py"]},
            started_at=now - 500,
        )
        rejected_review, _ = _parent_with_run(
            conn,
            title="review v1",
            profile="gauge",
            summary="Changes required for v1.",
            metadata={
                "commit": "sha-v1",
                "verdict": "changes_required",
                "findings": ["old flaw"],
            },
            started_at=now - 400,
        )
        accepted_impl, accepted_impl_run = _parent_with_run(
            conn,
            title="implementation v2",
            profile="patch",
            summary="ACCEPTED IMPLEMENTATION BODY",
            metadata={
                "commit": "sha-v2",
                "changed_files": ["new.py"],
                "tests": "focused pass",
            },
            started_at=now - 300,
        )
        accepted_review, accepted_review_run = _parent_with_run(
            conn,
            title="review v2",
            profile="gauge",
            summary="Approved exact version sha-v2.",
            metadata={
                "commit": "sha-v2",
                "verdict": "approved",
                "tests_summary": "review pass",
            },
            started_at=now - 200,
        )
        target = kb.create_task(
            conn,
            title="OAuth integration",
            body="Implement only the accepted OAuth integration target.",
            assignee="wrench",
        )
        for parent in (
            rejected_impl,
            rejected_review,
            accepted_impl,
            accepted_review,
        ):
            kb.link_tasks(conn, parent, target)
        kb.recompute_ready(conn)

        for attempt in range(12):
            _insert_run(
                conn,
                target,
                profile="wrench",
                outcome="crashed",
                summary=f"STALE ATTEMPT {attempt}",
                error=f"old error {attempt}",
                started_at=now - 100 + attempt,
            )
        for comment in range(45):
            kb.add_comment(
                conn,
                target,
                "operator",
                f"COMMENT STORM {comment}",
            )
        kb.add_comment(conn, target, "operator", "LATEST MATERIAL COMMENT")
        with kb.write_txn(conn):
            for event in range(70):
                kb._append_event(conn, target, "audit_fixture", {"sequence": event})
        assert kb.claim_task(conn, target, claimer="wrench:test") is not None

        context = kb.build_worker_context(conn, target)

        assert context.count("## Accepted parent output") == 1
        assert f"Producer: {accepted_impl} run {accepted_impl_run}" in context
        assert f"Accepted by: {accepted_review} run {accepted_review_run}" in context
        assert "Verdict: approved" in context
        assert "Version: commit=sha-v2" in context
        assert "Supersedes: commit=sha-v1" in context
        assert context.count("ACCEPTED IMPLEMENTATION BODY") == 1
        assert "REJECTED IMPLEMENTATION BODY" not in context
        assert "LATEST MATERIAL COMMENT" in context
        assert "COMMENT STORM 0" not in context
        assert "## Recent work by" not in context
        assert len(context) < 21_532
        assert _line_count(context) < 116

    shown = json.loads(kanban_tools._handle_show({"task_id": target}))
    assert len(shown["runs"]) == 13
    assert len(shown["comments"]) == 46
    assert any(run["summary"] == "STALE ATTEMPT 0" for run in shown["runs"])
    assert any(comment["body"] == "COMMENT STORM 0" for comment in shown["comments"])
    audit_events = [
        event for event in shown["events"] if event["kind"] == "audit_fixture"
    ]
    assert len(audit_events) == 70
    assert audit_events[0]["payload"] == {"sequence": 0}


def test_repair_context_projects_only_latest_changes_request(kanban_home: Path) -> None:
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="Repair phase routing",
            body="Repair the reviewed implementation.",
            assignee="builder",
        )
        implementation = kb.claim_task(conn, task_id, claimer="builder:1")
        assert implementation is not None
        assert kb.request_review(
            conn,
            task_id,
            reviewer="reviewer",
            summary="Candidate version v7 is ready.",
            metadata={"commit": "v7"},
            expected_run_id=implementation.current_run_id,
        )
        review = kb.claim_review_task(conn, task_id, claimer="reviewer:1")
        assert review is not None
        assert kb.request_changes(
            conn,
            task_id,
            reason="Add the missing fallback regression.",
            expected_run_id=review.current_run_id,
        ) == (True, "builder")
        repair = kb.claim_task(conn, task_id, claimer="builder:2")
        assert repair is not None

        unrelated = kb.create_task(conn, title="Unrelated work", assignee="builder")
        _insert_run(
            conn,
            unrelated,
            profile="builder",
            outcome="completed",
            summary="UNRELATED PROFILE HISTORY",
            started_at=int(time.time()),
        )

        context = kb.build_worker_context(conn, task_id)

    assert "Phase: repair" in context
    assert "## Latest material delta" in context
    assert "Add the missing fallback regression." in context
    assert "Candidate version v7 is ready." in context
    assert "UNRELATED PROFILE HISTORY" not in context
    assert _line_count(context) < 105


def test_closure_review_context_projects_exact_review_target(kanban_home: Path) -> None:
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="Closure review",
            body="Verify the submitted implementation.",
            assignee="builder",
        )
        implementation = kb.claim_task(conn, task_id, claimer="builder:1")
        assert implementation is not None
        assert kb.request_review(
            conn,
            task_id,
            reviewer="gauge",
            summary="Review exact candidate release-9.",
            metadata={"commit": "release-9", "tests": "52 passed"},
            expected_run_id=implementation.current_run_id,
        )
        review = kb.claim_review_task(conn, task_id, claimer="gauge:1")
        assert review is not None
        review_requested_run = [
            run
            for run in kb.list_runs(conn, task_id)
            if run.outcome == "review_requested"
        ][-1]

        context = kb.build_worker_context(conn, task_id)

    assert "Phase: closure review" in context
    assert "## Review target" in context
    assert f"Run: {review_requested_run.id}" in context
    assert "Review exact candidate release-9." in context
    assert "commit=release-9" in context
    assert "## Prior attempts on this task" not in context
    assert _line_count(context) < 53


def test_legacy_done_parents_without_pass_pointer_do_not_gain_authority(
    kanban_home: Path,
) -> None:
    now = int(time.time())
    with kb.connect() as conn:
        legacy, _ = _parent_with_run(
            conn,
            title="legacy done task",
            profile="legacy",
            summary="DONE BUT NOT ACCEPTED",
            metadata={"commit": "legacy-sha"},
            started_at=now - 100,
        )
        target = kb.create_task(
            conn,
            title="Clean integration",
            body="Integrate only an explicitly accepted parent output.",
            assignee="wrench",
        )
        kb.link_tasks(conn, legacy, target)
        kb.recompute_ready(conn)
        assert kb.claim_task(conn, target, claimer="wrench:test") is not None

        context = kb.build_worker_context(conn, target)

    assert "## Accepted parent output" not in context
    assert "No accepted parent output is recorded" in context
    assert "DONE BUT NOT ACCEPTED" not in context
    assert "legacy-sha" not in context
    assert _line_count(context) < 35


def test_spawn_projection_redacts_legacy_fields_and_shows_direct_blocker(
    kanban_home: Path,
) -> None:
    secret = "ghp_" + "A" * 40
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="Pending prerequisite", assignee="patch")
        target = kb.create_task(
            conn,
            title="Blocked target",
            body=f"legacy body token={secret}",
            assignee="wrench",
            parents=[parent],
        )
        _insert_run(
            conn,
            target,
            profile="wrench",
            outcome="crashed",
            summary=f"legacy run token={secret}",
            metadata={"debug_token": secret},
            started_at=int(time.time()) - 10,
        )
        kb.add_comment(conn, target, "operator", f"legacy comment token={secret}")

        context = kb.build_worker_context(conn, target)

    assert secret not in context
    assert "Pending prerequisite" in context
    assert f"Waiting on: {parent} [ready]" in context
