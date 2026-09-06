from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from tools import kanban_tools


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        kb,
        "_available_task_toolset_names",
        lambda *_args, **_kwargs: {
            "context7",
            "file",
            "kanban",
            "terminal",
            "web",
        },
        raising=False,
    )
    return home


def _parse_kanban(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    kanban_cli.build_parser(subparsers)
    return parser.parse_args(argv)


def test_cli_create_accepts_repeatable_task_toolsets_and_serializes_readback(
    kanban_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = _parse_kanban(
        [
            "kanban",
            "create",
            "bounded CLI task",
            "--assignee",
            "patch",
            "--toolset",
            "web",
            "--toolset",
            "terminal",
            "--json",
        ]
    )

    assert kanban_cli._cmd_create(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["enabled_toolsets"] == ["web", "terminal"]
    assert payload["effective_toolsets"] == [
        "web",
        "terminal",
        "context7",
        "kanban",
    ]


def test_cli_set_toolsets_updates_and_clears_override(
    kanban_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="edit", assignee="patch")

    set_args = _parse_kanban(
        [
            "kanban",
            "set-toolsets",
            task_id,
            "web",
            "file",
            "--json",
        ]
    )
    assert kanban_cli._cmd_set_toolsets(set_args) == 0
    updated = json.loads(capsys.readouterr().out)
    assert updated["enabled_toolsets"] == ["web", "file"]
    assert updated["effective_toolsets"] == ["web", "file", "context7", "kanban"]

    clear_args = _parse_kanban(
        ["kanban", "set-toolsets", task_id, "--clear", "--json"]
    )
    assert kanban_cli._cmd_set_toolsets(clear_args) == 0
    cleared = json.loads(capsys.readouterr().out)
    assert cleared["enabled_toolsets"] is None
    assert cleared["effective_toolsets"] is None


def test_kanban_create_tool_roundtrips_enabled_toolsets(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kanban_tools, "_maybe_auto_subscribe", lambda *_a, **_k: False)
    result = json.loads(
        kanban_tools._handle_create(
            {
                "title": "agent child",
                "assignee": "patch",
                "enabled_toolsets": ["terminal", "web", "terminal"],
            }
        )
    )

    assert result["ok"] is True
    with kb.connect() as conn:
        task = kb.get_task(conn, result["task_id"])
    assert task is not None
    assert task.enabled_toolsets == ["terminal", "web"]
    assert task.effective_toolsets == ["terminal", "web", "context7", "kanban"]
    assert "enabled_toolsets" in kanban_tools.KANBAN_CREATE_SCHEMA["parameters"]["properties"]