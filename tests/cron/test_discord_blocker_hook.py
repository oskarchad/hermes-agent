import json
import pytest
from unittest.mock import MagicMock
from cron.discord_blocker_hook import run_discord_blocker_hook


def test_run_discord_blocker_hook_flow(tmp_path):
    state_file = tmp_path / "discord-now-state.json"
    state_data = {
        "guild_id": "1546331229240954923",
        "bot_id": "1546329595446296658",
        "evidence": {
            "Zadanie Test": {
                "channel_id": "1001",
                "message_id": "2001",
                "note": "Zablokowane: brak dostępu",
            }
        },
    }
    state_file.write_text(json.dumps(state_data), encoding="utf-8")

    thread_reader = MagicMock(return_value=[{"id": "m1", "author": "worker", "content": "brak uprawnień"}])
    sender = MagicMock(return_value=True)
    report_file = tmp_path / "observer-report.json"

    # Run 1: new blocker -> generates suggestion & calls sender
    report1 = run_discord_blocker_hook(
        state_file_path=str(state_file),
        output_report_path=str(report_file),
        target_bot_id="1546329595446296658",
        thread_reader=thread_reader,
        sender_fn=sender,
    )
    assert len(report1.suggestions) == 1
    assert sender.call_count == 1
    assert report_file.exists()

    # Verify sidecar state was written
    sidecar = tmp_path / "discord-blocker-observer-state.json"
    assert sidecar.exists()

def test_f6_failed_send_persists_pending_without_dedup_suppression(tmp_path):
    """F6: Failed delivery must remain pending/retryable on the next tick without
    re-analyzing/calling model, and not suppress retry by falsely committing delivery."""
    state_file = tmp_path / "discord-now-state.json"
    state_data = {
        "guild_id": "1546331229240954923",
        "bot_id": "1546329595446296658",
        "evidence": {
            "Zadanie Test": {
                "channel_id": "1001",
                "message_id": "2001",
                "note": "Zablokowane: test błędu",
                "status": "blocked",
            }
        },
    }
    state_file.write_text(json.dumps(state_data), encoding="utf-8")

    thread_reader = MagicMock(return_value=[{"id": "m1", "author": "worker", "content": "brak uprawnień"}])
    failing_sender = MagicMock(return_value=False)
    report_file = tmp_path / "observer-report.json"

    # Tick 1: Sender fails
    report1 = run_discord_blocker_hook(
        state_file_path=str(state_file),
        output_report_path=str(report_file),
        target_bot_id="1546329595446296658",
        thread_reader=thread_reader,
        sender_fn=failing_sender,
    )
    assert failing_sender.call_count == 1
    assert any("Failed to send" in e for e in report1.errors)

    # Tick 2: Sender now succeeds; should retry sending without treating it as unchanged-suppressed!
    succeeding_sender = MagicMock(return_value=True)
    report2 = run_discord_blocker_hook(
        state_file_path=str(state_file),
        output_report_path=str(report_file),
        target_bot_id="1546329595446296658",
        thread_reader=thread_reader,
        sender_fn=succeeding_sender,
    )
    assert succeeding_sender.call_count == 1
    assert "Zadanie Test" not in report2.unchanged_topics


def test_f2_disable_control_and_dry_run(tmp_path):
    """F2: Hook must support explicit enabled/disabled configuration and dry_run mode,
    rather than deleting state files to disable it."""
    state_file = tmp_path / "discord-now-state.json"
    state_data = {
        "guild_id": "1546331229240954923",
        "bot_id": "1546329595446296658",
        "evidence": {
            "Zadanie Test": {
                "channel_id": "1001",
                "message_id": "2001",
                "note": "Zablokowane",
                "status": "blocked",
            }
        },
    }
    state_file.write_text(json.dumps(state_data), encoding="utf-8")
    sender = MagicMock(return_value=True)

    # Disabled hook -> no evaluate, no send, clean return
    report_disabled = run_discord_blocker_hook(
        state_file_path=str(state_file),
        enabled=False,
        sender_fn=sender,
    )
    assert len(report_disabled.suggestions) == 0
    assert sender.call_count == 0

    # Dry-run hook -> evaluates suggestions, but DOES NOT send
    sender.reset_mock()
    report_dry = run_discord_blocker_hook(
        state_file_path=str(state_file),
        enabled=True,
        dry_run=True,
        thread_reader=lambda _: [{"id": "m1", "author": "worker", "content": "brak"}],
        sender_fn=sender,
    )
    assert len(report_dry.suggestions) == 1
    assert sender.call_count == 0


def test_f2_scheduler_post_run_hook_invokes_blocker(tmp_path, monkeypatch):
    """F2: Test scheduler post_run_hook executes on successful run without breaking the core scheduler."""
    from cron.scheduler import _finish_completed_run, _RunDelivery
    from cron.jobs import create_job

    state_file = tmp_path / "discord-now-state.json"
    state_file.write_text(json.dumps({
        "guild_id": "123",
        "evidence": {"Task": {"channel_id": "c1", "message_id": "m1", "note": "Zablokowane", "status": "blocked"}}
    }), encoding="utf-8")

    called_with = []
    def mock_hook_target(**kwargs):
        called_with.append(kwargs)

    monkeypatch.setattr("cron.discord_blocker_hook.run_discord_blocker_hook", mock_hook_target)

    job = {
        "id": "test_job_147",
        "post_run_hook": {
            "target": "cron.discord_blocker_hook:run_discord_blocker_hook",
            "kwargs": {"state_file_path": str(state_file), "dry_run": True},
        },
    }
    d = _RunDelivery(job=job, success=True, error=None)
    monkeypatch.setattr("cron.scheduler.mark_job_run", lambda *args, **kwargs: True)
    monkeypatch.setattr("cron.scheduler.finish_execution", lambda *args, **kwargs: True)

    res = _finish_completed_run(d, fire_owner=None, execution_id="exec_1")
    assert res is True
    assert len(called_with) == 1
    assert called_with[0]["state_file_path"] == str(state_file)
    assert called_with[0]["dry_run"] is True
