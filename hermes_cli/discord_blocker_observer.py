from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

RADAR_ID = "1547333242502520872"
MODEL = "ag/gemini-3.8-flash-high"
PROVIDER = "9router"


def timestamp(value):
    if isinstance(value, (int, float)):
        return float(value)
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Timezone required")
    return dt.timestamp()


def author_id(message):
    author = message.get("author")
    return str(author.get("id", "")) if isinstance(author, dict) else ""


def normalize_messages(messages):
    if not isinstance(messages, list):
        raise ValueError("Invalid Discord history")
    if any(not isinstance(m, dict) or not str(m.get("id", "")).isdigit() for m in messages):
        raise ValueError("Invalid Discord message identity")
    return sorted(messages, key=lambda m: int(m["id"]))


def validate_analysis(result, context):
    if not isinstance(result, dict) or result.get("status") not in {"known", "unknown", "error"}:
        raise ValueError("Invalid analysis status")
    ids = result.get("evidence_ids")
    available = {m["id"] for m in context["messages"]}
    if not isinstance(ids, list) or any(not isinstance(i, str) or i not in available for i in ids):
        raise ValueError("Analysis cites unavailable evidence")
    if not isinstance(result.get("reason"), str) or not 0 < len(result["reason"]) <= 600:
        raise ValueError("Invalid analysis reason")
    if result["status"] == "known":
        if not ids or not isinstance(result.get("action"), str) or not 0 < len(result["action"]) <= 600:
            raise ValueError("Known analysis requires cited proposal")
    elif result.get("action"):
        raise ValueError("Unknown/error cannot propose action")
    return {k: result[k] for k in ("status", "reason", "evidence_ids", "action") if k in result}


@dataclass
class BlockerItem:
    topic: str
    channel_id: str
    message_id: str
    note: str
    status: str = "blocked"


@dataclass
class BlockerSuggestion:
    topic: str
    channel_id: str
    target_mention: str
    blocker_reason: str
    proof_link: str
    allowed_action: str
    content: str


@dataclass
class ObserverReport:
    evaluated_items: list[str] = field(default_factory=list)
    suggestions: list[BlockerSuggestion] = field(default_factory=list)
    unchanged_topics: list[str] = field(default_factory=list)
    settled_topics: list[str] = field(default_factory=list)
    model_calls: int = 0
    errors: list[str] = field(default_factory=list)


