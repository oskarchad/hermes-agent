"""A checkpoint finishing after abandonment must not start the requested backend."""
import json
import threading
from types import SimpleNamespace

import pytest

from agent import tool_executor as te
from agent.tool_guardrails import ToolCallGuardrailController
from tools import interrupt


@pytest.mark.parametrize("stop", ["deadline", "interrupt", "none"])
def test_checkpoint_return_after_abandonment_does_not_dispatch(tmp_path, monkeypatch, stop):
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    dispatched = []
    workers = []

    def delayed_checkpoint(*args):
        workers.append(threading.get_ident())
        entered.set()
        assert release.wait(10)
        return True

    agent = SimpleNamespace(
        _checkpoint_mgr=SimpleNamespace(enabled=True, ensure_checkpoint=delayed_checkpoint),
        quiet_mode=True, verbose_logging=False, tool_progress_mode="off",
        tool_progress_callback=None, tool_start_callback=None, tool_complete_callback=None,
        _touch_activity=lambda *_: None, _tool_guardrails=ToolCallGuardrailController(),
        session_id="checkpoint-cancel", _interrupt_requested=False,
        _tool_worker_threads=set(), _tool_worker_threads_lock=threading.Lock(),
    )
    # Use the real interrupt implementation through the executor's facade seam;
    # importing the whole CLI would run update recovery against this worktree's
    # external Git metadata instead of the temporary test home.
    monkeypatch.setattr(te, "_ra", lambda: SimpleNamespace(_set_interrupt=interrupt.set_interrupt))
    monkeypatch.setattr(te, "_resolve_sequential_tool_timeout", lambda: 2 if stop == "deadline" else 30)
    monkeypatch.setattr(te, "_SEQUENTIAL_INTERRUPT_POLL_SECONDS", 0.05)
    # Observe actual pipeline completion, not a blind sleep after release.
    pipeline = te._run_agent_tool_execution_middleware
    def observed(*args, **kwargs):
        try:
            return pipeline(*args, **kwargs)
        finally:
            finished.set()
    monkeypatch.setattr(te, "_run_agent_tool_execution_middleware", observed)

    def request_stop():
        assert entered.wait(5)
        if stop == "interrupt":
            agent._interrupt_requested = True
        elif stop == "none":
            release.set()
    controller = threading.Thread(target=request_stop)
    controller.start()
    try:
        result = te._run_sequential_tool_execution_middleware(
            agent, function_name="terminal",
            function_args={"command":"printf fixture > output.txt", "workdir":str(tmp_path)},
            effective_task_id="checkpoint-cancel", tool_call_id="checkpoint-cancel-call",
            execute=lambda args: dispatched.append(args) or json.dumps({"exit_code":0}),
        )
        if stop == "none":
            assert json.loads(result.result) == {"exit_code": 0}
        else:
            assert isinstance(result.result, te._ToolTimeoutResult if stop == "deadline" else te._ToolCancelledResult)
        assert entered.is_set()
    finally:
        release.set()
        controller.join(5)
        assert finished.wait(5)
        for tid in workers:
            interrupt.set_interrupt(False, tid)
    assert len(dispatched) == (1 if stop == "none" else 0), "abandoned checkpoint launched the backend after returning"
