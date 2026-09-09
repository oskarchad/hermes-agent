from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from hermes_cli.discord_blocker_observer import DiscordBlockerObserver, ObserverReport

logger = logging.getLogger(__name__)


def run_discord_blocker_hook(
    state_file_path: str,
    output_report_path: Optional[str] = None,
    target_bot_id: str = "1546329595446296658",
    thread_reader: Optional[Callable[[str], List[Dict[str, Any]]]] = None,
    sender_fn: Optional[Callable[[str, str], bool]] = None,
    enabled: bool = True,
    dry_run: bool = False,
    bot_token: Optional[str] = None,
) -> ObserverReport:
    """Read discord-now-state.json, run DiscordBlockerObserver, send any new suggestions,
    and persist observer state and report without mutating the primary list writer.

    Supports explicit enabled toggle (returns empty report if False) and dry_run mode.
    Handles failed delivery by keeping items retryable on the next tick without reanalysis.
    Provides production thread_reader and sender_fn defaults using tools.discord_tool._discord_request
    when bot_token (e.g. DISCORD_OBSERVER_BOT_TOKEN or bot_token) is provided.
    """
    if not enabled:
        logger.info("Discord blocker hook is disabled via configuration.")
        return ObserverReport()

    path = Path(state_file_path)
    if not path.exists():
        raise FileNotFoundError(f"State file {state_file_path} not found")

    with open(path, "r", encoding="utf-8") as f:
        state = json.load(f)

    # If thread_reader or sender_fn not provided, wire production discord request boundaries if token present
    if thread_reader is None or sender_fn is None:
        resolved_token = bot_token or os.environ.get("DISCORD_OBSERVER_BOT_TOKEN") or ""
        if resolved_token:
            from tools.discord_tool import _discord_request
            if thread_reader is None:
                def _prod_reader(ch_id: str) -> List[Dict[str, Any]]:
                    res = _discord_request("GET", f"/channels/{ch_id}/messages?limit=25", resolved_token)
                    return res if isinstance(res, list) else []
                thread_reader = _prod_reader

            if sender_fn is None and not dry_run:
                def _prod_sender(ch_id: str, content: str) -> bool:
                    body = {"content": content, "allowed_mentions": {"users": [target_bot_id]}}
                    res = _discord_request("POST", f"/channels/{ch_id}/messages", resolved_token, body=body)
                    return bool(isinstance(res, dict) and res.get("id"))
                sender_fn = _prod_sender

    observer = DiscordBlockerObserver(target_bot_id=target_bot_id)

    # Load observer sidecar state if present
    observer_state_file = path.parent / "discord-blocker-observer-state.json"
    if observer_state_file.exists():
        try:
            with open(observer_state_file, "r", encoding="utf-8") as f:
                saved_history = json.load(f)
                if isinstance(saved_history, dict):
                    observer._history = saved_history
        except Exception:
            pass

    # Check for pending deliveries from previous ticks (F6)
    pending_deliveries: Dict[str, Dict[str, Any]] = {}
    for topic, rec in observer._history.items():
        if rec.get("delivery_pending") and rec.get("pending_suggestion"):
            pending_deliveries[topic] = rec["pending_suggestion"]

    report = observer.evaluate(state, thread_reader=thread_reader)

    # If there are items that were evaluated, mark their pending state in history
    for sugg in report.suggestions:
        if sugg.topic in observer._history:
            observer._history[sugg.topic]["delivery_pending"] = True
            observer._history[sugg.topic]["pending_suggestion"] = {
                "channel_id": sugg.channel_id,
                "content": sugg.content,
                "topic": sugg.topic,
            }

    # Dispatch suggestions if sender provided and not dry_run
    if not dry_run and sender_fn:
        # 1. Dispatch freshly generated suggestions
        for sugg in list(report.suggestions):
            try:
                sent = sender_fn(sugg.channel_id, sugg.content)
                if sent:
                    if sugg.topic in observer._history:
                        observer._history[sugg.topic]["delivery_pending"] = False
                        observer._history[sugg.topic].pop("pending_suggestion", None)
                else:
                    report.errors.append(f"Failed to send suggestion to {sugg.channel_id}")
                    # Revert last_suggested_at / followup_sent so next tick can retry delivery without reanalysis
                    if sugg.topic in observer._history:
                        observer._history[sugg.topic]["delivery_pending"] = True
            except Exception as e:
                report.errors.append(f"Error sending suggestion to {sugg.channel_id}: {e}")
                if sugg.topic in observer._history:
                    observer._history[sugg.topic]["delivery_pending"] = True

        # 2. Retry any pending suggestions that weren't in report.suggestions (e.g. unchanged thread in tick 2)
        for topic, p_sugg in list(pending_deliveries.items()):
            if not any(s.topic == topic for s in report.suggestions):
                try:
                    sent = sender_fn(p_sugg["channel_id"], p_sugg["content"])
                    if sent:
                        if topic in observer._history:
                            observer._history[topic]["delivery_pending"] = False
                            observer._history[topic].pop("pending_suggestion", None)
                            if topic in report.unchanged_topics:
                                report.unchanged_topics.remove(topic)
                    else:
                        report.errors.append(f"Failed to retry send suggestion for {topic}")
                except Exception as e:
                    report.errors.append(f"Error retrying send suggestion for {topic}: {e}")
    elif dry_run:
        logger.info("Discord blocker hook running in dry-run mode; suggestions not sent.")

    # Persist sidecar state
    try:
        with open(observer_state_file, "w", encoding="utf-8") as f:
            json.dump(observer._history, f, indent=2, ensure_ascii=False)
    except Exception as e:
        report.errors.append(f"Could not persist observer state: {e}")

    # Optionally persist report
    if output_report_path:
        out_p = Path(output_report_path)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        report_data = {
            "evaluated_items": report.evaluated_items,
            "suggestions_count": len(report.suggestions),
            "suggestions": [
                {
                    "topic": s.topic,
                    "channel_id": s.channel_id,
                    "reason": s.blocker_reason,
                    "proof_link": s.proof_link,
                    "action": s.allowed_action,
                }
                for s in report.suggestions
            ],
            "unchanged_topics": report.unchanged_topics,
            "settled_topics": report.settled_topics,
            "model_calls": report.model_calls,
            "errors": report.errors,
            "enabled": enabled,
            "dry_run": dry_run,
        }
        with open(out_p, "w", encoding="utf-8") as f:
            json.dump(report_data, f, indent=2, ensure_ascii=False)

    return report
