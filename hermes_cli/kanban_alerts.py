"""Bounded, redacted presentation helpers for Kanban terminal alerts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

KANBAN_ALERT_MAX_CHARS = 700
_ATOM_MAX_CHARS = 80
_REDACTION_FAILURE_TEXT = "Kanban notification unavailable (redaction failed)"


@dataclass(frozen=True)
class KanbanEventLineage:
    """Fresh task/run projection attached to one historical event."""

    text: str
    lineage: str
    retry_is_current: bool
    block_is_current: bool


def _redact_text(value: Any) -> str:
    from agent.redact import redact_sensitive_text

    return redact_sensitive_text(str(value), force=True)


def _bounded_atom(value: Any, *, fallback: str) -> str:
    if value is None or value == "":
        return fallback
    try:
        text = _redact_text(value)
    except Exception:
        return "[redacted]"
    text = " ".join(text.split())
    if not text:
        return fallback
    if len(text) > _ATOM_MAX_CHARS:
        return f"{text[: _ATOM_MAX_CHARS - 1]}…"
    return text


def project_kanban_event_lineage(task: Any, event: Any, latest_run: Any) -> KanbanEventLineage:
    """Classify an event against a fresh task projection without mutating state."""

    event_run_id = getattr(event, "run_id", None)
    latest_run_id = getattr(latest_run, "id", None)
    status = getattr(task, "status", None)
    assignee = getattr(task, "assignee", None)
    current_run_id = getattr(task, "current_run_id", None)

    if event_run_id is None or latest_run_id is None:
        lineage = "unknown"
    elif int(event_run_id) == int(latest_run_id):
        lineage = "current"
    else:
        lineage = "superseded"

    status_text = _bounded_atom(status, fallback="missing")
    projection = (
        "["
        f"event_run_id={_bounded_atom(event_run_id, fallback='none')}; "
        f"status={status_text}; "
        f"assignee={_bounded_atom(assignee, fallback='unassigned')}; "
        f"current_run_id={_bounded_atom(current_run_id, fallback='none')}; "
        f"lineage={lineage}"
        "]"
    )
    return KanbanEventLineage(
        text=projection,
        lineage=lineage,
        retry_is_current=lineage == "current" and status_text in {"ready", "review"},
        block_is_current=lineage == "current" and status_text == "blocked",
    )


def render_kanban_alert(
    text: str,
    *,
    lineage: Optional[KanbanEventLineage] = None,
) -> str:
    """Force-redact and cap an alert while preserving its lineage projection."""

    try:
        redacted = _redact_text(text)
        suffix = f"\n{_redact_text(lineage.text)}" if lineage is not None else ""
    except Exception:
        return _REDACTION_FAILURE_TEXT

    prefix_limit = KANBAN_ALERT_MAX_CHARS - len(suffix)
    if prefix_limit <= 0:
        return suffix[-KANBAN_ALERT_MAX_CHARS :]
    if len(redacted) > prefix_limit:
        redacted = f"{redacted[: max(0, prefix_limit - 1)]}…"
    return f"{redacted}{suffix}"