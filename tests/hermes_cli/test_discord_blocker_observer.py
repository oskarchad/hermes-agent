import time
import pytest
from unittest.mock import MagicMock
from hermes_cli.discord_blocker_observer import (
    DiscordBlockerObserver,
    BlockerItem,
    ObserverReport,
)


def test_selects_only_blocked_items():
    state = {
        "evidence": {
            "Zadanie A": {
                "channel_id": "1001",
                "message_id": "2001",
                "note": "W toku: implementacja trwa",
                "status": "in_progress",
            },
            "Zadanie B": {
                "channel_id": "1002",
                "message_id": "2002",
                "note": "Zablokowane: brak dostępu do klucza API Stripe",
                "status": "blocked",
            },
            "Zadanie C": {
                "channel_id": "1003",
                "message_id": "2003",
                "note": "Zakończone: wdrożone na staging",
                "status": "done",
            },
        }
    }

    observer = DiscordBlockerObserver()
    blocked = observer.extract_blocked_items(state)
    assert len(blocked) == 1
    assert blocked[0].topic == "Zadanie B"
    assert blocked[0].channel_id == "1002"


def test_unchanged_thread_results_in_zero_model_calls():
    state = {
        "evidence": {
            "Zadanie B": {
                "channel_id": "1002",
                "message_id": "2002",
                "note": "Zablokowane: błąd uprawnień",
            }
        }
    }

    thread_messages = [
        {"id": "m1", "author": "dev", "content": "Nie mogę zapisać pliku, permission denied."}
    ]
    mock_reader = MagicMock(return_value=thread_messages)
    mock_llm = MagicMock()

    observer = DiscordBlockerObserver()

    # First run: evaluates, calls LLM or formats suggestion
    report1 = observer.evaluate(state, thread_reader=mock_reader, llm_evaluator=mock_llm)
    assert len(report1.suggestions) == 1
    assert report1.model_calls == 1

    # Second run with exact same thread messages: NO-OP, 0 model calls, 0 suggestions
    report2 = observer.evaluate(state, thread_reader=mock_reader, llm_evaluator=mock_llm)
    assert len(report2.suggestions) == 0
    assert report2.model_calls == 0
    assert "Zadanie B" in report2.unchanged_topics


def test_suggestion_format_contains_required_fields():
    state = {
        "guild_id": "1546331229240954923",
        "bot_id": "1546329595446296658",
        "evidence": {
            "Zadanie B": {
                "channel_id": "1002",
                "message_id": "2002",
                "note": "Zablokowane: brak certyfikatu",
            }
        }
    }

    thread_messages = [
        {"id": "m100", "author": "worker", "content": "Zablokowane: brak ssl_cert.pem"}
    ]
    mock_reader = MagicMock(return_value=thread_messages)

    observer = DiscordBlockerObserver(target_bot_id="1546329595446296658")
    report = observer.evaluate(state, thread_reader=mock_reader)

    assert len(report.suggestions) == 1
    sugg = report.suggestions[0]
    assert sugg.topic == "Zadanie B"
    assert sugg.channel_id == "1002"
    assert "1546329595446296658" in sugg.content  # Mentions Otto
    assert "https://discord.com/channels/1546331229240954923/1002/m100" in sugg.content or "dowód" in sugg.content.lower()
    assert sugg.allowed_action != ""


def test_resolved_or_replied_blocker_clears_or_settles():
    state = {
        "evidence": {
            "Zadanie B": {
                "channel_id": "1002",
                "message_id": "2002",
                "note": "Zablokowane: brak zgody",
            }
        }
    }

    mock_reader = MagicMock()
    mock_reader.return_value = [{"id": "m1", "author": "worker", "content": "Czekam na zgodę."}]

    observer = DiscordBlockerObserver()
    report1 = observer.evaluate(state, thread_reader=mock_reader)
    assert len(report1.suggestions) == 1

    # Now Otto responds in the thread with an action
    mock_reader.return_value = [
        {"id": "m1", "author": "worker", "content": "Czekam na zgodę."},
        {"id": "m2", "author": "otto", "content": "Zgoda udzielona, wznawiam zadanie przez kanban_unblock."},
    ]

    report2 = observer.evaluate(state, thread_reader=mock_reader)
    # The observer sees Otto responded/acted, so suggestion is resolved, no new suggestion sent
    assert len(report2.suggestions) == 0
    assert report2.model_calls == 0


def test_followup_deadline_fires_once_without_repeated_pings():
    state = {
        "evidence": {
            "Zadanie B": {
                "channel_id": "1002",
                "message_id": "2002",
                "note": "Zablokowane: brak certyfikatu",
            }
        }
    }

    thread_messages = [{"id": "m1", "author": "dev", "content": "brak certyfikatu"}]
    mock_reader = MagicMock(return_value=thread_messages)

    observer = DiscordBlockerObserver(idle_followup_seconds=60)
    # Run 1 at t=100
    report1 = observer.evaluate(state, thread_reader=mock_reader)
    assert len(report1.suggestions) == 1
    observer._history["Zadanie B"]["last_suggested_at"] = time.time() - 70  # simulate >60s passed

    # Run 2: deadline exceeded -> 1 follow-up ping sent
    report2 = observer.evaluate(state, thread_reader=mock_reader)
    assert len(report2.suggestions) == 1
    assert "FOLLOW-UP" in report2.suggestions[0].content
    assert report2.model_calls == 0  # 0 LLM calls for follow-up ping

    # Run 3: still unchanged and follow-up already sent -> NO-OP
    report3 = observer.evaluate(state, thread_reader=mock_reader)
    assert len(report3.suggestions) == 0
    assert "Zadanie B" in report3.unchanged_topics
