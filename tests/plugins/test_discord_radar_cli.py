"""Failing tests for discord-radar CLI and runnable entrypoint caller."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from hermes_cli.plugins import PluginContext, PluginManifest, PluginManager
from tests.plugins.conftest import load_discord_radar_module


def test_plugin_registers_cli_command(tmp_path):
    radar_mod = load_discord_radar_module()
    mgr = PluginManager()
    manifest = PluginManifest(name="discord-radar", path=str(tmp_path), source="bundled")
    ctx = PluginContext(manifest, mgr)

    radar_mod.register(ctx)

    assert "radar-observer" in mgr._cli_commands
    entry = mgr._cli_commands["radar-observer"]
    assert entry["plugin"] == "discord-radar"
    assert callable(entry["setup_fn"])
    assert callable(entry["handler_fn"])


def test_cli_command_handler_invokes_hook(tmp_path, monkeypatch):
    radar_mod = load_discord_radar_module()
    entrypoint_mod = getattr(radar_mod, "cli", None)
    assert entrypoint_mod is not None, "plugins.discord-radar.cli must be exposed"

    state_file = tmp_path / "discord-now-state.json"
    state_file.write_text(json.dumps({"evidence": {}}))
    report_file = tmp_path / "report.json"

    called_kwargs = {}

    def fake_hook(state_file_path, output_report_path=None, enabled=True, dry_run=False, **kwargs):
        called_kwargs["state_file_path"] = state_file_path
        called_kwargs["output_report_path"] = output_report_path
        called_kwargs["enabled"] = enabled
        called_kwargs["dry_run"] = dry_run
        return radar_mod.ObserverReport()

    monkeypatch.setattr(entrypoint_mod, "run_discord_blocker_hook", fake_hook)

    args = argparse.Namespace(
        state_path=str(state_file),
        report_path=str(report_file),
        dry_run=True,
        disabled=False,
        profile="otto",
    )
    res = entrypoint_mod.radar_observer_command(args)
    assert res == 0
    assert called_kwargs["state_file_path"] == str(state_file)
    assert called_kwargs["output_report_path"] == str(report_file)
    assert called_kwargs["dry_run"] is True
    assert called_kwargs["enabled"] is True


def test_cli_dispatches_cleanly_when_disabled(tmp_path, monkeypatch):
    radar_mod = load_discord_radar_module()
    entrypoint_mod = radar_mod.cli

    called = []
    def fake_hook(*args, **kwargs):
        called.append(kwargs)
        return radar_mod.ObserverReport()

    monkeypatch.setattr(entrypoint_mod, "run_discord_blocker_hook", fake_hook)

    args = argparse.Namespace(
        state_path=str(tmp_path / "state.json"),
        report_path=None,
        dry_run=False,
        disabled=True,
        profile=None,
    )
    res = entrypoint_mod.radar_observer_command(args)
    assert res == 0
    assert len(called) == 1
    assert called[0]["enabled"] is False


@pytest.mark.linux_only
def test_no_agent_cron_script_executes_radar_observer(tmp_path, monkeypatch):
    """Test full cycle: cron create -> stored job -> _run_no_agent_job -> checked-in wrapper -> radar-observer execution."""
    import os
    import sys

    home = tmp_path / "profiles" / "otto"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    repo_root = Path(__file__).resolve().parents[2]
    probe = tmp_path / "probe"
    probe.mkdir()
    # Instrument only the network edge; the CLI, plugin loader, hook and disk writes are real.
    (probe / "sitecustomize.py").write_text(
        "import sys, json, os\n"
        "from pathlib import Path\n"
        "from tools import discord_tool\n"
        "def request(method, endpoint, token, **kwargs):\n"
        "    assert method == 'GET'\n"
        "    if endpoint == '/users/@me':\n"
        "        return {'id': '1547333242502520872', 'bot': True}\n"
        "    assert endpoint == '/channels/123/messages/456'\n"
        "    return {'id': '456', 'channel_id': '123', 'author': {'id': '789'}, 'content': 'No blocked items'}\n"
        "discord_tool._discord_request = request\n"
        "def trace(frame, event, arg):\n"
        "    if frame.f_code.co_name == 'run_discord_blocker_hook' and event == 'return':\n"
        "        Path(os.environ['HERMES_HOME'], 'hook-call.json').write_text(json.dumps({'state_path': str(frame.f_locals['state_file_path']), 'errors': arg.errors}))\n"
        "sys.setprofile(trace)\n"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Standard installed console entrypoint, bound to this test interpreter.
    console = bin_dir / "hermes"
    console.write_text(f"#!{sys.executable}\nfrom hermes_cli.main import main\nmain()\n")
    console.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(probe), str(repo_root)]))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(repo_root / "plugins"))
    (home / "config.yaml").write_text("plugins:\n  enabled: [discord-radar]\n")
    (home / ".env").write_text("DISCORD_OBSERVER_BOT_TOKEN=fixture-only-not-a-credential\n")
    scripts_dir = home / "scripts"
    scripts_dir.mkdir()
    cron_dir = home / "cron"
    cron_dir.mkdir()
    state_path = cron_dir / "discord-now-state.json"
    state_path.write_text(json.dumps({"channel_id": "123", "message_id": "456", "bot_id": "789"}))

    monkeypatch.setenv("HERMES_HOME", str(home))

    import importlib
    import hermes_constants
    importlib.reload(hermes_constants)
    import cron.jobs
    importlib.reload(cron.jobs)
    import cron.scheduler
    importlib.reload(cron.scheduler)
    import cron.scheduler_script
    importlib.reload(cron.scheduler_script)

    from hermes_cli.subcommands.cron import build_cron_parser
    parser = argparse.ArgumentParser(prog="hermes")
    subparsers = parser.add_subparsers(dest="subcommand")
    build_cron_parser(subparsers, cmd_cron=lambda args: 0)

    # 1. Verify parser accepts positional schedule grammar
    args = parser.parse_args([
        "cron", "create", "every 10m",
        "--name", "Discord Radar · Blocker Observer co 10 min",
        "--deliver", "local",
        "--no-agent",
        "--script", "radar-observer.sh",
    ])
    assert args.cron_command == "create"
    assert args.schedule == "every 10m"
    assert args.no_agent is True
    assert args.script == "radar-observer.sh"

    # 2. Copy the real checked-in wrapper script
    repo_root = Path(__file__).resolve().parent.parent.parent
    checked_in_wrapper = repo_root / "plugins" / "discord-radar" / "radar-observer.sh"
    assert checked_in_wrapper.exists(), f"Checked-in wrapper must exist at {checked_in_wrapper}"

    radar_script = scripts_dir / "radar-observer.sh"
    radar_script.write_bytes(checked_in_wrapper.read_bytes())
    radar_script.chmod(0o755)

    # 3. Store job and execute via _run_no_agent_job
    job = cron.jobs.create_job(
        schedule="every 10m",
        prompt=None,
        name="Discord Radar · Blocker Observer co 10 min",
        deliver="local",
        no_agent=True,
        script="radar-observer.sh",
    )
    assert job["id"] is not None
    assert job["no_agent"] is True
    assert job["script"] == "radar-observer.sh"

    cancel_event = MagicMock()
    cancel_event.is_set.return_value = False

    success, full_output, deliver_output, err = cron.scheduler._run_no_agent_job(
        job, job["id"], job["name"], cancel_event
    )

    assert success is True, full_output
    assert err is None
    assert "[discord-radar] Success:" in deliver_output
    assert json.loads((home / "hook-call.json").read_text()) == {
        "state_path": str(state_path), "errors": [],
    }
    assert json.loads((cron_dir / "discord-blocker-observer-state.json").read_text()) == {}

