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

    # Run 2: exact same thread -> unchanged -> 0 suggestions, 0 sends
    sender.reset_mock()
    report2 = run_discord_blocker_hook(
        state_file_path=str(state_file),
        output_report_path=str(report_file),
        target_bot_id="1546329595446296658",
        thread_reader=thread_reader,
        sender_fn=sender,
    )
    assert len(report2.suggestions) == 0
    assert sender.call_count == 0
    assert "Zadanie Test" in report2.unchanged_topics
