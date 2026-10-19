"""A checkpoint finishing after abandonment must not start the requested backend."""
import json
import threading
from types import SimpleNamespace

import pytest

from agent import tool_executor as te
from agent.tool_guardrails import ToolCallGuardrailController
from agent.interrupt_control import InterruptControlMixin
from tools import interrupt


@pytest.mark.parametrize("stop", ["deadline", "interrupt", "grace", "none"])
def test_checkpoint_return_after_abandonment_does_not_dispatch(tmp_path, monkeypatch, stop):
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    dispatched = []
    workers = []
    posts = []
    signalled = threading.Event()

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
        session_id="checkpoint-cancel", _interrupt_requested=False, _execution_thread_id=None,
        _tool_worker_threads=set(), _tool_worker_threads_lock=threading.Lock(),
    )
    # Use the real interrupt implementation through the executor's facade seam;
    # importing the whole CLI would run update recovery against this worktree's
    # external Git metadata instead of the temporary test home.
    def set_signal(value, tid=None, **kwargs):
        interrupt.set_interrupt(value, tid, **kwargs)
        if value:
            signalled.set()
    monkeypatch.setattr(te, "_ra", lambda: SimpleNamespace(_set_interrupt=set_signal))
    monkeypatch.setattr(te, "_emit_terminal_post_tool_call", lambda *a, **kw: posts.append(kw))
    monkeypatch.setattr(te, "_commit_tool_result", lambda *a, **kw: None)
    monkeypatch.setattr(te, "apply_transform_tool_result", lambda *a, **kw: kw["result"])
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
        if stop in ("interrupt", "grace"):
            agent._interrupt_requested = True
            if stop == "grace":
                assert signalled.wait(5)
                release.set()
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
        elif stop != "grace":
            assert isinstance(result.result, te._ToolTimeoutResult if stop == "deadline" else te._ToolCancelledResult)
        assert entered.is_set()
        te._publish_sequential_result(
            agent, [], te._ToolCallRef("terminal", {}, "checkpoint-cancel", "checkpoint-cancel-call", []),
            result, tool_duration=0, index=0, budget=None, transform_applied=True,
        )
        assert len(posts) == 1
        InterruptControlMixin.clear_interrupt(agent)
    finally:
        release.set()
        controller.join(5)
        assert finished.wait(5)
        for tid in workers:
            interrupt.set_interrupt(False, tid)
    assert len(dispatched) == (1 if stop == "none" else 0), "abandoned checkpoint launched the backend after returning"
    assert len(posts) == 1, "late worker emitted a duplicate terminal hook"
    next_result = te._run_sequential_tool_execution_middleware(
        agent, function_name="terminal", function_args={"command": "printf next", "workdir": str(tmp_path)},
        effective_task_id="checkpoint-cancel", tool_call_id="next-call",
        execute=lambda args: dispatched.append(args) or '{"exit_code": 0}',
    )
    assert json.loads(next_result.result) == {"exit_code": 0}
    assert len(dispatched) == (2 if stop == "none" else 1)


@pytest.mark.parametrize("abandon", [False, True])
def test_concurrent_checkpoint_cancellation_has_one_terminal_owner(tmp_path, monkeypatch, abandon):
    posts, dispatched = [], []
    agent = SimpleNamespace(
        quiet_mode=True, tool_progress_mode="off", tool_progress_callback=None,
        tool_start_callback=None, _touch_activity=lambda *_: None,
        _tool_guardrails=ToolCallGuardrailController(), session_id="concurrent-checkpoint",
        _interrupt_requested=False, _execution_thread_id=None,
        _tool_worker_threads=set(), _tool_worker_threads_lock=threading.Lock(),
        _invoke_tool=lambda *a, **kw: dispatched.append(a) or "{}",
    )
    call = SimpleNamespace(id="concurrent-call")
    parsed = te._ParsedCall(call, "terminal", {"command": "printf fixture > output.txt", "workdir": str(tmp_path)}, [], None, None)
    batch = te._ConcurrentBatch(agent, [], "concurrent-checkpoint", [parsed], 2)

    def checkpoint(*args):
        interrupt.set_interrupt(True)
        if abandon:
            batch.gate.abandon()
            InterruptControlMixin.clear_interrupt(agent)
        return True

    agent._checkpoint_mgr = SimpleNamespace(enabled=True, ensure_checkpoint=checkpoint)
    monkeypatch.setattr(te, "_ra", lambda: SimpleNamespace(_set_interrupt=interrupt.set_interrupt))
    monkeypatch.setattr(te, "_emit_terminal_post_tool_call", lambda *a, **kw: posts.append(kw))
    batch.run_worker(0, 0)
    assert dispatched == []
    if abandon:
        assert batch.results == [None]
        assert posts == []
        te._unfinished_tool_result(agent, parsed.ref("concurrent-checkpoint"), timed_out=True, timeout_s=2)
    else:
        assert batch.results[0] is not None
        assert json.loads(batch.results[0].result)["status"] == "cancelled"
    assert len(posts) == 1


