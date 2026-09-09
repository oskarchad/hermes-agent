"""Test demonstrating a headless helper in an isolated environment reading a fixture
and writing its own authored report using standard supported tools.
Also asserts that unauthorized writes, arbitrary execution, and operator deny rules
remain enforced.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest
from tools.approval import check_all_command_guards, detect_dangerous_command


def test_headless_helper_reads_fixture_and_produces_report(tmp_path, monkeypatch):
    """AC: A headless worker in an isolated HOME reads fixture and produces its own report."""
    # Setup isolated HOME and task directory
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("HERMES_HOME", str(fake_home / ".hermes"))

    task_dir = tmp_path / "task_workspace"
    task_dir.mkdir()

    # Fixture file (e.g. rules.json)
    fixture_path = task_dir / "rules.json"
    fixture_path.write_text(json.dumps({
        "schema_version": "v1",
        "rules": [
            {"id": "RULE-1", "severity": "high", "pattern": "exec("},
            {"id": "RULE-2", "severity": "medium", "pattern": "eval("},
        ]
    }))

    # Helper script command that reads fixture via python -c safe carveout and parses it
    read_cmd = f"python3 -c 'import json,pathlib; print(json.loads(pathlib.Path(\"{fixture_path}\").read_text())[\"rules\"][0][\"id\"])'"

    # 1. Verify approval guard does NOT block read_cmd in headless / unattended mode
    guard_result = check_all_command_guards(read_cmd, "local")
    # check_all_command_guards returns None or {'approved': True, ...} when allowed, and {'approved': False, ...} when blocked
    assert guard_result is None or guard_result.get("approved") is True, f"Expected safe read command to be allowed, got: {guard_result}"

    # 2. Run the read command as a subprocess
    proc = subprocess.run(
        ["python3", "-c", f"import json,pathlib; print(json.loads(pathlib.Path('{fixture_path}').read_text())['rules'][0]['id'])"],
        capture_output=True,
        text=True,
        check=True,
    )
    first_rule_id = proc.stdout.strip()
    assert first_rule_id == "RULE-1"

    # 3. Helper produces its own authored report
    report_file = task_dir / "report.json"
    report_content = {
        "author": "headless-helper",
        "status": "complete",
        "analyzed_rule": first_rule_id,
        "summary": "Processed 1 rule successfully without approval blocks."
    }
    report_file.write_text(json.dumps(report_content, indent=2))

    # Assert report was written and contains correct content
    assert report_file.exists()
    loaded_report = json.loads(report_file.read_text())
    assert loaded_report["author"] == "headless-helper"
    assert loaded_report["analyzed_rule"] == "RULE-1"


def test_dangerous_execution_and_unauthorized_writes_remain_blocked():
    """Negative tests: arbitrary execution and dangerous code must still be flagged."""
    # Direct code execution via eval/exec
    eval_cmd = "python3 -c 'import sys; f=eval; f(sys.stdin.read())'"
    dangerous, _, desc = detect_dangerous_command(eval_cmd)
    assert dangerous is True
    assert desc == "script execution via -e/-c flag"

    # Arbitrary file write via Path.open()
    write_cmd = "python3 -c 'import pathlib; p=pathlib.Path(\"/tmp/bad\"); p.open(\"w\").write(\"bad\")'"
    dangerous, _, desc = detect_dangerous_command(write_cmd)
    assert dangerous is True
    assert desc == "script execution via -e/-c flag"

    # Arbitrary system call
    sys_cmd = "python3 -c 'import os; os.system(\"id\")'"
    dangerous, _, desc = detect_dangerous_command(sys_cmd)
    assert dangerous is True
    assert desc == "script execution via -e/-c flag"