class DiscordBlockerObserver:
    """Bounded analysis and delivery state; chat assertions never authorize actions."""

    def __init__(self, target_bot_id="1546329595446296658", idle_followup_seconds=1200,
                 observer_bot_id=RADAR_ID):
        self.target_bot_id = str(target_bot_id)
        self.observer_bot_id = str(observer_bot_id)
        self.idle_followup_seconds = idle_followup_seconds
        self._history: dict[str, dict[str, Any]] = {}

    def extract_blocked_items(self, state):
        # Link identity, not fuzzy topic names or historical notes, owns membership.
        channels = set()
        blocked = False
        for line in (state.get("list_content") or "").splitlines():
            line = line.strip()
            if line.startswith("**") and line.endswith("**"):
                blocked = line.strip("*").strip().casefold() == "zablokowane"
            elif blocked:
                channels.update(re.findall(r"<#(\d+)>", line))
                channels.update(re.findall(r"https://discord\.com/channels/\d+/(\d+)", line))
        return [BlockerItem(str(topic), str(info["channel_id"]), str(info.get("message_id", "")),
                            str(info.get("note", "")))
                for topic, info in state.get("evidence", {}).items()
                if isinstance(info, dict) and str(info.get("channel_id", "")) in channels]

    def _check_if_replied_or_acted(self, messages, last_suggested_time, prev_last_id=None):
        # Retained private entry point: a sentence is never an execution receipt.
        return False

    def _context(self, state, item, messages, record):
        bounded = [{"id": str(m["id"]), "author_id": author_id(m),
                    "content": str(m.get("content", ""))[:1600]} for m in messages[-10:]]
        return {"topic": item.topic, "channel_id": item.channel_id,
                "goal": str(state.get("scope") or item.topic)[:1200],
                "constraints": str(state.get("policy") or "No new permissions; operator gates remain required")[:2000],
                "blocked_evidence": item.note[:1200], "messages": bounded,
                "prior": {k: record[k] for k in ("analysis", "cursor", "delivery_receipt", "disposition", "action_evidence") if k in record}}

    def _queue(self, item, record, guild_id, followup=False):
        analysis = record["analysis"]
        proof = f"https://discord.com/channels/{guild_id}/{item.channel_id}/{analysis['evidence_ids'][0]}"
        mention = f"<@{self.target_bot_id}>"
        prefix = "[OBSERWATOR BLOKAD · FOLLOW-UP]" if followup else "[OBSERWATOR BLOKAD]"
        content = (f"{prefix} {mention}\nTemat: {item.topic[:200]}\n"
                   f"Przyczyna: {analysis['reason']}\nDowód: {proof}\n"
                   f"Propozycja (nie zgoda): {analysis['action']}\n"
                   "Zachowaj zakres i bramki operatora. ACK/zamiar nie jest dowodem wykonania.")
        suggestion = BlockerSuggestion(item.topic, item.channel_id, mention, analysis["reason"],
                                       proof, analysis["action"], content)
        record["pending_suggestion"] = dict(asdict(suggestion), followup=followup)
        record["delivery_pending"] = True
        return suggestion

    def evaluate(self, state, thread_reader=None, llm_evaluator=None):
        report = ObserverReport()
        for item in self.extract_blocked_items(state):
            report.evaluated_items.append(item.topic)
            record = self._history.setdefault(item.topic, {})
            if record and record.get("version") != 2:
                report.errors.append(f"legacy_state:{item.topic}")
                continue
            record["version"] = 2
            record["channel_id"] = item.channel_id
            try:
                messages = normalize_messages(thread_reader(item.channel_id)) if thread_reader else []
                if not messages:
                    raise ValueError("Missing thread evidence")
            except Exception:
                report.errors.append(f"thread_read_failed:{item.topic}")
                continue
            record["cursor"] = str(messages[-1]["id"])
            # Pending delivery and one response window are independent of chat wording.
            # ACK, negation, intent, strangers and own echo never reset that window.
            if record.get("pending_suggestion"):
                report.unchanged_topics.append(item.topic)
                continue
            receipt = record.get("delivery_receipt")
            if receipt:
                actors = {self.target_bot_id, str(state.get("operator_id", ""))} - {"", self.observer_bot_id}
                for m in messages:
                    if author_id(m) in actors and int(m["id"]) > int(receipt["id"]):
                        try:
                            if timestamp(m.get("timestamp")) <= receipt["delivered_at"]:
                                continue
                        except (ValueError, TypeError):
                            continue
                        record["disposition"] = {"kind": "response_observed_not_action",
                                                  "message_id": str(m["id"]), "author_id": author_id(m),
                                                  "timestamp": m["timestamp"],
                                                  "suggestion_message_id": receipt["id"]}
                if not record.get("followup_sent") and time.time() >= receipt["delivered_at"] + self.idle_followup_seconds:
                    report.suggestions.append(self._queue(item, record, state.get("guild_id", ""), True))
                else:
                    report.unchanged_topics.append(item.topic)
                continue
            material = [m for m in messages if author_id(m) != self.observer_bot_id]
            context = self._context(state, item, material, record)
            # Prior/cursor are continuity context, not new material evidence.
            hash_input = {k: v for k, v in context.items() if k != "prior"}
            digest = hashlib.sha256(json.dumps(hash_input, sort_keys=True).encode()).hexdigest()
            if record.get("hash") == digest and not record.get("retry_at"):
                report.unchanged_topics.append(item.topic)
                continue
            if time.time() < record.get("retry_at", 0):
                report.unchanged_topics.append(item.topic)
                continue
            record["hash"] = digest
            record["model"] = {"provider": PROVIDER, "model": MODEL}
            try:
                if not material or not llm_evaluator:
                    raise ValueError("Missing analyzer/evidence")
                report.model_calls += 1
                result = validate_analysis(llm_evaluator(context), context)
                record["analysis"] = result
                if result["status"] == "error":
                    raise ValueError("Analyzer returned error")
                record.pop("retry_at", None)
                record.pop("failures", None)
                if result["status"] == "known":
                    report.suggestions.append(self._queue(item, record, state.get("guild_id", "")))
            except Exception:
                failures = min(record.get("failures", 0) + 1, 5)
                record.update(failures=failures, retry_at=time.time() + min(600 * 2 ** (failures - 1), 9600))
                record["analysis"] = {"status": "error", "reason": "analysis_failed", "evidence_ids": []}
                report.errors.append(f"analysis_failed:{item.topic}")
        return report
