"""Kanban transport clients must import their installation, not the task workspace."""
import os
from pathlib import Path
import subprocess

import pytest

from cron.scheduler_worker_env import pin_hermes_tree_on_pythonpath
from hermes_cli import kanban_db_dispatch


@pytest.mark.parametrize("shadow_package", [False, True])
def test_worker_boot_reaches_claim_validation_from_any_workspace(tmp_path, shadow_package):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    if shadow_package:
        package = workspace / "hermes_cli"
        package.mkdir()
        (package / "__init__.py").write_text(
            "print('WORKSPACE_PACKAGE_IMPORTED'); raise SystemExit(73)\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("HERMES_KANBAN_") and k != "PYTHONSAFEPATH"}
    env.update(HOME=str(home), HERMES_HOME=str(home / ".hermes"),
               HTTPS_PROXY="http://127.0.0.1:9", https_proxy="http://127.0.0.1:9",
               NO_PROXY="", no_proxy="")
    env = pin_hermes_tree_on_pythonpath(
        env, Path(kanban_db_dispatch.__file__).resolve().parent.parent)
    command = kanban_db_dispatch._worker_argv(None, "default", env["HERMES_HOME"])
    result = subprocess.run(command, cwd=workspace, env=env, capture_output=True,
                            text=True, timeout=30, check=False)
    assert "WORKSPACE_PACKAGE_IMPORTED" not in result.stdout
    # A deliberate absent claim stops before connecting to an owner or executing work.
    assert result.returncode == 1
    assert "Kanban owner refused: 'HERMES_KANBAN_BOARD'" in result.stderr
