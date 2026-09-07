"""Command selectors distinguish shell execution from PR body data.

All command strings are inspected as data; no shell or GitHub operation runs.
"""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

import hermes_cli.config as config
from tools import approval
from tools.approval_context import _get_approval_config


@pytest.mark.parametrize("command,denied", [
    ('gh pr create --body "Do not execute gh pr merge 6; release needs approval."', False),
    ('gh pr review 6 --comment --body "Do not execute gh pr merge 6; release needs approval."', False),
    ('gh pr comment 6 --body "Do not execute gh pr merge 6; release needs approval."', False),
    ('gh api --method POST repos/owner/repo/pulls/6/reviews -f body="Policy: gh pr merge remains forbidden."', False),
    ('gh pr --repo owner/repo create --body "No merge authorization."', False),
    ('gh pr  create --body "No merge authorization."', False),
    ("echo 'gh pr merge 6'", False),
    ("echo '$(gh pr merge 6)'", False),
    ("echo '`gh pr merge 6`'", False),
    ('gh pr comment 6 --body "literal \\$(gh pr merge 6)"', False),
    ('gh pr comment 6 --body "line one\ngh pr merge 6"', False),
    ('gh pr create --body "bash -c gh pr merge 6"', False),
    ("bash -c 'echo \"gh pr merge 6\"'", False),
    ('gh pr comment 6 --body "$(printf gh) pr merge 6"', False),
    ('gh pr create --body "`printf safe`"', False),
    ('echo "`bash -c true`"', False),
    pytest.param("gh pr create --body-file - <<'BODY'\ngh pr merge 6\nBODY", False, id="ocr-inert-heredoc"),
    ("gh pr create --body-file - <<'BODY'\n$(gh pr merge 6)\nBODY", False),
    ('gh pr create --body "$(cat <<\'BODY\'\ngh pr merge 6\nBODY\n)"', False),
    ('echo "if gh pr merge 6; then :; fi"', False),
    ('gh pr create --body "2>/dev/null gh pr merge 6"', False),
    ('gh pr create --body-file - <<BODY\ngh pr merge 6\nBODY', False),
    ('gh --repo pr merge 6', False),
    ('gh pr view merge', False),
    ('other-gh pr merge 6', False),
    ('gh pr merger 6', False),
    ('gh pr merge 6', True),
    ('/usr/bin/gh pr merge 6', True),
    ('/usr/local/bin/gh --repo=owner/repo pr merge 6', True),
    ('gh -R owner/repo pr merge 6', True),
    ('gh -Rowner/repo pr merge 6', True),
    ('gh pr --repo owner/repo merge 6', True),
    ('gh pr -R owner/repo merge 6', True),
    ('gh pr --repo=owner/repo merge 6', True),
    ('gh\tpr\tmerge 6', True),
    ('gh  pr   merge 6', True),
    ('g""h pr m""erge 6', True),
    ("'gh' 'pr' 'merge' 6", True),
    ('gh p"r" me"rge" 6', True),
    ('g\\h pr m\\erge 6', True),
    ('gh \\\npr merge 6', True),
    ('env GH_CONFIG_DIR=/tmp/gh /usr/bin/gh pr merge 6', True),
    ('sudo -u build gh pr merge 6', True),
    ('true && gh pr merge 6', True),
    ('true; gh pr merge 6', True),
    ('true | gh pr merge 6', True),
    ('(gh pr merge 6)', True),
    ('{ gh pr merge 6; }', True),
    ('gh pr comment 6 --body "$(gh pr merge 6)"', True),
    ('gh pr comment 6 --body "`gh pr merge 6`"', True),
    ('echo $(gh pr merge 6)', True),
    ('echo "$(echo $(gh pr merge 6))"', True),
    ("bash -lc 'gh pr merge 6'", True),
    ("/bin/sh -c 'gh pr merge 6'", True),
    ("dash -c 'gh pr merge 6'", True),
    ("bash -O extglob -c 'gh pr merge 6'", True),
    ("eval 'gh pr merge 6'", True),
    ("eval gh pr merge 6", True),
    ("gh pr create --body 'unterminated", True),
    ("bash -c", True),
    ('echo "$(gh pr merge 6)', True),
    ('echo ' + 'x' * 20_000, True),
    pytest.param('if gh pr merge 6; then :; fi', True, id="ocr-control"),
    ('if true; then gh pr merge 6; fi', True),
    pytest.param('if ! gh pr merge 6; then :; fi', True, id="nested-control"),
    ('while true; do gh pr merge 6; done', True),
    pytest.param('gh pr 2>/dev/null merge 6', True, id="ocr-redirection"),
    ('2>/dev/null gh pr merge 6', True),
    ('gh pr > /tmp/result merge 6', True),
    ('gh pr create 2>"$(gh pr merge 6)"', True),
    ('gh pr create --body-file - <<BODY\n$(gh pr merge 6)\nBODY', True),
    ("bash <<'BODY'\ngh pr merge 6\nBODY", True),
    ("gh pr create --body-file - <<'BODY'\nplain text\nBODY\ngh pr merge 6", True),
])
@pytest.mark.parametrize("bypass", ["off", "yolo", "allowlist"])
def test_execution_not_data_is_denied(tmp_path, monkeypatch, command, denied, bypass):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "approvals": {"mode": "off" if bypass == "off" else "manual",
                      "deny_commands": ["gh pr merge"]},
        "security": {"tirith_enabled": False},
    }), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", bypass == "yolo")
    monkeypatch.setattr(approval, "_permanent_approved", {command} if bypass == "allowlist" else set())
    assert config.get_config_path() == path
    assert _get_approval_config()["deny_commands"] == ["gh pr merge"]
    # The selector floor is independent of ordinary manual approvals. Exercise
    # both public entry points for denied operations and off/yolo allowed data.
    from tools.approval_floors import _user_command_deny_block
    assert bool(_user_command_deny_block(command)) is denied
    if denied or bypass != "allowlist":
        for guard in (approval.check_dangerous_command, approval.check_all_command_guards):
            result = guard(command, "local")
            assert result["approved"] is not denied, result
            if denied:
                assert result.get("user_deny") or result.get("hardline"), result


