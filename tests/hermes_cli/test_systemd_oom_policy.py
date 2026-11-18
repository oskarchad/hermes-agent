"""The generated gateway unit survives an OOM kill of one of its children.

Agent turns (managed workers) and their foreground terminal commands run inside the
gateway's service cgroup. With systemd's default ``OOMPolicy=stop`` a kernel OOM kill of
any one of them (an ffmpeg render, a test run) stops the whole unit: Discord, Kanban and
cron go down together. ``OOMPolicy=continue`` keeps the unit running; the killed worker is
reported lost and its turn/card is handled like any other crashed worker.
"""

from pathlib import Path

from hermes_cli import gateway as gateway_cli


def _service_section(unit: str) -> str:
    return unit.split("[Service]", 1)[1].split("[Install]", 1)[0]


def test_user_and_system_units_continue_after_child_oom_kill(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(
        gateway_cli, "_system_service_identity",
        lambda run_as_user=None: ("alice", "alice", str(tmp_path), 1001),
    )
    monkeypatch.setattr(gateway_cli, "_build_service_path_dirs", list)

    for unit in (gateway_cli.generate_systemd_unit(system=False),
                 gateway_cli.generate_systemd_unit(system=True, run_as_user="alice")):
        assert "OOMPolicy=continue" in _service_section(unit).splitlines()
