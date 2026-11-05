"""The detached /restart child runs the module-form Hermes cmd from any cwd.

``_resolve_hermes_bin`` returns ``python -P -m hermes_cli.main`` (``-P``: a ``hermes_cli/`` in the
cwd must not shadow the install), and the watcher env strips Hermes-owned PYTHONPATH entries, so
the watcher env has to pin the tree back or the respawn cannot import ``hermes_cli``.
"""

import subprocess

import pytest

from gateway.run import _resolve_hermes_bin
from gateway.run_shutdown import GatewayShutdownMixin


@pytest.mark.platforms("posix")  # the Windows watcher path is covered by its own marked test
def test_restart_cmd_with_watcher_env_starts_outside_the_checkout(tmp_path):
    cmd = _resolve_hermes_bin()
    env = GatewayShutdownMixin._restart_watcher_env(cmd)
    r = subprocess.run(cmd + ["--version"], cwd=tmp_path, env=env,
                       capture_output=True, text=True, timeout=60, check=False)

    assert r.returncode == 0, r.stderr[-500:]
