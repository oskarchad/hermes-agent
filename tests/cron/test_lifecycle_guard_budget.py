"""Whole-walk work budget for the gateway lifecycle guard (#78398).

The per-file byte cap and recursion depth bound one read, not the walk. These
tests pin the shared budget that bounds the whole referenced-script walk and
is charged *before* any text reaches ``shlex``.

Budget constants are monkeypatched to tiny values so the tests are fast and
deterministic; ``_LifecycleScanBudget`` reads them at construction time.
"""

from __future__ import annotations

import pytest

import cron.lifecycle_guard as lifecycle_guard

guard = lifecycle_guard.contains_gateway_lifecycle_command_or_referenced_script


def _explode(*_args, **_kwargs):
    raise AssertionError("over-budget text reached shlex")


# --- root command (depth 0) -----------------------------------------------


def test_root_byte_limit_allows_exact_and_rejects_plus_one(monkeypatch):
    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_BYTES", 8)
    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_LINE_BYTES", 8)

    assert guard("x" * 8) is False

    monkeypatch.setattr(lifecycle_guard.shlex, "shlex", _explode)
    assert guard("x" * 9) is True


def test_root_line_limit_allows_exact_and_rejects_plus_one(monkeypatch):
    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_LINES", 2)

    assert guard("one\ntwo") is False
    assert guard("one\ntwo\nthree") is True


def test_single_giant_line_rejected_before_shlex(monkeypatch):
    """One enormous token is the quadratic shlex case."""
    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_LINE_BYTES", 8)
    monkeypatch.setattr(lifecycle_guard.shlex, "shlex", _explode)

    assert guard("xxxxxxxxx\necho ok") is True


def test_root_budget_counts_utf8_bytes(monkeypatch):
    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_BYTES", 4)
    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_LINE_BYTES", 4)

    assert guard("éé") is False
    assert guard("ééé") is True


def test_exhaustion_is_logged_at_warning(monkeypatch, caplog):
    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_BYTES", 4)
    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_LINE_BYTES", 4)

    with caplog.at_level("WARNING", logger=lifecycle_guard.logger.name):
        assert guard("echo hello") is True
    assert "budget exhausted" in caplog.text


def test_lifecycle_scan_root_within_budget_is_not_a_verdict(monkeypatch):
    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_BYTES", 8)
    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_LINE_BYTES", 8)

    assert lifecycle_guard.lifecycle_scan_root_within_budget("x" * 8) is True
    assert lifecycle_guard.lifecycle_scan_root_within_budget("x" * 9) is False


# --- referenced-script walk ------------------------------------------------


def test_unique_path_budget_bounds_reads_and_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_PATHS", 2)
    for i in range(3):
        (tmp_path / f"s{i}.sh").write_text("echo ok\n", encoding="utf-8")

    two = " && ".join(f"bash {tmp_path}/s{i}.sh" for i in range(2))
    three = " && ".join(f"bash {tmp_path}/s{i}.sh" for i in range(3))

    assert guard(two) is False
    assert guard(three) is True


def test_repeated_path_does_not_spend_unique_path_budget(monkeypatch, tmp_path):
    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_PATHS", 1)
    script = tmp_path / "s.sh"
    script.write_text("echo ok\n", encoding="utf-8")

    assert guard(f"bash {script} && bash {script} && sh {script}") is False


def test_remote_read_budget_charged_before_remote_read(monkeypatch):
    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_REMOTE_READS", 1)
    reads: list[str] = []

    def remote(path: str):
        reads.append(path)
        return "echo ok\n"

    assert (
        guard(
            "bash /remote/a.sh && bash /remote/b.sh",
            read_remote_script=remote,
        )
        is True
    )
    assert reads == ["/remote/a.sh"]


def test_cumulative_text_budget_bounds_recursive_scan(monkeypatch, tmp_path):
    """Two scripts individually under the per-file cap exceed the walk cap.

    Relative references keep the root command short so the budget arithmetic
    is about the scripts, not the tmp_path length."""
    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_BYTES", 48)
    (tmp_path / "a.sh").write_text("echo " + "a" * 10 + "\n", encoding="utf-8")  # 16
    (tmp_path / "b.sh").write_text("echo " + "b" * 10 + "\n", encoding="utf-8")  # 16
    cwd = str(tmp_path)

    # 9 (root) + 16 fits in 48; 19 (root) + 16 + 16 does not → fail closed.
    assert guard("bash a.sh", cwd=cwd) is False
    assert guard("bash a.sh;bash b.sh", cwd=cwd) is True


def test_referenced_read_is_capped_at_remaining_budget(monkeypatch, tmp_path):
    """A file bigger than what the walk can still afford is never read whole:
    the read helper receives the remaining budget as its cap."""
    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_BYTES", 64)
    (tmp_path / "big.sh").write_text("echo " + "x" * 200 + "\n", encoding="utf-8")

    caps: list = []
    original = lifecycle_guard._read_referenced_script

    def spy(path, *, max_bytes=None):
        caps.append(max_bytes)
        return original(path, max_bytes=max_bytes)

    monkeypatch.setattr(lifecycle_guard, "_read_referenced_script", spy)

    root = "bash big.sh"
    assert guard(root, cwd=str(tmp_path)) is True
    assert caps == [64 - len(root)]


