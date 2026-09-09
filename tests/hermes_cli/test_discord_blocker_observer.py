"""Evidence-only observer contracts. Discord payloads, never display-name authority."""
import pytest
from hermes_cli.discord_blocker_observer import DiscordBlockerObserver


def state(content="**Zablokowane**\n☐ A — czeka → <#100>"):
    return {"guild_id": "700", "operator_id": "999", "scope": "Discord only",
            "policy": "No restart without human approval", "list_content": content,
            "evidence": {"A": {"channel_id": "100", "message_id": "201", "note": "Zablokowane"}}}


def message(mid="201", author="555", content="Missing certificate", ts=100):
    return {"id": mid, "author": {"id": author, "username": "otto"},
            "content": content, "timestamp": ts}


def known(context):
    return {"status": "known", "reason": "Missing certificate", "action": "Ask operator for certificate",
            "evidence_ids": ["201"]}


@pytest.mark.parametrize("content", [None, "", "**W trakcie**\n☐ A → <#100>", "**Zablokowane**\nBrak"])
def test_no_current_blocked_links_never_uses_historical_notes(content):
    assert DiscordBlockerObserver().extract_blocked_items(state(content)) == []


def test_selection_uses_exact_thread_link_not_topic_substrings():
    s = state("**Zablokowane**\n☐ Different title → <#100>\n**Ostatnio zrobione**\n☑ A → <#101>")
    s["evidence"]["A longer"] = {"channel_id": "101", "note": "Zablokowane"}
    assert [i.channel_id for i in DiscordBlockerObserver().extract_blocked_items(s)] == ["100"]


@pytest.mark.parametrize("text", ["Wykonano kanban_unblock; readback status=ready.",
    "Zgoda udzielona, wznawiam zadanie przez kanban_unblock.", "Nie wykonano kanban_unblock",
    "Przyjęto, sprawdzam to."])
def test_text_or_display_name_never_proves_action(text):
    observer = DiscordBlockerObserver(target_bot_id="777")
    assert not observer._check_if_replied_or_acted([message("301", "777", text, 200)], 150, "250")


def test_response_retains_source_time_and_delivery_correlation_without_settlement(monkeypatch):
    monkeypatch.setattr("hermes_cli.discord_blocker_observer.time.time", lambda: 200)
    o = DiscordBlockerObserver(target_bot_id="777")
    o._history["A"] = {"version": 2, "delivery_receipt": {
        "id": "250", "author_id": o.observer_bot_id, "delivered_at": 150}}
    response = message("301", "777", "ACK; not executed", "2026-09-09T20:00:00Z")
    report = o.evaluate(state(), lambda _: [response], known)
    assert not report.settled_topics and report.model_calls == 0
    assert o._history["A"]["disposition"] == {
        "kind": "response_observed_not_action", "message_id": "301", "author_id": "777",
        "timestamp": response["timestamp"], "suggestion_message_id": "250"}


def test_known_analysis_persists_bounded_context_and_pending_not_sent(monkeypatch):
    monkeypatch.setattr("hermes_cli.discord_blocker_observer.time.time", lambda: 150)
    contexts = []
    def evaluate(context):
        contexts.append(context)
        return known(context)
    o = DiscordBlockerObserver(target_bot_id="777")
    r = o.evaluate(state(), lambda _: [message()], evaluate)
    assert len(r.suggestions) == 1 and r.model_calls == 1
    rec = o._history["A"]
    assert rec["pending_suggestion"] and not rec.get("delivery_receipt")
    assert not rec.get("last_suggested_at")
    assert contexts[0]["constraints"] and contexts[0]["goal"] and "prior" in contexts[0]
    assert rec["analysis"]["evidence_ids"] == ["201"] and rec["cursor"] == "201"
    again = o.evaluate(state(), lambda _: [message(), message("302", "777", "ACK", 200)], evaluate)
    assert again.model_calls == 0 and not again.suggestions


@pytest.mark.parametrize("result", [{}, {"status": "known", "reason": "x", "action": "unblock", "evidence_ids": ["9999"]},
                                      {"status": "unknown", "reason": "unknown", "evidence_ids": []}])
def test_unknown_or_invalid_never_suggests_and_failure_is_retryable(result, monkeypatch):
    clock = [150]
    monkeypatch.setattr("hermes_cli.discord_blocker_observer.time.time", lambda: clock[0])
    o = DiscordBlockerObserver()
    r = o.evaluate(state(), lambda _: [message()], lambda _: result)
    assert not r.suggestions
    if result.get("status") != "unknown":
        assert r.errors
        clock[0] += 600
        retry = o.evaluate(state(), lambda _: [message()], known)
        assert retry.model_calls == 1 and retry.suggestions
