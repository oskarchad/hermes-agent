"""Curator inactivity archival must spare skills an open Kanban card of this profile requires.

A card's ``skills`` are force-loaded when the dispatcher spawns its worker, so archiving one under
a todo/ready/blocked card strands that card exactly like archiving a cron job's skill would.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "skills").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-root"))
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_PROFILE_NAME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "vera")

    import tools.skill_usage as usage
    importlib.reload(usage)
    from agent import curator
    importlib.reload(curator)
    monkeypatch.setattr(curator, "_load_config", dict)
    monkeypatch.setattr(usage, "_prune_builtins_enabled", lambda: False)
    monkeypatch.setattr(curator, "_cron_referenced_skills", set)
    return home, curator, usage


def _stale_skill(home: Path, usage, name: str) -> None:
    d = home / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: x\n---\n", encoding="utf-8")
    ts = (datetime.now(UTC) - timedelta(days=200)).isoformat()
    data = usage.load_usage()
    data[name] = usage._empty_record()
    data[name].update(created_by="agent", created_at=ts, last_used_at=ts, last_activity_at=ts, use_count=1)
    usage.save_usage(data)


def _card(assignee: str, skills: list[str], status: str | None = None) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    with connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee=assignee, skills=skills)
        if status:
            with conn:
                conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))


def test_open_card_of_this_profile_protects_both_name_forms(env):
    home, curator, usage = env
    for name in ("dashboard-data-lookup", "xlsx", "plain", "orphan"):
        _stale_skill(home, usage, name)
    _card("vera", ["productivity:dashboard-data-lookup", "data/xlsx", "plain"], status="todo")

    curator.apply_automatic_transitions()

    states = {n: r["state"] for n, r in usage.load_usage().items()}
    assert states["dashboard-data-lookup"] == usage.STATE_ACTIVE
    assert states["xlsx"] == usage.STATE_ACTIVE
    assert states["plain"] == usage.STATE_ACTIVE
    assert states["orphan"] == usage.STATE_ARCHIVED


@pytest.mark.parametrize(("assignee", "status"), [("vera", "done"), ("vera", "archived"), ("wrench", "todo")])
def test_done_archived_or_foreign_card_does_not_protect(env, assignee, status):
    home, curator, usage = env
    _stale_skill(home, usage, "dashboard-data-lookup")
    _card(assignee, ["dashboard-data-lookup"], status=status)

    curator.apply_automatic_transitions()

    assert usage.load_usage()["dashboard-data-lookup"]["state"] == usage.STATE_ARCHIVED


def test_unreadable_board_never_crashes_curator(env, monkeypatch):
    home, curator, usage = env
    _stale_skill(home, usage, "orphan")
    _card("vera", ["orphan"], status="todo")
    import hermes_cli.kanban_db_connect as kbc

    def boom(*a, **k):
        raise RuntimeError("db locked")
    monkeypatch.setattr(kbc, "connect", boom)

    curator.apply_automatic_transitions()

    assert usage.load_usage()["orphan"]["state"] == usage.STATE_ARCHIVED