def test_remote_script_sanitizer_honours_remaining_budget():
    text, unsafe = lifecycle_guard._sanitize_remote_script_text(
        "echo ok\n", max_bytes=4
    )
    assert (text, unsafe) == (None, True)
    text, unsafe = lifecycle_guard._sanitize_remote_script_text(
        "echo ok\n", max_bytes=8
    )
    assert (text, unsafe) == ("echo ok\n", False)


def test_line_budget_fails_closed_before_tokenizing_every_line(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_LINES", 4)
    script = tmp_path / "many.sh"
    script.write_text("echo ok\n" * 10, encoding="utf-8")

    lexers = 0
    real_shlex = lifecycle_guard.shlex.shlex

    def counting(*args, **kwargs):
        nonlocal lexers
        lexers += 1
        return real_shlex(*args, **kwargs)

    monkeypatch.setattr(lifecycle_guard.shlex, "shlex", counting)
    root = f"bash {script}"
    assert guard(root) is True
    # Only the one-line root was tokenized (a handful of lexers across the
    # direct scans); the 10-line script never was.
    assert 0 < lexers < 10


def test_python_data_operands_do_not_spend_script_read_budget(monkeypatch, tmp_path):
    """A Python call argument is not a shell command after an opening parenthesis."""
    data = tmp_path / "large.data"
    with data.open("wb") as stream:
        stream.truncate(lifecycle_guard._MAX_REFERENCED_SCRIPT_BYTES + 1)
    reads = []
    original = lifecycle_guard._read_referenced_script

    def spy(path, *, max_bytes=None):
        reads.append(path)
        return original(path, max_bytes=max_bytes)

    monkeypatch.setattr(lifecycle_guard, "_read_referenced_script", spy)
    command = (
        "python3 - <<'PY'\n"
        "from pathlib import Path\n"
        f"conn=connect(db_path=Path({str(data)!r}))\n"
        f"prefix=Path({str(data)!r}).read_text()\n"
        "PY"
    )
    # Classification only: never execute this input or open a real database.
    assert guard(command, cwd=str(tmp_path)) is False
    assert data not in reads
    # UTF-8 AST columns are byte offsets; retain the command after the terminator.
    unicode_command = command.replace("conn=", "é = 1; conn=") + "\n"
    assert guard(unicode_command, cwd=str(tmp_path)) is False
    assert data not in reads
    # Unknown call operands and incomplete Python retain fail-closed behavior.
    assert guard(command.replace("Path(", "unknown("), cwd=str(tmp_path)) is True
    assert guard(command.replace("conn=", "conn=("), cwd=str(tmp_path)) is True
    # The same bytes in an executable position must still fail closed.
    assert guard(f"sh {data}", cwd=str(tmp_path)) is True
    assert data in reads


def test_python_argv_data_and_script_positions_share_bounded_walk(monkeypatch, tmp_path):
    data = tmp_path / "large.data"
    with data.open("wb") as stream:
        stream.truncate(lifecycle_guard._MAX_REFERENCED_SCRIPT_BYTES + 1)
    wrapper = tmp_path / "wrapper.sh"
    wrapper.write_text("hermes gateway stop\n", encoding="utf-8")

    def classify(argv):
        return guard(
            "python3 - <<'PY'\nimport subprocess\nsubprocess.run(" + repr(argv) + ")\nPY",
            cwd=str(tmp_path),
        )

    reads = []
    original = lifecycle_guard._read_referenced_script

    def spy(path, *, max_bytes=None):
        reads.append(path)
        return original(path, max_bytes=max_bytes)

    monkeypatch.setattr(lifecycle_guard, "_read_referenced_script", spy)
    assert classify(["printf", "%s", f"({data}) hermes gateway stop"]) is False
    assert data not in reads
    for argv in ([str(wrapper)], ["sh", str(wrapper)], ["sudo", "sh", str(wrapper)], ["sh", str(data)]):
        assert classify(argv) is True
    assert wrapper in reads and data in reads
    # No subprocess-wide exemption: executable paths retain the shared limits.
    benign = tmp_path / "benign.sh"
    benign.write_text("echo ok\n", encoding="utf-8")
    with monkeypatch.context() as m:
        m.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_PATHS", 0)
        assert classify(["sh", str(benign)]) is True
    with monkeypatch.context() as m:
        m.setattr(lifecycle_guard, "_MAX_REFERENCED_SCRIPT_DEPTH", 1)
        assert classify(["sh", str(benign)]) is True
    with monkeypatch.context() as m:
        m.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_REMOTE_READS", 0)
        assert guard(
            "python3 - <<'PY'\nsubprocess.run(['sh', '/remote/absent.sh'])\nPY",
            read_remote_script=lambda path: pytest.fail("exhausted remote reader called"),
        ) is True


@pytest.mark.parametrize("argv", [
    ["python3", "-c", "print('ok')"],
    ["python3.12", "-I", "-cprint('ok')"],
    ["python3", "benign.py"],
    ["node", "--eval=console.log('ok')"],
    ["perl", "-e", "print 'ok'"],
    ["ruby", "-e", "puts 'ok'"],
    ["php", "-r", "print('ok');"],
    ["pwsh", "-command", "Write-Output ok"],
    ["osascript", "-e", 'return "ok"'],
    ["xargs", "printf"],
    ["eval", "printf ok"],
    ["env", "--split-string=printf ok"],
    ["timeout", "10", "env", "-Sprintf ok"],
    ["timeout", "10", "su", "--command=printf ok", "nobody"],
    ["sudo", "runuser", "-c", "printf ok", "nobody"],
    ["sh", "-lc", "printf ok"],
    ["sh", "--command=printf ok"],
    ["sh", "-c"],
    ["source"],
    ["env"],
    ["timeout", "10"],
    ["env"] * (lifecycle_guard._MAX_PREFIX_PEELS + 1) + ["printf", "ok"],
])
def test_python_argv_unowned_execution_is_refusal_not_detection(argv, tmp_path, caplog):
    from tools import process_registry
    from tools.terminal_tool_guards import gateway_lifecycle_block
    from unittest.mock import patch

    command = "python3 - <<'PY'\nsubprocess.run(" + repr(argv) + ")\nPY"
    # Innocuous, unsupported execution is still refused, not certified as data.
    with caplog.at_level("WARNING", logger=lifecycle_guard.logger.name):
        assert guard(command, cwd=str(tmp_path)) is True
    assert "incomplete process argv inspection" in caplog.text
    assert "falling back to direct-scan verdict" not in caplog.text
    with patch.object(process_registry, "_is_supervised_gateway_process", return_value=True):
        assert gateway_lifecycle_block(
            command=command, env=object(), env_type="local", cwd=str(tmp_path),
            workdir=str(tmp_path), session_key="coverage-refusal",
        ) is not None
    # The new boundary applies only inside the complete quoted Python stdin lane.
    assert guard("python3 -c \"print('ok')\"", cwd=str(tmp_path)) is False
    assert guard("python3 - <<'PY'\nsubprocess.run(['sh', '-c', 'printf ok'])\nPY",
                 cwd=str(tmp_path)) is False


# --- scheduler entry point --------------------------------------------------


def test_check_gateway_lifecycle_shell_script_budget(monkeypatch, tmp_path):
    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_BYTES", 8)
    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_LINE_BYTES", 8)
    script = tmp_path / "long-line.sh"

    script.write_text("x" * 7, encoding="utf-8")
    lifecycle_guard.check_gateway_lifecycle("", str(script))

    script.write_text("x" * 9, encoding="utf-8")
    with pytest.raises(lifecycle_guard.GatewayLifecycleBlocked):
        lifecycle_guard.check_gateway_lifecycle("", str(script))


def test_check_gateway_lifecycle_python_path_charges_masker(monkeypatch, tmp_path):
    """The .py branch's data-exemption masker tokenizes too, so it is budgeted
    and fails closed before shlex on an over-budget line."""
    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_LINE_BYTES", 16)

    small = tmp_path / "small.py"
    small.write_text("x = 1\n", encoding="utf-8")
    lifecycle_guard.check_gateway_lifecycle("run report", str(small))

    monkeypatch.setattr(lifecycle_guard.shlex, "shlex", _explode)
    long_line = tmp_path / "long.py"
    long_line.write_text("x = 1\n" + "y" * 40 + "\n", encoding="utf-8")
    with pytest.raises(lifecycle_guard.GatewayLifecycleBlocked):
        lifecycle_guard.check_gateway_lifecycle("run report", str(long_line))


# --- no regression on realistic benign graphs ------------------------------


def test_default_budget_admits_a_wide_benign_wrapper_graph(tmp_path):
    """Issue #78398's shape: one wrapper invoking 200 small legitimate scripts
    must still be allowed under the DEFAULT limits (an earlier fail-closed
    attempt with a 64-path cap blocked exactly this)."""
    children = []
    for i in range(200):
        child = tmp_path / f"c{i}.sh"
        child.write_text("echo step && ls -la /tmp\n" * 20, encoding="utf-8")
        children.append(child)
    hub = tmp_path / "hub.sh"
    hub.write_text("".join(f"bash {c}\n" for c in children), encoding="utf-8")

    assert guard(f"bash {hub}") is False

    # ...and a lifecycle command hidden behind the 200 benign scripts is still
    # found: the budget bounds work, it does not stop the walk early.
    evil = tmp_path / "evil.sh"
    evil.write_text("hermes gateway restart\n", encoding="utf-8")
    hub.write_text(hub.read_text() + f"bash {evil}\n", encoding="utf-8")
    assert guard(f"bash {hub}") is True
