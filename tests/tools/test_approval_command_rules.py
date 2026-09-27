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
    pytest.param('case x in x) gh pr merge 6;; esac', True, id="gauge-case-arm"),
    ('case x in x) echo "gh pr merge 6";; esac', False),
    ('gh pr create --body "case x in x) gh pr merge 6;; esac"', False),
    pytest.param('command -- gh pr merge 6', True, id="gauge-command-terminator"),
    ('command -- gh pr create --body "gh pr merge 6"', False),
    ('command -v gh pr merge', False),
    pytest.param('env ' * 13 + 'gh pr merge 6', True, id="gauge-wrapper-limit"),
    ('echo "' + 'env ' * 13 + 'gh pr merge 6"', False),
    pytest.param("bash <<< 'gh pr merge 6'", True, id="gauge-shell-here-string"),
    ("<<< 'gh pr merge 6' bash", True),
    ("bash -s -- arg <<< 'gh pr merge 6'", True),
    ("bash -- <<< 'gh pr merge 6'", True),
    ("bash 0<<< 'gh pr merge 6'", True),
    ("cat <<< 'gh pr merge 6'", False),
    ("bash -c 'cat' <<< 'gh pr merge 6'", False),
    ("bash <<< 'gh pr merge 6' -c 'cat'", False),
    ("bash script.sh <<< 'gh pr merge 6'", False),
    ("bash 3<<< 'gh pr merge 6'", False),
    pytest.param("bash -c 'gh pr create --body-file -' <<'BODY'\ngh pr merge 6\nBODY", False,
                 id="gauge-shell-c-heredoc-data"),
    ("bash -c 'gh pr create --body-file -' <<'BODY'\nDo not execute gh pr merge 6; release needs approval.\nBODY", False),
    ("bash script.sh <<'BODY'\ngh pr merge 6\nBODY", False),
    ("bash <<'BODY' -c 'cat'\ngh pr merge 6\nBODY", False),
    ("bash -s -- arg <<'BODY'\ngh pr merge 6\nBODY", True),
    ("bash <<'BODY'; cat\ngh pr merge 6\nBODY", True),
    ("cat <<'BODY'; bash\ngh pr merge 6\nBODY", False),
    ("bash -c 'cat' <<BODY\n'$(gh pr merge 6)'\nBODY", True),
    ("cat <<< \"$(gh pr merge 6)\"", True),
    pytest.param("eval -- 'gh pr merge 6'", True, id="gauge-eval-terminator"),
    ("eval -- 'echo \"gh pr merge 6\"'", False),
    ('gh pr create --body "eval -- gh pr merge 6"', False),
    pytest.param("gh $'pr' $'merge' 6", True, id="gauge-ansi-literal"),
    ("$'gh' p$'r' m$'erge' 6", True),
    (r"gh $'p\x72' merge 6", True),  # Unsupported executable quoting fails closed.
    ("gh pr create --body $'gh pr merge 6'", False),
    ("echo $'gh pr merge 6'", False),
    ("gh pr create --body $'$(gh pr merge 6)'", False),
    pytest.param("bash 3<<'BODY' <&3\ngh pr merge 6\nBODY", True, id="closure-fd-heredoc"),
    pytest.param("bash 3<<< 'gh pr merge 6' <&3", True, id="closure-fd-herestring"),
    pytest.param("bash 3<<'BODY'\ngh pr merge 6\nBODY", False, id="closure-unused-fd"),
    pytest.param("bash -c 'gh pr create --body-file -' 3<<'BODY' <&3\ngh pr merge 6\nBODY", False, id="closure-fd-pr-data"),
    pytest.param(r"gh pr create --body $'Summary\nDo not execute gh pr merge 6.'", False, id="closure-ansi-body"),
    pytest.param(r"cat <<< $'Summary\ngh pr merge 6'", False, id="closure-ansi-input-data"),
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
    if denied or bypass != "allowlist":
        for guard in (approval.check_dangerous_command, approval.check_all_command_guards):
            result = guard(command, "local")
            assert result["approved"] is not denied, result
            if denied:
                assert result.get("user_deny") or result.get("hardline"), result
    assert bool(_user_command_deny_block(command)) is denied


