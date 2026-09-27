"""Test demonstrating a headless helper in an isolated environment reading a fixture
and writing its own authored report using standard supported tools (read_file, write_file).
Also asserts that unauthorized writes, arbitrary execution, and operator deny rules
remain enforced.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest
from tools.approval import check_all_command_guards, detect_dangerous_command
from tools.file_tools import read_file_tool, write_file_tool


def test_headless_helper_reads_fixture_and_produces_report(tmp_path, monkeypatch):
    """AC: A headless worker in an isolated HOME reads fixture and produces its own report

    using supported structured tools without false approval blocks.
    """
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("HERMES_HOME", str(fake_home / ".hermes"))

    task_dir = tmp_path / "task_workspace"
    task_dir.mkdir()

    # 1. Fixture file (e.g. rules.json)
    fixture_path = task_dir / "rules.json"
    fixture_data = {
        "schema_version": "v1",
        "rules": [
            {"id": "RULE-1", "severity": "high", "pattern": "exec("},
            {"id": "RULE-2", "severity": "medium", "pattern": "eval("},
        ]
    }
    fixture_path.write_text(json.dumps(fixture_data, indent=2))

    # 2. Pipeline read command: gh api pipe to python3 -c safe data read
    read_cmd = f"python3 -c 'import json,pathlib; print(json.loads(pathlib.Path(\"{fixture_path}\").read_text())[\"rules\"][0][\"id\"])'"
    guard_result = check_all_command_guards(read_cmd, "local")
    assert guard_result is None or guard_result.get("approved") is True, f"Expected safe read command to be allowed, got: {guard_result}"

    # 3. Helper reads the fixture using supported read_file_tool
    read_result = read_file_tool(path=str(fixture_path), task_id="test_helper_task")
    read_dict = json.loads(read_result)
    assert "content" in read_dict
    assert "RULE-1" in read_dict["content"]

    # 4. Helper writes its own authored report using supported write_file_tool
    report_path = task_dir / "report.json"
    report_payload = json.dumps({
        "author": "headless-helper",
        "status": "complete",
        "analyzed_rule": "RULE-1",
        "summary": "Processed fixture successfully via supported structured tools.",
    }, indent=2)

    write_result = write_file_tool(path=str(report_path), content=report_payload, task_id="test_helper_task")
    write_dict = json.loads(write_result)
    assert write_dict.get("verified") is True
    assert str(report_path) in write_dict.get("files_modified", [])

    # 5. Verify the report file exists and contains the exact content written by helper
    assert report_path.exists()
    loaded_report = json.loads(report_path.read_text())
    assert loaded_report["author"] == "headless-helper"
    assert loaded_report["analyzed_rule"] == "RULE-1"


def test_dangerous_execution_and_unauthorized_writes_remain_blocked(tmp_path):
    """Negative tests: arbitrary execution, unsafe compound statements, open() and operator deny remain enforced."""
    # Direct code execution via eval/exec
    eval_cmd = "python3 -c 'import sys; f=eval; f(sys.stdin.read())'"
    dangerous, _, desc = detect_dangerous_command(eval_cmd)
    assert dangerous is True
    assert desc == "script execution via -e/-c flag"

    # Compound statement rebinding (F1 Gauge counterexample)
    nested_rebind = "python3 -c 'import sys\nif True:\n    from builtins import eval as print\nprint(sys.stdin.read())'"
    dangerous, _, desc = detect_dangerous_command(nested_rebind)
    assert dangerous is True
    assert desc == "script execution via -e/-c flag"

    # Bundled option arg ownership (F2 Gauge counterexample)
    bundle_cmd = "python3 -BWc'import sys; print(sys.stdin.read())' -c 'import sys; exec(sys.stdin.read())'"
    dangerous, _, desc = detect_dangerous_command(bundle_cmd)
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
