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

