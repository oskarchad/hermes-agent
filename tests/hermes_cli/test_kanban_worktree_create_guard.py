"""A worktree task with no anchor is rejected at create, not at dispatch.

Without a ``workspace_path``, a project repo or the board's ``default_workdir``
the dispatcher can never materialize the worktree: it used to fail
``spawn_failed`` twice and give up. ``create_task`` now refuses it up front.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import projects_db as pdb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_unanchored_worktree_is_rejected_and_not_written(kanban_home):
    with kbc.connect() as conn:
        with pytest.raises(ValueError, match="default_workdir"):
            kb.create_task(conn, title="orphan", assignee="coder", workspace_kind="worktree")
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_anchored_worktrees_are_created(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    with pdb.connect_closing() as pc:
        proj = pdb.get_project(pc, pdb.create_project(pc, name="App", folders=[str(repo)]))
    with kbc.connect() as conn:
        explicit = kb.create_task(conn, title="a", workspace_kind="worktree",
                                  workspace_path=str(repo / ".worktrees" / "a"))
        projected = kb.create_task(conn, title="b", workspace_kind="worktree", project_id=proj.slug)
        kb.write_board_metadata(kb.get_current_board(), default_workdir=str(repo))
        inherited = kb.create_task(conn, title="c", workspace_kind="worktree")
        paths = {t: kb.get_task(conn, t).workspace_path for t in (explicit, projected, inherited)}

    assert paths[explicit] == str(repo / ".worktrees" / "a")
    assert paths[projected] == str(repo / ".worktrees" / projected)
    assert Path(paths[inherited]).resolve() == repo.resolve()
