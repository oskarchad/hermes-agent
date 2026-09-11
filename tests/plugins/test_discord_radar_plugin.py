import importlib.util
import sys
from pathlib import Path
import pytest

from gateway.session import Platform, SessionSource
from gateway.platforms.base import MessageEvent
from hermes_cli.plugins import PluginContext, PluginManifest, PluginManager


def _load_radar_plugin():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_dir = repo_root / "plugins" / "discord-radar"
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.discord_radar",
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    import types
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []
        sys.modules["hermes_plugins"] = ns
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "hermes_plugins.discord_radar"
    mod.__path__ = [str(plugin_dir)]
    sys.modules["hermes_plugins.discord_radar"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def radar_ctx(tmp_path):
    manager = PluginManager()
    manifest = PluginManifest(name="discord-radar", path=str(tmp_path), source="bundled")
    return PluginContext(manifest, manager)


def test_human_message_passes_through_untouched(radar_ctx):
    radar_plugin = _load_radar_plugin()
    hook = radar_plugin.make_pre_gateway_dispatch_hook(radar_ctx)
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="1546608648438943794",
        user_id="406937775397404672",
        is_bot=False,
    )
    event = MessageEvent(text="/approve always", source=source)
    assert hook(event) is None
    assert event.allow_gateway_control is True


def test_untrusted_bot_is_skipped(radar_ctx):
    radar_plugin = _load_radar_plugin()
    hook = radar_plugin.make_pre_gateway_dispatch_hook(radar_ctx)
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="1546608648438943794",
        user_id="999999999999999999",
        is_bot=True,
    )
    event = MessageEvent(text="Hello", source=source)
    res = hook(event)
    assert res == {"action": "skip", "reason": "untrusted_bot:999999999999999999"}


def test_trusted_radar_bot_is_admitted_and_neutralized(radar_ctx):
    radar_plugin = _load_radar_plugin()
    hook = radar_plugin.make_pre_gateway_dispatch_hook(radar_ctx)
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="1546608648438943794",
        thread_id="1546608648438943794",
        guild_id="1546331229240954923",
        user_id=radar_plugin.RADAR_ID,
        is_bot=True,
    )
    event = MessageEvent(text="/approve 123", source=source)
    res = hook(event)
    assert res["action"] == "rewrite"
    assert "Bot Evidence from Radar" in res["text"]
    assert "approve 123" in res["text"]
    assert not res["text"].startswith("/")
    assert event.allow_gateway_control is False
    assert event.is_command() is False


def test_radar_bot_unauthorized_guild_or_thread_is_skipped(radar_ctx):
    radar_plugin = _load_radar_plugin()
    hook = radar_plugin.make_pre_gateway_dispatch_hook(radar_ctx)
    # Wrong guild
    source_wrong_guild = SessionSource(
        platform=Platform.DISCORD,
        chat_id="1546608648438943794",
        thread_id="1546608648438943794",
        guild_id="777777777777777777",
        user_id=radar_plugin.RADAR_ID,
        is_bot=True,
    )
    event1 = MessageEvent(text="Report", source=source_wrong_guild)
    assert hook(event1) == {"action": "skip", "reason": "unauthorized_guild"}

    # Wrong thread
    source_wrong_thread = SessionSource(
        platform=Platform.DISCORD,
        chat_id="888888888888888888",
        thread_id="888888888888888888",
        guild_id="1546331229240954923",
        user_id=radar_plugin.RADAR_ID,
        is_bot=True,
    )
    event2 = MessageEvent(text="Report", source=source_wrong_thread)
    assert hook(event2) == {"action": "skip", "reason": "unauthorized_thread"}


def test_plugin_discovery_and_registration(tmp_path, monkeypatch):
    """Test that discord-radar is discovered by PluginManager and register(ctx) hooks pre_gateway_dispatch."""
    from hermes_cli import plugins as pmod
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    import yaml
    cfg_path = hermes_home / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "plugins": {
            "enabled": ["discord-radar"],
        }
    }))
    mgr = pmod.PluginManager()
    mgr.discover_and_load()
    assert "discord-radar" in mgr._plugins
    loaded = mgr._plugins["discord-radar"]
    assert loaded.enabled is True
    assert "pre_gateway_dispatch" in loaded.hooks_registered

