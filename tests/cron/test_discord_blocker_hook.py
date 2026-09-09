"""Hook controls; full REST/provider/restart flow is in test_discord_blocker_hook_red.

The former sender_fn/thread_reader fixtures bypassed the authoritative list,
provider and receipt path; those cases now exercise network edges there.
"""
import json

from cron.discord_blocker_hook import run_discord_blocker_hook


def test_disabled_preserves_list_and_sidecar_without_network(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("disabled observer must not access network")

    monkeypatch.setattr("urllib.request.urlopen", unexpected)
    source = tmp_path / "discord-now-state.json"
    sidecar = tmp_path / "discord-blocker-observer-state.json"
    source.write_text('{"evidence": {}}')
    sidecar.write_text('{"preserved": {}}')
    before = (source.read_bytes(), sidecar.read_bytes())
    report = run_discord_blocker_hook(str(source), enabled=False)
    assert not report.errors and report.model_calls == 0
    assert (source.read_bytes(), sidecar.read_bytes()) == before


def test_missing_state_is_reported_without_inference(tmp_path):
    report = run_discord_blocker_hook(str(tmp_path / "missing" / "state.json"))
    assert report.errors == ["observer_state_missing"]
    assert report.model_calls == 0 and not report.suggestions


def test_corrupt_state_report_is_exported_without_overwriting_source(tmp_path):
    source = tmp_path / "discord-now-state.json"
    source.write_text("{")
    output = tmp_path / "observer-report.json"
    report = run_discord_blocker_hook(str(source), output_report_path=str(output))
    assert report.errors == ["observer_source_or_credential_failed"]
    assert json.loads(output.read_text())["errors"] == report.errors
    assert source.read_text() == "{"
