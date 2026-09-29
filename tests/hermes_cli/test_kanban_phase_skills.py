"""Phase-owned forced skills survive cross-profile review without loader bypasses."""
from pathlib import Path

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch


COMMON = ["repo-context-gate", "architectural-direction-gate", "verification-before-completion"]
AUTHOR = COMMON + ["test-driven-development", "open-code-review-worker-helper"]
REVIEW = COMMON + ["hermes-agent", "sdlc-review"]


@pytest.fixture
def homes(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    for name, skills in (("wrench", AUTHOR), ("gauge", REVIEW)):
        profile = home / "profiles" / name
        profile.mkdir(parents=True)
        (profile / "config.yaml").write_text("{}\n")
        for skill in skills:
            folder = profile / "skills" / skill
            folder.mkdir(parents=True)
            (folder / "SKILL.md").write_text(
                f"---\nname: {skill}\ndescription: Isolated test skill\n---\nRead {skill}.\n"
            )
    kb.init_db()
    return home


def preload(home, task):
    from agent.skill_commands import build_preloaded_skills_prompt
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    argv = dispatch._worker_argv(task, task.assignee, str(home / "profiles" / task.assignee))
    skills = [argv[i + 1] for i, value in enumerate(argv) if value == "--skills"]
    token = set_hermes_home_override(str(home / "profiles" / task.assignee))
    try:
        _, loaded, missing = build_preloaded_skills_prompt(skills)
        return loaded, missing
    finally:
        reset_hermes_home_override(token)


def test_phase_skills_reach_real_preload_and_round_trip(homes, monkeypatch):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="Phase skills", assignee="wrench", skills=AUTHOR)
        author = kb.claim_task(conn, tid)
        assert preload(homes, author) == (AUTHOR, [])
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(author.current_run_id))
        flags = " ".join(f"--review-skill {skill}" for skill in REVIEW)
        result = cli.run_slash(f"request-review {tid} --reviewer gauge --summary ready {flags}")
        assert "Requested review" in result
        assert kb.get_task(conn, tid).skills == AUTHOR

        observed = []
        def spawn(task, workspace, board=None):
            observed.append(task)
            assert task.skills == REVIEW
            assert preload(homes, task) == (REVIEW, [])

        # The actual dispatcher claim and argv consumer; no model or process launch.
        outcome = dispatch.dispatch_once(conn, spawn_fn=spawn, max_spawn=1)
        assert [row[0] for row in outcome.spawned] == [tid]
        review = observed[-1]
        assert kb.request_changes(conn, tid, reason="fix", expected_run_id=review.current_run_id) == (True, "wrench")
        author = kb.claim_task(conn, tid)
        assert author.skills == AUTHOR
        assert preload(homes, author) == (AUTHOR, [])
        assert kb.request_review(conn, tid, expected_run_id=author.current_run_id)
        review = kb.claim_review_task(conn, tid)
        assert review.skills == REVIEW
        assert preload(homes, review) == (REVIEW, [])
        assert kb.get_task(conn, tid).skills == AUTHOR

        # All explicit review requirements remain strict, even if unavailable.
        assert kb.request_changes(conn, tid, reason="again", expected_run_id=review.current_run_id)[0]
        author = kb.claim_task(conn, tid)
        import json
        from tools import kanban_tools

        monkeypatch.setenv("HERMES_PROFILE", "wrench")
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(author.current_run_id))
        assert json.loads(kanban_tools._handle_request_review({
            "task_id": tid, "summary": "ready", "reviewer": "gauge",
            "review_skills": REVIEW + ["missing-review-skill"],
        }))["ok"]
        review = kb.claim_review_task(conn, tid)
        assert preload(homes, review)[1] == ["missing-review-skill"]


def test_ambiguous_legacy_review_cannot_claim_or_reserve_capacity(homes):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="Legacy", assignee="wrench", skills=AUTHOR)
        # Persist an actual pre-fix handoff shape in an isolated fixture DB.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'review', assignee = 'gauge' WHERE id = ?", (tid,))
            kb._append_event(conn, tid, "review_requested", {"implementer": "wrench", "reviewer": "gauge"})
        healthy = kb.create_task(conn, title="Healthy", assignee="wrench")
        assert kb.claim_review_task(conn, tid) is None
        assert not dispatch._any_spawnable_review(conn, dispatch._lane_rows(conn, "review"))
        dry = dispatch.dispatch_once(conn, dry_run=True, max_spawn=2)
        assert [row[0] for row in dry.spawned] == [healthy]
        result = dispatch.dispatch_once(conn, spawn_fn=lambda task, workspace: None, max_spawn=1)
        assert [row[0] for row in result.spawned] == [healthy]
        assert kb.get_task(conn, tid).status == "review"
        assert kb.get_task(conn, tid).current_run_id is None
        # Recovery uses existing reopen and request-review, never raw operator SQL.
        assert kb.reopen_review_task(conn, tid)
        assert kb.get_task(conn, tid).assignee == "wrench"
        assert kb.request_review(conn, tid, reviewer="gauge", review_skills=REVIEW)
        assert kb.claim_review_task(conn, tid).skills == REVIEW

        assigned = kb.create_task(conn, title="Two-step assignment", assignee="wrench", skills=AUTHOR)
        assert kb.request_review(conn, assigned)
        assert kb.assign_task(conn, assigned, "gauge")
        assert kb.claim_review_task(conn, assigned) is None

        for bad in ("skill", [42], [""], ["a,b"]):
            fresh = kb.create_task(conn, title="Invalid skills", assignee="wrench", skills=AUTHOR)
            assert not kb.request_review(conn, fresh, reviewer="gauge", review_skills=bad)
            assert kb.get_task(conn, fresh).status == "ready"
        assert kb.claim_review_task(conn, "t_missing") is None

        implicit = kb.create_task(conn, title="Changed author", assignee="wrench", skills=AUTHOR)
        author = kb.claim_task(conn, implicit)
        assert kb.request_review(conn, implicit, reviewer="wrench", expected_run_id=author.current_run_id)
        assert kb.reopen_review_task(conn, implicit)
        assert kb.assign_task(conn, implicit, "gauge")
        author = kb.claim_task(conn, implicit)
        assert not kb.request_review(conn, implicit, reviewer="wrench", expected_run_id=author.current_run_id)
