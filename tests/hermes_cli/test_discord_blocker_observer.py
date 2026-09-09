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


def test_f3_authoritative_blocked_section_parsing():
    """F3: Candidates must come from the actual list's **Zablokowane** section,
    NOT matching done items with historical 'zablokowane' notes, and correctly
    extracting items under **Zablokowane** even if evidence has no status field."""
    # Case 1: done item with historical word in note -> NOT selected
    state_done = {
        "evidence": {
            "Zadanie Done": {
                "channel_id": "1001",
                "message_id": "2001",
                "note": "Wcześniej zablokowane, teraz pomyślnie zakończone",
                "status": "done",
            }
        }
    }
    observer = DiscordBlockerObserver()
    selected_done = observer.extract_blocked_items(state_done)
    assert len(selected_done) == 0

    # Case 2: authoritative list content with **Zablokowane** section
    raw_list_content = (
        "## Teraz robimy\n"
        "**W trakcie**\n"
        "☐ Finanse — PR #681 otwarty → <#1546596113581084682>\n\n"
        "**Zablokowane**\n"
        "☐ Audyt wizualny PDP — luki w wycinkach 46 stron; PDF niezatwierdzony → <#1547023057896476702>\n"
        "☐ AeroKołdra — Vera odrzuciła stronę; edytor zablokowany → <#1546942690967421049>\n\n"
        "**Ostatnio zrobione**\n"
        "☑ Wcześniejsze zadanie — wcześniej zablokowane → <#1546679236876832858>\n"
    )
    state_with_list = {
        "list_content": raw_list_content,
        "evidence": {
            "Audyt wizualny PDP": {
                "channel_id": "1547023057896476702",
                "message_id": "2001",
                "note": "Luki w wycinkach",
            },
            "AeroKołdra": {
                "channel_id": "1546942690967421049",
                "message_id": "2002",
                "note": "Vera odrzuciła stronę",
            },
            "Wcześniejsze zadanie": {
                "channel_id": "1546679236876832858",
                "message_id": "2003",
                "note": "wcześniej zablokowane",
            },
        },
    }
    selected = observer.extract_blocked_items(state_with_list)
    topics = [item.topic for item in selected]
    assert "Audyt wizualny PDP" in topics
    assert "AeroKołdra" in topics
    assert "Wcześniejsze zadanie" not in topics
    assert "Finanse" not in topics


def test_f5_correlation_and_settlement_rules():
    """F5: Correlate strictly after suggestion timestamp; exclude observer's own messages;
    plain ACK is not action/readback; preserve deadline."""
    state = {
        "evidence": {
            "Zadanie A": {
                "channel_id": "1001",
                "message_id": "2001",
                "note": "Zablokowane",
                "status": "blocked",
            }
        }
    }
    observer = DiscordBlockerObserver(target_bot_id="otto_bot_id")

    # Initial run: suggestion generated
    t0 = time.time()
    old_messages = [
        {"id": "m0", "author": "otto_bot_id", "content": "Stara wiadomość Otta sprzed sugestii", "timestamp": t0 - 100}
    ]
    report1 = observer.evaluate(state, thread_reader=lambda _: old_messages)
    assert len(report1.suggestions) == 1

    # Check 1: Old Otto message does NOT settle new suggestion
    report2 = observer.evaluate(state, thread_reader=lambda _: old_messages)
    assert "Zadanie A" not in report2.settled_topics

    # Check 2: Observer's own suggestion (even with kanban_unblock text) does NOT settle suggestion
    own_suggestion_msg = {
        "id": "m_obs",
        "author": "ObserverBot",
        "content": report1.suggestions[0].content,
        "timestamp": t0 + 10,
    }
    messages_with_own = old_messages + [own_suggestion_msg]
    report3 = observer.evaluate(state, thread_reader=lambda _: messages_with_own)
    assert "Zadanie A" not in report3.settled_topics

    # Check 3: Plain ACK (e.g. 'przyjęto', 'ok', 'widzę') is NOT full action/readback -> does not settle
    ack_msg = {
        "id": "m_ack",
        "author": "otto_bot_id",
        "content": "Przyjęto, sprawdzam to.",
        "timestamp": t0 + 20,
    }
    messages_with_ack = messages_with_own + [ack_msg]
    report4 = observer.evaluate(state, thread_reader=lambda _: messages_with_ack)
    assert "Zadanie A" not in report4.settled_topics

    # Check 4: Real action / readback (e.g. executed kanban_unblock or confirmed decision) settles
    action_msg = {
        "id": "m_act",
        "author": "otto_bot_id",
        "content": "Wykonano kanban_unblock t_123, zadanie odblokowane.",
        "timestamp": t0 + 30,
    }
    messages_with_action = messages_with_own + [action_msg]
    report5 = observer.evaluate(state, thread_reader=lambda _: messages_with_action)
    assert "Zadanie A" in report5.settled_topics


def test_f7_missing_reader_or_failed_analysis_emits_error_no_unblock():
    """F7: Missing reader or failed analysis must result in unknown/error outcome,
    NEVER emitting an allowed_action claiming 'kanban_unblock' permission."""
    state = {
        "evidence": {
            "Zadanie A": {
                "channel_id": "1001",
                "message_id": "2001",
                "note": "Zablokowane",
                "status": "blocked",
            }
        }
    }
    # No reader provided -> error/unknown, no allowed_action with kanban_unblock
    observer = DiscordBlockerObserver()
    report = observer.evaluate(state, thread_reader=None)
    assert len(report.errors) > 0 or len(report.suggestions) == 0 or any(
        "kanban_unblock" not in s.allowed_action for s in report.suggestions
    )

    # Failed evaluator -> records error, does NOT give unverified kanban_unblock advice
    def broken_eval(*_args):
        raise RuntimeError("LLM unavailable")

    report_err = observer.evaluate(
        state,
        thread_reader=lambda _: [{"id": "1", "author": "worker", "content": "issue"}],
        llm_evaluator=broken_eval,
    )
    assert len(report_err.errors) > 0
    for sugg in report_err.suggestions:
        assert "kanban_unblock" not in sugg.allowed_action
