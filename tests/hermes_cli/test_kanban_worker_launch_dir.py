"""A module-form Kanban worker runs the install's hermes_cli, never a same-named package in its workspace."""

import os
import subprocess

import pytest

# A worker workspace that is a worktree of hermes-agent itself carries its own ``hermes_cli``.
SHADOW_INIT = '''
from pathlib import Path
Path(__file__).resolve().parents[1].joinpath("shadow-imported.txt").write_text("imported")
'''


@pytest.mark.platforms("any")  # a real child interpreter: sys.path rules are per-OS
def test_module_worker_ignores_a_hermes_cli_package_in_the_workspace(tmp_path, monkeypatch):
    from hermes_cli import kanban_db_dispatch as kbd

    workspace = tmp_path / "hermes-agent-worktree"
    (workspace / "hermes_cli").mkdir(parents=True)
    (workspace / "hermes_cli" / "__init__.py").write_text(SHADOW_INIT, encoding="utf-8")
    (workspace / "hermes_cli" / "main.py").write_text("raise SystemExit(7)\n", encoding="utf-8")
    monkeypatch.delenv("HERMES_BIN", raising=False)

    argv = kbd._resolve_hermes_argv()
    env = dict(os.environ)
    kbd._propagate_module_import_root(argv, env)
    r = subprocess.run(argv + ["--version"], cwd=workspace, env=env,
                       capture_output=True, text=True, timeout=60, check=False)

    assert not (workspace / "shadow-imported.txt").exists()
    assert r.returncode == 0, r.stderr[-500:]
