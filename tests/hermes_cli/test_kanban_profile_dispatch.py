"""Profile opt-out is a Kanban boundary, not deletion or history isolation."""
from pathlib import Path
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_decompose as decompose
from hermes_cli import profiles


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    for name in ("retired", "wrench", "reviewer"):
        profiles.get_profile_dir(name).mkdir(parents=True)
    with kbc.connect(home / "kanban.db") as conn:
        yield home, conn


@pytest.mark.parametrize("metadata", [
    "dispatch_enabled: false\n", 'dispatch_enabled: "false"\n',
    "dispatch_enabled: 0\n", "dispatch_enabled: null\n",
    "dispatch_enabled: []\n", "dispatch_enabled: [\n", "false\n",
])
def test_opt_out_preserves_history_but_refuses_routing_and_stale_dispatch(fleet, metadata):
    home, conn = fleet
    retired = profiles.get_profile_dir("retired")
    history = retired / "state.db"
    with sqlite3.connect(history) as db:
        db.execute("CREATE TABLE history (message TEXT)")
        db.execute("INSERT INTO history VALUES ('retained conversation')")
    history_bytes = history.read_bytes()
    assert "retired" in decompose._build_roster()[1]  # absent flag is compatible
    ready = kb.create_task(conn, title="old ready", assignee="retired", idempotency_key="old")
    review = kb.create_task(conn, title="old review", assignee="retired")
    assert kb.request_review(conn, review)
    before = {tid: kb.list_events(conn, tid) for tid in (ready, review)}
    flag = retired / "profile.yaml"
    flag.write_text(metadata)

    roster, names = decompose._build_roster()
    assert "retired" not in names
    assert "retired" not in {p["name"] for p in roster}
    assert "wrench" in names
    assert profiles.profile_exists("retired")
    assert profiles.resolve_profile_env("retired") == str(retired)
    assert {p.name: p.path for p in profiles.list_profiles()}["retired"] == retired
    assert history.read_bytes() == history_bytes
    with sqlite3.connect(profiles.get_profile_dir("retired") / "state.db") as db:
        assert db.execute("SELECT message FROM history").fetchone()[0] == "retained conversation"
    # An idempotent read of existing work remains legal; no new admission occurs.
    assert kb.create_task(conn, title="old ready", assignee="retired", idempotency_key="old") == ready
    assert not dispatch.has_spawnable_ready(conn)
    assert not dispatch.has_spawnable_review(conn)
    calls = []
    dry = dispatch.dispatch_once(conn, dry_run=True, reconcile_orphans=False)
    assert not dry.spawned
    assert kb.get_task(conn, ready).status == "ready"
    assert kb.get_task(conn, review).status == "review"
    outcome = dispatch.dispatch_once(conn, spawn_fn=lambda *a: calls.append(a), reconcile_orphans=False)
    assert not calls and not outcome.spawned
    for tid in (ready, review):
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.assignee == "retired"
        assert task.consecutive_failures == 0
        events = kb.list_events(conn, tid)
        assert events[:len(before[tid])] == before[tid]
        assert "dispatch_enabled" in events[-1].payload["reason"]
        assert "Captain" in events[-1].payload["reason"]
    settled = {tid: kb.list_events(conn, tid) for tid in (ready, review)}
    dispatch.dispatch_once(conn, spawn_fn=lambda *a: calls.append(a), reconcile_orphans=False)
    assert not calls
    assert settled == {tid: kb.list_events(conn, tid) for tid in settled}

    flag.write_text("dispatch_enabled: true\n")
    assert "retired" in decompose._build_roster()[1]
    assert kb.unblock_task(conn, ready)
    assert kb.unblock_task(conn, review)
    assert kb.get_task(conn, review).status == "review"
    outcome = dispatch.dispatch_once(conn, spawn_fn=lambda *a: calls.append(a), reconcile_orphans=False)
    assert {row[0] for row in outcome.spawned} == {ready, review}
    assert history.read_bytes() == history_bytes


@pytest.mark.parametrize("metadata", ["dispatch_enabled: false\n", 'dispatch_enabled: "true"\n'])
def test_admission_and_review_return_refuse_disabled_without_rewriting_provenance(fleet, metadata, monkeypatch):
    _, conn = fleet
    task = kb.create_task(conn, title="review return", assignee="retired")
    implementation = kb.claim_task(conn, task)
    assert kb.request_review(conn, task, reviewer="reviewer", expected_run_id=implementation.current_run_id)
    review = kb.claim_review_task(conn, task)
    events = kb.list_events(conn, task)
    triage = kb.create_task(conn, title="triage", triage=True)
    ready = kb.create_task(conn, title="unassigned")
    profiles.get_profile_dir("retired").joinpath("profile.yaml").write_text(metadata)

    with pytest.raises(ValueError, match="dispatch_enabled"):
        kb.create_task(conn, title="new work", assignee="retired")
    with pytest.raises(ValueError, match="dispatch_enabled"):
        kb.assign_task(conn, ready, "retired")
    with pytest.raises(ValueError, match="dispatch_enabled"):
        kb.specify_triage_task(conn, triage, assignee="retired")
    with pytest.raises(ValueError, match="dispatch_enabled"):
        kb.decompose_triage_task(conn, triage, root_assignee="wrench", children=[
            {"title": "valid first", "assignee": "wrench"},
            {"title": "disabled second", "assignee": "retired"},
        ])
    assert kb.get_task(conn, triage).status == "triage"
    assert not any(t.title == "valid first" for t in kb.list_tasks(conn))
    ok, reason = kb.request_review(conn, ready, reviewer="retired", with_reason=True)
    assert not ok and "dispatch_enabled" in reason
    ok, reason = kb.request_changes(conn, task, reason="fix regression", expected_run_id=review.current_run_id)
    assert not ok and "dispatch_enabled" in reason and "Captain" in reason
    assert kb.get_task(conn, task).assignee == "reviewer"
    assert kb.get_task(conn, task).current_run_id == review.current_run_id
    assert kb.list_events(conn, task) == events
    # Explicit refusal lets the reviewer use the existing needs-input handoff.
    assert kb.block_task(conn, task, reason=reason, kind="needs_input", expected_run_id=review.current_run_id)
    assert kb.get_task(conn, task).status == "blocked"
    assert kb.assign_task(conn, ready, "wrench")
    assert kb.get_task(conn, ready).assignee == "wrench"
    # Disabled configured defaults and stale LLM choices must not silently reroute.
    with pytest.raises(ValueError, match="dispatch_enabled"):
        decompose._resolve_profile_from_cfg({"kanban": {"default_assignee": "retired"}}, "default_assignee")
    with pytest.raises(ValueError, match="dispatch_enabled"):
        decompose._normalize_assignee_choice("retired", default_assignee="wrench", valid_names={"wrench"})
    monkeypatch.setattr(decompose, "_load_config", lambda: {"kanban": {"default_assignee": "retired"}})
    result = decompose.decompose_task(triage)
    assert not result.ok and "dispatch_enabled" in result.reason
    monkeypatch.setattr(decompose, "_load_config", lambda: {"kanban": {"default_assignee": "wrench"}})
    monkeypatch.setattr(decompose, "_call_aux", lambda *a, **kw: (
        '{"fanout": false, "title": "new specification", "assignee": "retired"}', "",
    ))
    result = decompose.decompose_task(triage)
    assert not result.ok and "dispatch_enabled" in result.reason
