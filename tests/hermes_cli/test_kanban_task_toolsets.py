from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


def _allow_task_toolsets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        kb,
        "_available_task_toolset_names",
        lambda *_args, **_kwargs: {
            "browser",
            "context7",
            "file",
            "kanban",
            "skills",
            "terminal",
            "web",
        },
        raising=False,
    )


def test_enabled_toolsets_additive_migration_preserves_legacy_rows() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(kb.SCHEMA_SQL)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    if "enabled_toolsets" in columns:
        conn.execute("ALTER TABLE tasks DROP COLUMN enabled_toolsets")
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) VALUES (?, ?, ?, ?)",
        ("t_legacy", "legacy", "ready", 1),
    )
    conn.commit()

    kb._migrate_add_optional_columns(conn)

    migrated_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(tasks)")
    }
    assert "enabled_toolsets" in migrated_columns
    assert conn.execute(
        "SELECT enabled_toolsets FROM tasks WHERE id = ?", ("t_legacy",)
    ).fetchone()["enabled_toolsets"] is None


def test_create_task_normalizes_and_roundtrips_requested_and_effective_toolsets(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_task_toolsets(monkeypatch)
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="bounded worker",
            assignee="patch",
            enabled_toolsets=[" web ", "terminal", "web"],
        )
        task = kb.get_task(conn, task_id)

    assert task is not None
    assert task.enabled_toolsets == ["web", "terminal"]
    assert task.effective_toolsets == ["web", "terminal", "context7", "kanban"]


def test_create_task_null_toolsets_preserves_legacy_profile_inheritance(
    kanban_home: Path,
) -> None:
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="legacy", assignee="patch")
        task = kb.get_task(conn, task_id)

    assert task is not None
    assert task.enabled_toolsets is None
    assert task.effective_toolsets is None


def test_idempotent_duplicate_returns_before_toolset_availability_drift(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_task_toolsets(monkeypatch)
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="first delivery",
            assignee="patch",
            enabled_toolsets=["web"],
            idempotency_key="delivery-42",
        )
        monkeypatch.setattr(
            kb,
            "_available_task_toolset_names",
            lambda *_args, **_kwargs: {"terminal", "kanban"},
        )

        duplicate_id = kb.create_task(
            conn,
            title="retried delivery",
            assignee="patch",
            enabled_toolsets=["web"],
            idempotency_key="delivery-42",
        )

    assert duplicate_id == task_id


def test_profile_enabled_mcp_server_name_is_a_valid_task_toolset_alias(
    tmp_path: Path,
) -> None:
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    (profile_home / "config.yaml").write_text(
        "mcp_servers:\n"
        "  context7:\n"
        "    enabled: true\n"
        "    command: node\n"
        "    args: []\n",
        encoding="utf-8",
    )

    available = kb._available_task_toolset_names(str(profile_home))
    assert "context7" in available
    assert kb.normalize_enabled_toolsets(
        ["terminal", "context7"], hermes_home=str(profile_home)
    ) == ["terminal", "context7"]


def test_live_mcp_alias_from_other_profile_is_not_available_to_target_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tools.registry import registry

    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    (profile_home / "config.yaml").write_text("{}\n", encoding="utf-8")

    context7_probe = "mcp__context7__foreign_profile_probe"
    registry.register(
        name=context7_probe,
        toolset="mcp-context7",
        schema={
            "name": context7_probe,
            "description": "Foreign profile MCP probe",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=lambda _args, **_kwargs: "{}",
    )
    monkeypatch.setitem(registry._toolset_aliases, "context7", "mcp-context7")
    try:
        available = kb._available_task_toolset_names(str(profile_home))
    finally:
        registry.deregister(context7_probe)

    assert "context7" not in available


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("web,terminal", "must be a list"),
        (["web", 3], "must contain only strings"),
        (["unknown-toolset"], "unknown toolset"),
        (["web"] * 33, "at most"),
    ],
)
def test_create_task_rejects_malformed_or_unbounded_toolsets(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    value,
    message: str,
) -> None:
    _allow_task_toolsets(monkeypatch)
    with kb.connect() as conn:
        with pytest.raises(ValueError, match=message):
            kb.create_task(
                conn,
                title="invalid",
                assignee="patch",
                enabled_toolsets=value,
            )


def _spawn_task(kb_module, *, status: str = "running"):
    task = kb_module.Task(
        id="t_spawn_bounded",
        title="spawn bounded",
        body=None,
        assignee="patch",
        status=status,
        priority=0,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=None,
        claim_lock="lock",
        claim_expires=None,
        tenant=None,
        current_run_id=7,
    )
    task.enabled_toolsets = ["web", "terminal", "web"]
    task.effective_toolsets = ["web", "terminal", "context7", "kanban"]
    return task


@pytest.mark.parametrize("status", ["running", "review"])
def test_default_spawn_uses_exact_task_toolsets_in_worker_and_reviewer_phases(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    status: str,
) -> None:
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "patch"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    _allow_task_toolsets(monkeypatch)
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(
        kb,
        "_resolve_worker_cli_toolsets",
        lambda _home: [
            "web",
            "terminal",
            "context7",
            "meta_ads",
            "email",
            "claude_design",
            "video",
            "kanban",
        ],
    )

    captured: dict[str, object] = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    kb._default_spawn(_spawn_task(kb, status=status), str(workspace))

    command = captured["cmd"]
    assert isinstance(command, list)
    pinned = command[command.index("--toolsets") + 1].split(",")
    assert pinned == ["web", "terminal", "context7", "kanban"]
    assert not ({"meta_ads", "email", "claude_design", "video"} & set(pinned))