@pytest.mark.parametrize("winner", ["worker", "deadline"])
def test_concurrent_terminal_selection_precedes_stalled_hook(tmp_path, monkeypatch, winner):
    checkpoint_entered, hook_entered, release, finished = [threading.Event() for _ in range(4)]
    posts, dispatched, committed = [], [], []
    agent = SimpleNamespace(
        quiet_mode=True, tool_progress_mode="off", tool_progress_callback=None,
        tool_start_callback=None, _touch_activity=lambda *_: None,
        _tool_guardrails=ToolCallGuardrailController(), session_id="terminal-race",
        _interrupt_requested=False, _execution_thread_id=None,
        _tool_worker_threads=set(), _tool_worker_threads_lock=threading.Lock(),
        _invoke_tool=lambda *a, **kw: dispatched.append(a) or "{}",
    )

    def checkpoint(*args):
        checkpoint_entered.set()
        if winner == "deadline":
            assert release.wait(10)
        interrupt.set_interrupt(True)
        return True

    def post(*args, **kwargs):
        posts.append(kwargs)
        hook_entered.set()
        if winner == "worker":
            assert release.wait(10)

    agent._checkpoint_mgr = SimpleNamespace(enabled=True, ensure_checkpoint=checkpoint)
    parsed = te._ParsedCall(SimpleNamespace(id="terminal-race-call"), "terminal",
                            {"command": "printf fixture > output.txt", "workdir": str(tmp_path)}, [], None, None)
    batch = te._ConcurrentBatch(agent, [], "terminal-race", [parsed], 2)
    original = batch.run_worker

    def worker(*args):
        try:
            return original(*args)
        finally:
            finished.set()

    monkeypatch.setattr(te, "_ra", lambda: SimpleNamespace(_set_interrupt=interrupt.set_interrupt))
    monkeypatch.setattr(te, "_emit_terminal_post_tool_call", post)
    monkeypatch.setattr(te, "_commit_tool_result", lambda *a, **kw: committed.append(a[3]))
    monkeypatch.setattr(batch, "run_worker", worker)
    try:
        batch.run()
        assert checkpoint_entered.is_set()
        if winner == "worker":
            assert hook_entered.is_set()
        te._append_batch_results(agent, [], "terminal-race", batch, None)
        selected = batch.results[0]
        assert selected is not None
        assert committed == [selected.result]
        if winner == "worker":
            assert json.loads(selected.result)["status"] == "cancelled"
        else:
            assert "timed out" in selected.result
    finally:
        release.set()
        assert finished.wait(5)
    assert dispatched == []
    assert len(posts) == 1
    assert batch.results[0] is selected, "late worker overwrote the selected terminal result"


def test_prepared_terminal_retains_batch_abandonment_after_checkpoint(tmp_path, monkeypatch):
    from agent import terminal_approval_batch as tab
    dispatched = []
    agent = SimpleNamespace(
        quiet_mode=True, tool_progress_mode="off", tool_progress_callback=None,
        tool_start_callback=None, _touch_activity=lambda *_: None,
        _tool_guardrails=ToolCallGuardrailController(), session_id="prepared-checkpoint",
        _interrupt_requested=False, _execution_thread_id=None,
        _tool_worker_threads=set(), _tool_worker_threads_lock=threading.Lock(),
    )
    parsed = te._ParsedCall(SimpleNamespace(id="prepared-call"), "terminal",
                            {"command": "printf fixture > output.txt", "workdir": str(tmp_path)}, [], None, None)
    batch = tab._TerminalBatch(agent, [], "prepared-checkpoint", [parsed])
    slot = batch.slots[0]

    def checkpoint(*args):
        batch.close()
        InterruptControlMixin.clear_interrupt(agent)
        return True

    agent._checkpoint_mgr = SimpleNamespace(enabled=True, ensure_checkpoint=checkpoint)
    monkeypatch.setattr(te, "_ra", lambda: SimpleNamespace(_set_interrupt=interrupt.set_interrupt))
    monkeypatch.setattr(te, "_resolve_sequential_dispatch", lambda *a: te._SequentialDispatch(
        execute=lambda args: dispatched.append(args) or "{}"))
    # Consent was already obtained; exercise the real slot→middleware→checkpoint boundary.
    monkeypatch.setattr(slot, "prepare", lambda ref: None)
    try:
        with pytest.raises(te._BatchAbandoned):
            slot.run()
    finally:
        batch.close()
    assert dispatched == []
