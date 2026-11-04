"""Pre-mutation handoff readiness is not final review/release acceptance.

Historical refusal is verbatim from judge-visible-handoff-receipt.json on
t_e9de8df4. The goal/package below retains its PARTIAL -> INITIAL -> release shape.
The private live replay uses the complete original body/summary, not these excerpts.
"""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_cli import goals, kanban
from tools import kanban_tools

HISTORICAL_REFUSAL = (
    "The frozen author package and readiness checks are documented, but the guarded "
    "request-review handoff has not yet been executed; submit it once, preserving "
    "any exact refusal without retrying."
)


@pytest.mark.parametrize("port", ["tool", "cli"])
@pytest.mark.parametrize("operation", ["request-review", "complete"])
def test_gate_sends_current_operation_to_real_judge_prompt(port, operation):
    task = SimpleNamespace(
        id="task-1", goal_mode=True, title="HER-335: Hook first 3–4 seconds",
        body="Author: frozen PR, CI and raw evidence. PARTIAL stays PARTIAL. "
             "INITIAL Gauge 0/1 is next. Release requires approval and green CI.",
    )
    response = "Frozen author package exists; PARTIAL/OPEN. Review and release have not happened."
    captured = []

    def call_llm(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({"verdict": "continue", "reason": HISTORICAL_REFUSAL})))])

    with patch("agent.auxiliary_client.get_text_auxiliary_client", return_value=(object(), "model")), \
         patch("agent.auxiliary_client.call_llm", side_effect=call_llm), \
         patch.object(kanban_tools, "_goal_judge_available", return_value=True):
        if port == "tool":
            with pytest.raises(kanban_tools._Reject, match="handoff has not yet been executed"):
                kanban_tools._goal_gate("kanban_" + operation.replace("-", "_"), task, task.id, response)
        else:
            with patch.object(kanban.kb, "get_task", return_value=task):
                error = kanban._goal_gate_error(
                    None, task.id, response,
                    "review handoff" if operation == "request-review" else "completion", "block", "evidence")
            assert error is not None and HISTORICAL_REFUSAL in error

    assert len(captured) == 1
    system, user = captured[0]["messages"]
    assert f"Current Kanban operation: {operation}" in system["content"]
    assert "before its state mutation" in system["content"]
    assert task.title in user["content"] and response in user["content"]
    if operation == "request-review":
        assert "Do not require this request-review to have already succeeded" in system["content"]
        assert "does not approve the implementation" in system["content"]
    else:
        assert "not an automatic allow" in system["content"]


def test_ordinary_goal_judge_keeps_original_system_prompt():
    with patch("agent.auxiliary_client.call_llm") as call:
        call.return_value = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content='{"verdict":"done","reason":"deliverable exists"}'))])
        assert goals.judge_goal("goal", "evidence")[0] == "done"
    assert call.call_args.kwargs["messages"][0]["content"] == goals.JUDGE_SYSTEM_PROMPT


@pytest.mark.parametrize("port", ["tool", "cli"])
@pytest.mark.parametrize("raw, expected", [
    ('{"verdict":"done","reason":"author package ready"}', "review"),
    ('{"verdict":"continue","reason":"author package missing"}', "running"),
    ('{"verdict":"blocked","reason":"needs input"}', "running"),
    ('not JSON', "running"),
])
def test_real_entrypoints_preserve_verdict_and_native_lifecycle(monkeypatch, tmp_path, port, raw, expected):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    from tools.registry import registry

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "builder")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Frozen author package; review next", assignee="builder", goal_mode=True)
        claimed = kb.claim_task(conn, tid, claimer="builder:1")
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
    reply = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=raw))])
    with patch("agent.auxiliary_client.get_text_auxiliary_client", return_value=(object(), "model")), \
         patch("agent.auxiliary_client.call_llm", return_value=reply) as call:
        if port == "tool":
            output = json.loads(registry.get_entry("kanban_request_review").handler(
                {"summary": "Frozen code, test evidence and raw results exist; INITIAL review next."}))
            assert bool(output.get("ok")) == (expected == "review")
        else:
            output = kanban.run_slash(f"request-review {tid} --summary 'Frozen author package; INITIAL next.'")
            assert ("Requested review" in output) == (expected == "review")
        assert call.call_count == 1
        assert "Current Kanban operation: request-review" in call.call_args.kwargs["messages"][0]["content"]
    with kbc.connect_closing() as conn:
        after = kb.get_task(conn, tid)
        assert after.status == expected and after.goal_mode
        assert after.assignee == "builder"