@pytest.mark.parametrize("selectors,invalid", [
    ([], False), (["gh pr merge"], False), (None, True), ("gh pr merge", True),
    ({"gh pr merge": True}, True), ([123], True), (["gh pr *"], True),
    (["gh pr merge", "unknown"], True), ([" gh pr merge "], True),
    (["git push main"], False), (["gh pr merge", "git push main"], False),
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
from tools.approval import check_dangerous_command, check_all_command_guards
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
for command, denied in [
    ('case x in x) gh pr merge 6;; esac', True),
    ('command -- gh pr merge 6', True),
    ('env ' * 13 + 'gh pr merge 6', True),
    ("bash <<< 'gh pr merge 6'", True),
    ("bash -c 'gh pr create --body-file -' <<'BODY'\ngh pr merge 6\nBODY", False),
    ("eval -- 'gh pr merge 6'", True),
    ("gh $'pr' $'merge' 6", True),
    ("gh pr create --body $'gh pr merge 6'", False),
]:
    result = check_all_command_guards(command, 'local')
    assert result['approved'] is not denied, (command, result)
raw['approvals']['deny_commands'] = ['gh pr merge', 'git push main']
path.write_text(yaml.safe_dump(raw))
os.utime(path, ns=(old + 4_000_000_000, old + 4_000_000_000))
assert load_config_readonly()['approvals']['deny_commands'] == raw['approvals']['deny_commands']
for command, denied in [
    ("bash 3<<'BODY' <&3\ngh pr merge 6\nBODY", True),
    ("bash 3<<< 'gh pr merge 6' <&3", True),
    ("bash 3<<'BODY'\ngh pr merge 6\nBODY", False),
    ("bash -c 'gh pr create --body-file -' 3<<'BODY' <&3\ngh pr merge 6\nBODY", False),
    (r"gh pr create --body $'Summary\nDo not execute gh pr merge 6.'", False),
    ('git push origin HEAD:refs/heads/main --tags && git status', True),
    ('git push origin main:feature && gh pr create --base main', False),
]:
    result = check_all_command_guards(command, 'local')
    assert result['approved'] is not denied, (command, result)
print(json.dumps({'first': first, 'merge': merge}))
'''
    env = {**os.environ, "HERMES_HOME": str(tmp_path)}
    result = subprocess.run([sys.executable, "-c", probe], env=env,
                            cwd=Path(__file__).resolve().parents[2],
                            text=True, capture_output=True, check=True, timeout=30)
    data = json.loads(result.stdout)
    assert bool(data["first"].get("config_error")) is invalid
    assert data["first"]["approved"] is not invalid
    assert data["merge"]["approved"] is (not invalid and "gh pr merge" not in selectors)


@pytest.mark.parametrize("command,denied", [
    ('git push origin main --tags && git status', True),
    ('git push origin HEAD:refs/heads/main', True),
    ('git push origin +main', True),
    ('git push origin :main', True),
    ('git push --delete origin refs/heads/main', True),
    ('git push -d origin main', True),
    ('/usr/bin/git -C /workspace -c advice.pushUpdateRejected=false push origin main', True),
    ('git --git-dir /tmp/repo --work-tree=/tmp/tree push origin main', True),
    ('env X=1 command -- git push origin feature HEAD:main --porcelain', True),
    ('case x in x) git push origin main;; esac', True),
    ("bash -c 'git push origin HEAD:main'", True),
    ('echo "$(git push origin main)"', True),
    ('git push --repo origin main', False),  # Positional repository, no explicit ref.
    ('git push --repo=origin +HEAD:main', False),
    ('git push origin -- main', True),
    ('git push origin main:feature && gh pr create --base main', False),
    ('git push origin feature && gh pr create --body "git push origin main --tags"', False),
    (r"git push origin feature && gh pr create --body $'Summary\ngit push origin main'", False),
    ('git push origin main-feature', False),
    ('git push main feature', False),
    ('git push --push-option main origin feature', False),
    ('git push -vo main origin feature', False),
    ('git push --receive-pack main --repo origin feature', False),
    ('git push --force-with-lease=main origin feature', False),
    ('git push origin tag main', False),
    ('git -c alias.example="push origin main" status', False),
    ('git --version', False),
    ('git push --help', False),
    ("echo 'git push origin main'", False),
    ('gh pr merge 6', False),  # The git selector does not enable the gh selector.
    ('git push --push-option', True),  # Malformed inspection fails closed.
    (r"git push origin $'m\x61in'", True),
])
@pytest.mark.parametrize("bypass", ["off", "yolo", "allowlist"])
def test_git_destination_not_other_command_or_data(tmp_path, monkeypatch, command, denied, bypass):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "approvals": {"mode": "off" if bypass == "off" else "manual",
                      "deny_commands": ["git push main"]},
        "security": {"tirith_enabled": False},
    }), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", bypass == "yolo")
    monkeypatch.setattr(approval, "_permanent_approved", {command} if bypass == "allowlist" else set())
    result = approval.check_all_command_guards(command, "local")
    assert result["approved"] is not denied, result
    if denied:
        assert result.get("user_deny") or result.get("hardline"), result


@pytest.mark.parametrize("operands,denied", [
    ("tag release:main", True),
    ("tag release:refs/heads/main", True),
    ("tag main", True),  # `tag` is the repository here.
    ("origin tag release:main", True),
    ("origin tag release:refs/heads/main", True),
    ("origin tag main", False),
    ("main feature", False),
    ("origin main", True),
    ("origin HEAD:main", True),
    ("--delete origin main", True),
    ("--delete origin tag main", False),
])
def test_git_positional_repository_overrides_repo_option(tmp_path, operands, denied):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "approvals": {"mode": "off", "deny_commands": ["git push main"]},
        "security": {"tirith_enabled": False},
    }), encoding="utf-8")
    # A fresh import exercises the actual config loader and public guard. The
    # strings are data only: subprocess runs Python, never the sampled commands.
    probe = r'''
import json, os, sys
from pathlib import Path
from hermes_cli.config import get_config_path, load_config_readonly
from tools.approval import check_all_command_guards
assert get_config_path() == Path(os.environ['HERMES_HOME']) / 'config.yaml'
assert load_config_readonly()['approvals']['deny_commands'] == ['git push main']
operands, denied = json.loads(sys.argv[1])
for repo_option in ('', '--repo=origin ', '--repo origin '):
    command = 'git push ' + repo_option + operands
    result = check_all_command_guards(command, 'local')
    assert result['approved'] is not denied, (command, result)
    if denied:
        assert result.get('user_deny'), (command, result)
'''
    subprocess.run([sys.executable, "-c", probe, json.dumps([operands, denied])],
                   env={**os.environ, "HERMES_HOME": str(tmp_path)},
                   cwd=Path(__file__).resolve().parents[2], check=True, timeout=30)


@pytest.mark.parametrize("shorthand,normalized,denied", [
    ("tag release:main", "refs/tags/release:main", True),
    ("tag release:refs/heads/main", "refs/tags/release:refs/heads/main", True),
    ("tag main", "refs/tags/main", False),
    ("--delete tag main", ":refs/tags/main", False),
    ("--delete main", ":main", True),
    ("--delete refs/heads/main", ":refs/heads/main", True),
])
def test_git_tag_shorthand_matches_normalized_destination(tmp_path, monkeypatch, shorthand, normalized, denied):
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "approvals": {"mode": "off", "deny_commands": ["git push main"]},
        "security": {"tirith_enabled": False},
    }), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for refspec in (shorthand, normalized):
        result = approval.check_all_command_guards("git push origin " + refspec, "local")
        assert result["approved"] is not denied, result
        if denied:
            assert result.get("user_deny"), result
