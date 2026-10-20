"""Unavailable native tools must not bootstrap a worker for every consumer call."""
import importlib

import pytest

from pm import client, paths
from pm.package import InstallError
from pm.registry import get_package
from pm.store import current_target


@pytest.fixture(autouse=True)
def isolated_tool_store(tmp_path, monkeypatch):
    store = tmp_path / "tools"
    monkeypatch.setattr(paths, "store_root", lambda: store)
    monkeypatch.setattr(paths, "writable_store_root", lambda: store)
    monkeypatch.setattr(paths, "facts_path", lambda: store / "facts.json")


@pytest.mark.platforms("linux", "macos")
def test_system_git_selection_rejects_acquisition_without_worker(monkeypatch):
    engine = importlib.import_module("pm.install")
    monkeypatch.setattr(client, "is_runtime", lambda: False)
    monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)
    target = current_target()
    reason = get_package("git").missing_reason(target)
    assert reason is not None
    with pytest.raises(InstallError) as direct:
        engine.ensure("git")

    def no_worker(*args, **kwargs):
        pytest.fail("unavailable Git acquired an isolated PM worker")

    monkeypatch.setattr(client, "_request", no_worker)
    for _ in range(3):
        with pytest.raises(InstallError) as remote:
            client.ensure("git")
        assert (remote.value.package, remote.value.cause, remote.value.remedy) == (
            direct.value.package, direct.value.cause, direct.value.remedy)


@pytest.mark.platforms("linux", "macos")
def test_lazy_refusal_precedes_unavailable_target(monkeypatch):
    monkeypatch.setattr(client, "is_runtime", lambda: False)
    monkeypatch.setenv("HERMES_DISABLE_LAZY_INSTALLS", "1")
    monkeypatch.setattr(client, "_request", lambda *a, **kw: pytest.fail("refusal launched worker"))
    with pytest.raises(InstallError, match="lazy installs are disabled"):
        client.ensure("git")


def test_supported_missing_and_explicit_acquisition_still_use_worker(monkeypatch):
    engine = importlib.import_module("pm.install")
    monkeypatch.setattr(client, "is_runtime", lambda: False)
    monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)
    calls = []
    monkeypatch.setattr(client, "_request", lambda op, args, **kw: calls.append((op, args)))
    monkeypatch.setattr(engine, "env_for", lambda name, base_env=None: dict(base_env or {}))
    for explicit in (False, True):
        assert client.ensure("node", explicit=explicit, base_env={"PATH": "caller"}).env == {"PATH": "caller"}
    assert calls == [("ensure", {"name": "node", "explicit": False}),
                     ("ensure", {"name": "node", "explicit": True})]