def test_review_transition_preserves_requested_and_effective_toolsets(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_task_toolsets(monkeypatch)
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="review bounded surface",
            assignee="patch",
            enabled_toolsets=["terminal", "file"],
        )
        assert kb.request_review(conn, task_id, summary="implementation complete")
        task = kb.get_task(conn, task_id)

    assert task is not None and task.status == "review"
    assert task.enabled_toolsets == ["terminal", "file"]
    assert task.effective_toolsets == ["terminal", "file", "context7", "kanban"]


def test_dispatch_blocks_tampered_unknown_toolset_before_spawn(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_task_toolsets(monkeypatch)
    spawned: list[str] = []

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="tampered",
            assignee="patch",
            enabled_toolsets=["web"],
        )
        conn.execute(
            "UPDATE tasks SET enabled_toolsets = ? WHERE id = ?",
            (json.dumps(["web", "secret-looking-bad-value"]), task_id),
        )
        conn.commit()
        monkeypatch.setattr(
            "hermes_cli.profiles.profile_exists", lambda _name: True
        )

        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace: spawned.append(task.id),
        )
        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)

    assert not result.spawned
    assert spawned == []
    assert task is not None and task.status == "blocked"
    audit = [event for event in events if event.kind == "toolsets_validation_failed"]
    assert len(audit) == 1
    assert audit[0].payload == {
        "field": "enabled_toolsets",
        "reason_code": "unknown_toolset",
    }
    serialized = json.dumps(audit[0].payload)
    assert "secret-looking-bad-value" not in serialized


def test_dispatch_validates_default_assignee_toolsets_before_claim_or_spawn(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (kanban_home / "config.yaml").write_text(
        "mcp_servers:\n"
        "  context7:\n"
        "    enabled: true\n"
        "    command: node\n"
        "    args: []\n",
        encoding="utf-8",
    )
    default_profile = kanban_home / "profiles" / "patch"
    default_profile.mkdir(parents=True)
    (default_profile / "config.yaml").write_text("{}\n", encoding="utf-8")
    spawned: list[str] = []

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="default profile validation",
            assignee=None,
            enabled_toolsets=["terminal"],
        )
        monkeypatch.setattr(
            "hermes_cli.profiles.profile_exists", lambda _name: True
        )

        result = kb.dispatch_once(
            conn,
            default_assignee="patch",
            spawn_fn=lambda task, _workspace: spawned.append(task.id),
        )
        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)

    assert spawned == []
    assert not result.spawned
    assert result.auto_blocked == [task_id]
    assert task is not None and task.status == "blocked"
    validation_events = [
        event for event in events if event.kind == "toolsets_validation_failed"
    ]
    assert len(validation_events) == 1
    assert validation_events[0].payload == {
        "field": "enabled_toolsets",
        "reason_code": "required_toolset_unavailable",
    }
    assert not ({"spawn_failed", "gave_up"} & {event.kind for event in events})


def test_bounded_worker_toolsets_reduce_real_model_tool_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from model_tools import get_tool_definitions

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_schema")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "1")
    broad = get_tool_definitions(
        [
            "terminal", "file", "web", "delegation", "session_search",
            "todo", "kanban",
        ],
        quiet_mode=True,
        skip_tool_search_assembly=True,
    )
    bounded = get_tool_definitions(
        ["terminal", "file", "kanban"],
        quiet_mode=True,
        skip_tool_search_assembly=True,
    )

    broad_names = {item["function"]["name"] for item in broad}
    bounded_names = {item["function"]["name"] for item in bounded}
    unrelated = {"web_search", "delegate_task", "session_search", "todo"}
    assert unrelated <= broad_names
    assert not (unrelated & bounded_names)
    assert "kanban_show" in bounded_names
    assert len(json.dumps(bounded, sort_keys=True)) < len(
        json.dumps(broad, sort_keys=True)
    )


def test_dispatcher_bounded_worker_mandatory_toolsets_survive_profile_disables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from model_tools import get_tool_definitions
    from tools.registry import registry

    context7_probe = "mcp__context7__task_toolset_probe"
    registry.register(
        name=context7_probe,
        toolset="mcp-context7",
        schema={
            "name": context7_probe,
            "description": "Task toolset regression probe",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=lambda _args, **_kwargs: "{}",
    )
    monkeypatch.setitem(registry._toolset_aliases, "context7", "mcp-context7")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_mandatory_surface")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")
    try:
        definitions = get_tool_definitions(
            enabled_toolsets=kb.effective_task_toolsets(["terminal"]),
            disabled_toolsets=["context7", "kanban"],
            quiet_mode=True,
            skip_tool_search_assembly=True,
        )
    finally:
        registry.deregister(context7_probe)

    names = {item["function"]["name"] for item in definitions}
    assert context7_probe in names
    assert "kanban_show" in names
    assert "kanban_complete" in names
