"""Execute the attribution workflow against isolated stacked Git history."""

import os
from pathlib import Path
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]


def git(repo, *args):
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


@pytest.mark.linux_only
def test_attribution_checks_only_event_range_and_fails_closed(tmp_path):
    workflow = yaml.safe_load((ROOT / ".github/workflows/contributor-check.yml").read_text())
    step = next(s for s in workflow["jobs"]["check-attribution"]["steps"] if s.get("id") == "check-emails")
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.name", "Fixture")
    git(tmp_path, "config", "user.email", "mapped@example.invalid")
    (tmp_path / "contributors/emails").mkdir(parents=True)
    (tmp_path / "contributors/emails/mapped@example.invalid").write_text("fixture\n")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/release.py").write_text("AUTHOR_MAP = {}\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "root")
    git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(tmp_path, "-c", "user.email=imported@example.invalid", "commit", "--allow-empty", "-m", "overlay")
    overlay = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "commit", "--allow-empty", "-m", "mapped PR")

    def check(event, pr_base="", before=""):
        values = {
            "${{ github.event.pull_request.base.sha }}": pr_base,
            "${{ github.event.before }}": before,
        }
        env = {**os.environ, "GITHUB_EVENT_NAME": event, "GITHUB_OUTPUT": str(tmp_path / "output")}
        env.update({key: values[value] for key, value in step.get("env", {}).items()})
        return subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail"],
                              input=step["run"], cwd=tmp_path, env=env,
                              text=True, capture_output=True)

    result = check("pull_request", overlay)
    assert result.returncode == 0, result.stdout + result.stderr
    assert check("push", before=overlay).returncode == 0
    for event, pr_base, before in [
        ("pull_request", "", overlay),
        ("pull_request", "0" * 40, ""),
        ("pull_request", "$(touch injected)", ""),
        ("push", "", ""),
        ("workflow_dispatch", overlay, overlay),
    ]:
        assert check(event, pr_base, before).returncode != 0
    assert not (tmp_path / "injected").exists()
    git(tmp_path, "-c", "user.email=unknown@example.invalid", "commit", "--allow-empty", "-m", "unknown PR author")
    for event in ("pull_request", "push"):
        result = check(event, overlay, overlay)
        assert result.returncode != 0
        assert "unknown@example.invalid" in result.stdout
        assert "imported@example.invalid" not in result.stdout


def test_fork_budget_covers_observed_throughput_without_changing_upstream():
    workflow = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())
    budget = workflow["jobs"]["test"]["timeout-minutes"]
    # Decode the existing GitHub conditional-value idiom, not workflow source text.
    if isinstance(budget, int):
        fork = upstream = budget
    else:
        condition, choices = budget.removeprefix("${{").removesuffix("}}").strip().split(" && ")
        assert condition == "github.repository == 'oskarchad/hermes-agent'"
        fork, upstream = map(int, choices.split(" || "))
    assert upstream == 30
    assert 30 / 0.497 < fork <= 90
