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


def test_default_spawn_marks_only_explicit_task_toolset_bounds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "patch"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(
        """
platform_toolsets:
  cli:
    - terminal
mcp_servers:
  context7:
    enabled: true
    command: node
    args: []
agent:
  disabled_toolsets:
    - context7
    - kanban
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_TASK_TOOLSETS_BOUNDED", "stale-parent-value")
    _allow_task_toolsets(monkeypatch)
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])

    captured: list[tuple[list[str], dict[str, str]]] = []

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured.append((list(cmd), dict(kwargs.get("env") or {})))
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with kb.connect() as conn:
        legacy_id = kb.create_task(
            conn,
            title="spawn inherited task",
            assignee="patch",
        )
        explicit_id = kb.create_task(
            conn,
            title="spawn bounded task",
            assignee="patch",
            enabled_toolsets=["terminal"],
        )
        legacy_task = kb.get_task(conn, legacy_id)
        explicit_task = kb.get_task(conn, explicit_id)

    assert legacy_task is not None
    assert explicit_task is not None

    kb._default_spawn(legacy_task, str(workspace))
    kb._default_spawn(explicit_task, str(workspace))

    legacy_cmd, legacy_env = captured[0]
    explicit_cmd, explicit_env = captured[1]
    legacy_toolsets = legacy_cmd[legacy_cmd.index("--toolsets") + 1].split(",")
    explicit_toolsets = explicit_cmd[explicit_cmd.index("--toolsets") + 1].split(",")

    assert legacy_task.effective_toolsets is None
    assert "terminal" in legacy_toolsets
    assert not ({"context7", "kanban"} & set(legacy_toolsets))
    assert "HERMES_KANBAN_TASK_TOOLSETS_BOUNDED" not in legacy_env
    assert explicit_toolsets == ["terminal", "context7", "kanban"]
    assert explicit_env["HERMES_KANBAN_TASK_TOOLSETS_BOUNDED"] == "1"

    from model_tools import get_tool_definitions
    from tools.registry import registry

    context7_probe = "mcp__context7__spawn_policy_probe"
    registry.register(
        name=context7_probe,
        toolset="mcp-context7",
        schema={
            "name": context7_probe,
            "description": "Spawn policy integration probe",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=lambda _args, **_kwargs: "{}",
    )
    monkeypatch.setitem(registry._toolset_aliases, "context7", "mcp-context7")

    def spawned_tool_names(
        toolsets: list[str], child_env: dict[str, str]
    ) -> set[str]:
        with monkeypatch.context() as child:
            for key in (
                "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_RUN_ID",
                "HERMES_KANBAN_TASK_TOOLSETS_BOUNDED",
            ):
                if key in child_env:
                    child.setenv(key, child_env[key])
                else:
                    child.delenv(key, raising=False)
            definitions = get_tool_definitions(
                enabled_toolsets=toolsets,
                disabled_toolsets=["context7", "kanban"],
                quiet_mode=True,
                skip_tool_search_assembly=True,
            )
        return {item["function"]["name"] for item in definitions}

    try:
        legacy_names = spawned_tool_names(legacy_toolsets, legacy_env)
        explicit_names = spawned_tool_names(explicit_toolsets, explicit_env)
    finally:
        registry.deregister(context7_probe)

    assert context7_probe not in legacy_names
    assert "kanban_show" in legacy_names
    assert "kanban_complete" in legacy_names
    assert context7_probe in explicit_names
    assert "kanban_show" in explicit_names
    assert "kanban_complete" in explicit_names


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


def test_bounded_worker_mandatory_alias_target_survives_profile_disable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from model_tools import get_tool_definitions
    from tools.registry import registry

    context7_probe = "mcp__context7__mandatory_alias_probe"
    registry.register(
        name=context7_probe,
        toolset="mcp-context7",
        schema={
            "name": context7_probe,
            "description": "Mandatory alias protection probe",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=lambda _args, **_kwargs: "{}",
    )
    monkeypatch.setitem(registry._toolset_aliases, "context7", "mcp-context7")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_bounded_alias")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "17")
    monkeypatch.setenv("HERMES_KANBAN_TASK_TOOLSETS_BOUNDED", "1")

    try:
        definitions = get_tool_definitions(
            enabled_toolsets=["terminal", "context7", "kanban"],
            disabled_toolsets=["mcp-context7"],
            quiet_mode=True,
            skip_tool_search_assembly=True,
        )
    finally:
        registry.deregister(context7_probe)

    names = {item["function"]["name"] for item in definitions}
    assert context7_probe in names


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


def test_dispatch_blocks_tampered_review_toolset_before_spawn(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_task_toolsets(monkeypatch)
    spawned: list[str] = []

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="tampered review",
            assignee="patch",
            enabled_toolsets=["web"],
        )
        assert kb.request_review(conn, task_id, summary="ready for review")
        conn.execute(
            "UPDATE tasks SET enabled_toolsets = ? WHERE id = ?",
            (json.dumps(["web", "unknown-review-toolset"]), task_id),
        )
        conn.commit()
        monkeypatch.setattr(
            "hermes_cli.profiles.profile_exists", lambda _name: True
        )
        monkeypatch.setattr(kb, "_memory_pressure_level", lambda: "ok")
        monkeypatch.setattr(kb, "review_dispatch_enabled", lambda: True)

        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace: spawned.append(task.id),
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
        "reason_code": "unknown_toolset",
    }


@pytest.mark.parametrize(
    ("lane", "claim_fn"),
    [
        ("ready", kb.claim_task),
        ("review", kb.claim_review_task),
    ],
)
def test_invalid_toolset_auto_block_preserves_concurrent_owner(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    lane: str,
    claim_fn,
) -> None:
    _allow_task_toolsets(monkeypatch)
    spawned: list[str] = []

    with kb.connect() as dispatcher_conn, kb.connect() as external_conn:
        task_id = kb.create_task(
            dispatcher_conn,
            title=f"concurrent {lane} claim",
            assignee="patch",
            enabled_toolsets=["web"],
        )
        if lane == "review":
            assert kb.request_review(
                dispatcher_conn, task_id, summary="ready for concurrent review"
            )
        dispatcher_conn.execute(
            "UPDATE tasks SET enabled_toolsets = ? WHERE id = ?",
            (json.dumps(["web", "unknown-after-enumeration"]), task_id),
        )
        dispatcher_conn.commit()
        monkeypatch.setattr(
            "hermes_cli.profiles.profile_exists", lambda _name: True
        )
        monkeypatch.setattr(kb, "_memory_pressure_level", lambda: "ok")
        monkeypatch.setattr(kb, "review_dispatch_enabled", lambda: True)

        claimed: dict[str, kb.Task] = {}

        def claim_during_validation(*_args, **_kwargs) -> set[str]:
            if not claimed:
                successor = claim_fn(
                    external_conn,
                    task_id,
                    claimer=f"external-{lane}-owner",
                )
                assert successor is not None
                assert successor.current_run_id is not None
                assert successor.claim_lock is not None
                assert kb._set_worker_pid(
                    external_conn,
                    task_id,
                    4242,
                    expected_run_id=successor.current_run_id,
                    expected_claim_lock=successor.claim_lock,
                )
                owned = kb.get_task(external_conn, task_id)
                assert owned is not None
                claimed["task"] = owned
            return {"context7", "kanban", "web"}

        monkeypatch.setattr(
            kb, "_available_task_toolset_names", claim_during_validation
        )

        result = kb.dispatch_once(
            dispatcher_conn,
            spawn_fn=lambda task, workspace: spawned.append(task.id),
        )
        successor = claimed["task"]
        current = kb.get_task(dispatcher_conn, task_id)
        run = dispatcher_conn.execute(
            "SELECT status, outcome, ended_at, claim_lock, worker_pid "
            "FROM task_runs WHERE id = ?",
            (successor.current_run_id,),
        ).fetchone()
        events = kb.list_events(dispatcher_conn, task_id)

    assert spawned == []
    assert not result.spawned
    assert result.auto_blocked == []
    assert current is not None and current.status == "running"
    assert current.claim_lock == successor.claim_lock
    assert current.current_run_id == successor.current_run_id
    assert current.worker_pid == 4242
    assert run is not None and run["status"] == "running"
    assert run["outcome"] is None
    assert run["ended_at"] is None
    assert run["claim_lock"] == successor.claim_lock
    assert run["worker_pid"] == 4242
    assert not any(
        event.kind == "toolsets_validation_failed" for event in events
    )


def test_invalid_toolset_auto_block_rechecks_concurrent_config_update(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_task_toolsets(monkeypatch)

    with kb.connect() as dispatcher_conn, kb.connect() as external_conn:
        task_id = kb.create_task(
            dispatcher_conn,
            title="concurrent toolset repair",
            assignee="patch",
            enabled_toolsets=["web"],
        )
        dispatcher_conn.execute(
            "UPDATE tasks SET enabled_toolsets = ? WHERE id = ?",
            (json.dumps(["web", "unknown-before-repair"]), task_id),
        )
        dispatcher_conn.commit()
        monkeypatch.setattr(
            "hermes_cli.profiles.profile_exists", lambda _name: True
        )
        monkeypatch.setattr(kb, "_memory_pressure_level", lambda: "ok")

        repaired = False

        def repair_during_validation(*_args, **_kwargs) -> set[str]:
            nonlocal repaired
            if not repaired:
                external_conn.execute(
                    "UPDATE tasks SET enabled_toolsets = ? WHERE id = ?",
                    (json.dumps(["web"]), task_id),
                )
                external_conn.commit()
                repaired = True
            return {"context7", "kanban", "web"}

        monkeypatch.setattr(
            kb, "_available_task_toolset_names", repair_during_validation
        )
        first = kb.dispatch_once(dispatcher_conn, spawn_fn=lambda *_args: None)
        current = kb.get_task(dispatcher_conn, task_id)
        first_events = kb.list_events(dispatcher_conn, task_id)

        monkeypatch.setattr(
            kb,
            "_available_task_toolset_names",
            lambda *_args, **_kwargs: {"context7", "kanban", "web"},
        )
        second = kb.dispatch_once(
            dispatcher_conn,
            dry_run=True,
            spawn_fn=lambda *_args: None,
        )

    assert first.auto_blocked == []
    assert current is not None and current.status == "ready"
    assert current.enabled_toolsets == ["web"]
    assert not any(
        event.kind == "toolsets_validation_failed" for event in first_events
    )
    assert [item[0] for item in second.spawned] == [task_id]


def test_invalid_toolset_auto_block_rechecks_concurrent_assignee_update(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_task_toolsets(monkeypatch)

    with kb.connect() as dispatcher_conn, kb.connect() as external_conn:
        task_id = kb.create_task(
            dispatcher_conn,
            title="concurrent assignee repair",
            assignee="patch",
            enabled_toolsets=["terminal"],
        )
        monkeypatch.setattr(
            "hermes_cli.profiles.profile_exists", lambda _name: True
        )
        monkeypatch.setattr(kb, "_memory_pressure_level", lambda: "ok")
        monkeypatch.setattr(kb, "_profile_home_for_task", lambda assignee: assignee)

        reassigned = False

        def reassign_during_validation(profile_home: str | None) -> set[str]:
            nonlocal reassigned
            if not reassigned:
                assert profile_home == "patch"
                external_conn.execute(
                    "UPDATE tasks SET assignee = ? WHERE id = ?",
                    ("gauge", task_id),
                )
                external_conn.commit()
                reassigned = True
                return {"kanban", "terminal"}
            return {"context7", "kanban", "terminal"}

        monkeypatch.setattr(
            kb, "_available_task_toolset_names", reassign_during_validation
        )
        first = kb.dispatch_once(dispatcher_conn, spawn_fn=lambda *_args: None)
        current = kb.get_task(dispatcher_conn, task_id)
        first_events = kb.list_events(dispatcher_conn, task_id)

        second = kb.dispatch_once(
            dispatcher_conn,
            dry_run=True,
            spawn_fn=lambda *_args: None,
        )

    assert first.auto_blocked == []
    assert current is not None and current.status == "ready"
    assert current.assignee == "gauge"
    assert not any(
        event.kind == "toolsets_validation_failed" for event in first_events
    )
    assert second.spawned == [(task_id, "gauge", "")]


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
    monkeypatch.setenv("HERMES_KANBAN_TASK_TOOLSETS_BOUNDED", "1")
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


def test_task_bound_marker_changes_cached_mandatory_toolset_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from model_tools import get_tool_definitions
    from tools.registry import registry

    context7_probe = "mcp__context7__task_bound_cache_probe"
    registry.register(
        name=context7_probe,
        toolset="mcp-context7",
        schema={
            "name": context7_probe,
            "description": "Task bound cache identity probe",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=lambda _args, **_kwargs: "{}",
    )
    monkeypatch.setitem(registry._toolset_aliases, "context7", "mcp-context7")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_bound_cache")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "8")
    monkeypatch.delenv("HERMES_KANBAN_TASK_TOOLSETS_BOUNDED", raising=False)
    enabled = ["terminal", "context7", "kanban"]
    disabled = ["context7", "kanban"]
    try:
        inherited = get_tool_definitions(
            enabled_toolsets=enabled,
            disabled_toolsets=disabled,
            quiet_mode=True,
            skip_tool_search_assembly=True,
        )
        monkeypatch.setenv("HERMES_KANBAN_TASK_TOOLSETS_BOUNDED", "1")
        bounded = get_tool_definitions(
            enabled_toolsets=enabled,
            disabled_toolsets=disabled,
            quiet_mode=True,
            skip_tool_search_assembly=True,
        )
    finally:
        registry.deregister(context7_probe)

    inherited_names = {item["function"]["name"] for item in inherited}
    bounded_names = {item["function"]["name"] for item in bounded}
    assert context7_probe not in inherited_names
    assert "kanban_show" in inherited_names
    assert context7_probe in bounded_names
    assert "kanban_show" in bounded_names