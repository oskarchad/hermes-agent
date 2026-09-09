from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from hermes_cli.discord_blocker_observer import DiscordBlockerObserver, ObserverReport


def run_discord_blocker_hook(
    state_file_path: str,
    output_report_path: Optional[str] = None,
    target_bot_id: str = "1546329595446296658",
    thread_reader: Optional[Callable[[str], List[Dict[str, Any]]]] = None,
    sender_fn: Optional[Callable[[str, str], bool]] = None,
) -> ObserverReport:
    """Read discord-now-state.json, run DiscordBlockerObserver, send any new suggestions,
    and persist observer state and report without mutating the primary list writer.
    """
    path = Path(state_file_path)
    if not path.exists():
        raise FileNotFoundError(f"State file {state_file_path} not found")

    with open(path, "r", encoding="utf-8") as f:
        state = json.load(f)

    observer = DiscordBlockerObserver(target_bot_id=target_bot_id)

    # If an observer state sidecar exists, load it
    observer_state_file = path.parent / "discord-blocker-observer-state.json"
    if observer_state_file.exists():
        try:
            with open(observer_state_file, "r", encoding="utf-8") as f:
                saved_history = json.load(f)
                if isinstance(saved_history, dict):
                    observer._history = saved_history
        except Exception:
            pass

    report = observer.evaluate(state, thread_reader=thread_reader)

    # Dispatch suggestions if sender provided
    if sender_fn and report.suggestions:
        for sugg in report.suggestions:
            try:
                sent = sender_fn(sugg.channel_id, sugg.content)
                if not sent:
                    report.errors.append(f"Failed to send suggestion to {sugg.channel_id}")
            except Exception as e:
                report.errors.append(f"Error sending suggestion to {sugg.channel_id}: {e}")

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
        }
        with open(out_p, "w", encoding="utf-8") as f:
            json.dump(report_data, f, indent=2, ensure_ascii=False)

    return report
