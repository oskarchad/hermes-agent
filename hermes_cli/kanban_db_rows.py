"""Task row decoding shared by native Kanban reads and legacy-cap admission."""


def task_from_row(cls, row):
    from hermes_cli.kanban_db import _json_or, _lossy_text, _row_get, _to_epoch

    g = lambda col, default=None: _lossy_text(_row_get(row, col, default))
    parsed = _json_or(g("skills"))
    skills_value = [str(s) for s in parsed if s] if isinstance(parsed, list) else None
    raw_toolsets = g("enabled_toolsets")
    task_toolsets = _json_or(raw_toolsets, raw_toolsets)
    return cls(
        **{col: _lossy_text(row[col]) for col in _TASK_REQUIRED_COLUMNS},
        **{col: _to_epoch(g(col)) for col in _TASK_EPOCH_COLUMNS},  # outside writers may store text: epoch or None
        **{col: g(col) for col in _TASK_OPTIONAL_COLUMNS},
        **{col: g(col) or None for col in _TASK_EMPTY_IS_NULL_COLUMNS},
        # Pre-migration fallbacks (spawn_failures / last_spawn_error) are only
        # reachable on a DB never opened since the rename migration landed.
        consecutive_failures=g("consecutive_failures", g("spawn_failures", 0)),
        last_failure_error=g("last_failure_error", g("last_spawn_error")),
        skills=skills_value,
        enabled_toolsets=raw_toolsets if task_toolsets is None else task_toolsets,
        goal_mode=bool(g("goal_mode")),
        block_recurrences=int(g("block_recurrences") or 0),
    )


# Columns every schema version has (KeyError if the SELECT omitted them).
_TASK_REQUIRED_COLUMNS = (
    "id", "title", "body", "assignee", "status", "priority", "created_by",
    "workspace_kind", "workspace_path", "claim_lock",
)
_TASK_EPOCH_COLUMNS = ("created_at", "started_at", "completed_at", "claim_expires", "last_heartbeat_at")
# Later-added columns read as NULL when absent from the row.
_TASK_OPTIONAL_COLUMNS = (
    "branch_name", "project_id", "tenant", "result", "idempotency_key", "worker_pid",
    "max_runtime_seconds", "current_run_id", "workflow_template_id",
    "current_step_key", "max_retries", "session_id", "completion_contract",
)
# Text columns where "" is stored/read as "not set".
_TASK_EMPTY_IS_NULL_COLUMNS = (
    "model_override", "provider_override", "reasoning_effort", "goal_max_turns", "block_kind",
)
