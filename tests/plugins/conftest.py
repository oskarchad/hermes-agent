import importlib.util
import sys
import types
from pathlib import Path


def load_discord_radar_module():
    """Import plugins/discord-radar using Hermes plugin loader namespace convention."""
    mod_name = "hermes_plugins.discord_radar"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    repo_root = Path(__file__).resolve().parents[2]
    plugin_dir = repo_root / "plugins" / "discord-radar"
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []
        sys.modules["hermes_plugins"] = ns
    else:
        ns = sys.modules["hermes_plugins"]

    # First load observer submodule
    obs_name = f"{mod_name}.observer"
    obs_file = plugin_dir / "observer.py"
    obs_spec = importlib.util.spec_from_file_location(
        obs_name, obs_file
    )
    assert obs_spec is not None and obs_spec.loader is not None
    obs_mod = importlib.util.module_from_spec(obs_spec)
    obs_mod.__package__ = mod_name
    sys.modules[obs_name] = obs_mod
    obs_spec.loader.exec_module(obs_mod)

    # Next load hook submodule
    hook_name = f"{mod_name}.hook"
    hook_file = plugin_dir / "hook.py"
    hook_spec = importlib.util.spec_from_file_location(
        hook_name, hook_file
    )
    assert hook_spec is not None and hook_spec.loader is not None
    hook_mod = importlib.util.module_from_spec(hook_spec)
    hook_mod.__package__ = mod_name
    sys.modules[hook_name] = hook_mod
    hook_spec.loader.exec_module(hook_mod)

    # Now load __init__.py
    init_file = plugin_dir / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        mod_name, init_file, submodule_search_locations=[str(plugin_dir)]
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = mod_name
    mod.__path__ = [str(plugin_dir)]
    setattr(mod, "observer", obs_mod)
    setattr(mod, "hook", hook_mod)
    sys.modules[mod_name] = mod
    setattr(ns, "discord_radar", mod)
    spec.loader.exec_module(mod)
    return mod
