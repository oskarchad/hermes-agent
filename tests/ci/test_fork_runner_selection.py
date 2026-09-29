"""Fork CI must select available runners without changing upstream selection."""

import ast
from pathlib import Path
import re

import yaml


ROOT = Path(__file__).resolve().parents[2]
FORK = "oskarchad/hermes-agent"
UPSTREAM = "NousResearch/hermes-agent"


def repository_value(value, repository):
    """Resolve the conditional-value subset used by these workflow settings."""
    if not isinstance(value, str) or not value.startswith("${{"):
        return value
    match = re.fullmatch(
        r"\$\{\{ github.repository == ('[^']+') && (.+) \|\| (.+) \}\}", value
    )
    assert match, f"Unsupported repository selector: {value}"
    owner, yes, no = map(ast.literal_eval, match.groups())
    return (yes if repository.casefold() == owner.casefold() else False) or no


def workflow(name):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text())


def test_active_ci_jobs_resolve_to_standard_runners_on_fork():
    caller = workflow("ci.yaml")
    checked = []
    for call in caller["jobs"].values():
        target = call.get("uses", "")
        if not target.startswith("./.github/workflows/") or call.get("if") is False:
            continue
        for name, job in workflow(Path(target).name)["jobs"].items():
            runner = job.get("runs-on", "")
            if "-core" not in runner:
                continue  # Matrix runners are outside this direct-selector contract.
            assert repository_value(runner, FORK) == "ubuntu-latest", (target, name)
            assert repository_value(runner, UPSTREAM).endswith("-core"), (target, name)
            assert repository_value(runner, "unrelated/hermes-agent") == repository_value(runner, UPSTREAM)
            checked.append((target, name))
    assert ("./.github/workflows/tests.yml", "e2e") in checked
    assert ("./.github/workflows/tests.yml", "e2e-upgrade") in checked
    assert ("./.github/workflows/e2e-desktop-core.yml", "core") in checked


def test_python_e2e_fork_concurrency_is_bounded_without_overriding_upstream():
    for name in ("e2e", "e2e-upgrade"):
        job = workflow("tests.yml")["jobs"][name]
        step = next(s for s in job["steps"] if "scripts/run_tests.sh" in s.get("run", ""))
        env = {**job.get("env", {}), **step.get("env", {})}
        workers = env.get("HERMES_TEST_WORKERS", "")
        assert repository_value(workers, FORK) in (1, 2, 3, 4), name
        assert repository_value(workers, UPSTREAM) == "", name