@pytest.mark.parametrize("selectors,invalid", [
    ([], False), (["gh pr merge"], False), (None, True), ("gh pr merge", True),
    ({"gh pr merge": True}, True), ([123], True), (["gh pr *"], True),
    (["gh pr merge", "unknown"], True), ([" gh pr merge "], True),
])
def test_fresh_loader_reload_and_fail_closed_config(tmp_path, selectors, invalid):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"approvals": {"mode": "off", "deny_commands": selectors},
                                    "security": {"tirith_enabled": False}}), encoding="utf-8")
    probe = r'''
import json, os
from pathlib import Path
import yaml
from hermes_cli.config import get_config_path, load_config_readonly
from tools.approval import check_dangerous_command
path = Path(os.environ['HERMES_HOME']) / 'config.yaml'
assert get_config_path() == path
raw = yaml.safe_load(path.read_text())
assert load_config_readonly()['approvals']['deny_commands'] == raw['approvals']['deny_commands']
first = check_dangerous_command('echo safe', 'local')
merge = check_dangerous_command('gh pr merge 6', 'local')
old = path.stat().st_mtime_ns
# This is only a temporary home, never the running agent's policy.
raw['approvals']['deny_commands'] = ['gh pr merge']
raw['approvals']['deny'] = ['*retained-protection*']
path.write_text(yaml.safe_dump(raw))
os.utime(path, ns=(old + 2_000_000_000, old + 2_000_000_000))
assert load_config_readonly()['approvals']['deny_commands'] == ['gh pr merge']
assert not check_dangerous_command('gh pr merge 6', 'local')['approved']
assert check_dangerous_command('echo "gh pr merge 6"', 'local')['approved']
assert not check_dangerous_command('echo retained-protection', 'local')['approved']
print(json.dumps({'first': first, 'merge': merge}))
'''
    env = {**os.environ, "HERMES_HOME": str(tmp_path)}
    result = subprocess.run([sys.executable, "-c", probe], env=env,
                            cwd=Path(__file__).resolve().parents[2],
                            text=True, capture_output=True, check=True, timeout=30)
    data = json.loads(result.stdout)
    assert bool(data["first"].get("config_error")) is invalid
    assert data["first"]["approved"] is not invalid
    assert data["merge"]["approved"] is (not invalid and not selectors)
