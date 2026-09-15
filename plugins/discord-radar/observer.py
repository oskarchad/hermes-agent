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


def pure_ack(message):
    # Full-message match only: "ACK, but ..." carries evidence, not just receipt.
    text = str(message.get("content", "")).strip().casefold().rstrip(".! ")
    response_only = text in {
        "ack", "ok", "okay", "przyjęto", "potwierdzam odbiór", "received", "👍",
    } or re.fullmatch(r"(?:ack; not executed|nie wykonano [\w_]+|wznawiam przez [\w_]+)", text)
    return not message.get("attachments") and not message.get("embeds") and bool(response_only)


def validate_history(history):
    for topic, current in history.items():
        revisions = current.get("revisions")
        if not isinstance(revisions, list):
            raise ValueError("Invalid revision archive")
        for record in [*revisions, current]:
            if (not isinstance(record, dict) or record.get("version") != 3
                    or not isinstance(record.get("revision"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", record["revision"])
                    or type(record.get("episode")) is not int or record["episode"] < 1
                    or type(record.get("eligible")) is not bool
                    or not str(record.get("channel_id", "")).isdigit()
                    or not str(record.get("message_id", "")).isdigit()):
                raise ValueError("Invalid revision state")
            for key in ("pending_suggestion", "delivery_receipt", "followup_receipt"):
                if key not in record:
                    continue
                bound = record[key]
                if (not isinstance(bound, dict) or bound.get("revision") != record["revision"]
                        or bound.get("episode") != record["episode"]):
                    raise ValueError("Invalid proposal revision")
                if key == "pending_suggestion":
                    if (bound.get("topic") != topic or bound.get("channel_id") != record["channel_id"]
                            or not isinstance(bound.get("content"), str)
                            or type(bound.get("followup")) is not bool):
                        raise ValueError("Invalid pending identity")
                elif not str(bound.get("id", "")).isdigit() or not isinstance(bound.get("delivered_at"), (int, float)):
                    raise ValueError("Invalid receipt identity")


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
    list_entry: str = ""


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
    delivery_topics: list[str] = field(default_factory=list)


class DiscordBlockerObserver:
    """Bounded analysis and delivery state; chat assertions never authorize actions."""

    def __init__(self, target_bot_id="1546329595446296658", idle_followup_seconds=1200,
                 observer_bot_id=RADAR_ID):
        self.target_bot_id = str(target_bot_id)
        self.observer_bot_id = str(observer_bot_id)
        self.idle_followup_seconds = idle_followup_seconds
        self._history: dict[str, dict[str, Any]] = {}

    def extract_blocked_items(self, state):
        items = []
        blocked = False
        for line in (state.get("list_content") or "").splitlines():
            line = line.strip()
            if line.startswith("**") and line.endswith("**"):
                blocked = line.strip("*").strip().casefold() == "zablokowane"
                continue
            if not blocked or not line or line.casefold().strip("_. ") in {"brak", "brak zablokowanych tematów"}:
                continue
            channels = set(re.findall(r"<#(\d+)>", line))
            links = re.findall(r"https://discord\.com/channels/(\d+)/(\d+)(?:/(\d+))?", line)
            channels.update(ch for guild, ch, _ in links if guild == str(state.get("guild_id")))
            title = line.lstrip("☐☑*- ")
            matches = []
            for topic, info in state.get("evidence", {}).items():
                if not isinstance(info, dict) or str(info.get("channel_id")) not in channels:
                    continue
                # Producer emits one exact topic label followed by a delimiter.
                named = re.match(re.escape(str(topic)) + r"(?=\s*(?:—|–|:|→|<#|https://|$))", title)
                linked = (str(state.get("guild_id")), str(info["channel_id"]), str(info.get("message_id"))) in links
                if named or linked:
                    matches.append((topic, info))
            if len(matches) != 1 or len(channels) != 1:
                raise ValueError("Ambiguous blocked topic identity")
            topic, info = matches[0]
            mid = str(info.get("message_id", ""))
            if not mid.isdigit() or any(i.topic == topic for i in items):
                raise ValueError("Invalid blocked topic identity")
            items.append(BlockerItem(str(topic), str(info["channel_id"]), mid,
                                     str(info.get("note", "")), list_entry=line))
        return items

    def _topic_messages(self, state, item, messages, record):
        siblings = {str(info.get("message_id")) for topic, info in state["evidence"].items()
                    if topic != item.topic and isinstance(info, dict)
                    and str(info.get("channel_id")) == item.channel_id}
        dedicated = item.channel_id in state.get("thread_ids", []) and not siblings
        # Old proposal replies remain topic evidence after a new revision is opened.
        roots = {item.message_id}
        for revision in [*record.get("revisions", []), record]:
            if revision.get("channel_id") != item.channel_id:
                continue
            roots.add(str(revision.get("message_id", "")))
            roots.update(str(revision[k]["id"]) for k in ("delivery_receipt", "followup_receipt") if revision.get(k))
            pending = revision.get("pending_suggestion") or {}
            if pending.get("sent_id"):
                roots.add(pending["sent_id"])
        selected = []
        for m in messages:
            mid = str(m["id"])
            if mid in siblings:
                continue
            ref = m.get("message_reference") or {}
            reply = str(ref.get("message_id")) in roots and str(ref.get("channel_id", item.channel_id)) == item.channel_id
            if mid in roots or reply or (dedicated and int(mid) >= int(item.message_id)):
                roots.add(mid)
                selected.append(m)
        if not any(str(m["id"]) == item.message_id for m in selected):
            raise ValueError("Selected topic anchor unavailable")
        return selected

    def _observe_responses(self, state, record, messages):
        actors = {self.target_bot_id, str(state.get("operator_id", ""))} - {"", self.observer_bot_id}
        channel = record.get("channel_id")
        revisions = [r for r in [*record.get("revisions", []), record] if r.get("channel_id") == channel]
        receipts = {str(r[key]["id"]): r[key] for r in revisions
                    for key in ("delivery_receipt", "followup_receipt") if r.get(key)}
        ancestry = {mid: mid for mid in receipts}
        for revision in revisions:
            known = revision.get("disposition") or {}
            if known.get("suggestion_message_id") in receipts:
                ancestry[str(known["message_id"])] = known["suggestion_message_id"]
        for m in messages:
            mid = str(m["id"])
            ref = m.get("message_reference") or {}
            if str(ref.get("channel_id", channel)) == channel:
                parent = ancestry.get(str(ref.get("message_id")))
                if parent:
                    ancestry[mid] = parent
            if author_id(m) not in actors:
                continue
            suggestion_id = ancestry.get(mid)
            receipt = receipts.get(suggestion_id or "")
            try:
                observed_at = timestamp(m.get("timestamp"))
                if receipt and (int(mid) <= int(receipt["id"]) or observed_at <= receipt["delivered_at"]):
                    continue
            except (ValueError, TypeError):
                continue
            # Re-reading an older window must not replace the latest response.
            previous = record.get("disposition") or {}
            if int(mid) < int(previous.get("message_id", "0")):
                continue
            record["disposition"] = {"kind": "response_observed_not_action",
                                     "message_id": mid, "author_id": author_id(m),
                                     "timestamp": m["timestamp"],
                                     "suggestion_message_id": suggestion_id}

    def _start_revision(self, record, item, digest):
        history = record.get("revisions", [])
        episode = record.get("episode", 0) + (not record.get("eligible", False))
        if record.get("revision"):
            history.append({k: v for k, v in record.items() if k != "revisions"})
        disposition = record.get("disposition")
        record.clear()
        record.update(version=3, channel_id=item.channel_id, message_id=item.message_id,
                      revision=digest, episode=episode, eligible=True, revisions=history)
        if disposition:
            record["disposition"] = disposition

    def _check_if_replied_or_acted(self, messages, last_suggested_time, prev_last_id=None):
        # Retained private entry point: a sentence is never an execution receipt.
        return False

    def _context(self, state, item, messages, record):
        bounded = [{"id": str(m["id"]), "author_id": author_id(m),
                    "content": str(m.get("content", ""))[:1600]} for m in messages[-10:]]
        return {"topic": item.topic, "channel_id": item.channel_id,
                "goal": str(state.get("scope") or item.topic)[:1200],
                "constraints": str(state.get("policy") or "No new permissions; operator gates remain required")[:2000],
                "blocked_evidence": item.note[:1200], "current_blocked_entry": item.list_entry[:1900], "messages": bounded,
                "prior": {k: record[k] for k in ("analysis", "cursor", "delivery_receipt", "disposition", "action_evidence") if k in record}}

    def _queue(self, item, record, guild_id, followup=False):
        analysis = record["analysis"]
        proof = f"https://discord.com/channels/{guild_id}/{item.channel_id}/{analysis['evidence_ids'][0]}"
        mention = f"<@{self.target_bot_id}>"
        prefix = "[OBSERWATOR BLOKAD · FOLLOW-UP]" if followup else "[OBSERWATOR BLOKAD]"
        content = (f"{prefix} {mention}\nTemat: {item.topic[:200]}\n"
                   f"Kontekst: {record['episode']}:{len(record['revisions']) + 1}:{record['revision']}\n"
                   f"Przyczyna: {analysis['reason']}\nDowód: {proof}\n"
                   f"Propozycja (nie zgoda): {analysis['action']}\n"
                   "Zachowaj zakres i bramki operatora. ACK/zamiar nie jest dowodem wykonania.")
        suggestion = BlockerSuggestion(item.topic, item.channel_id, mention, analysis["reason"],
                                       proof, analysis["action"], content)
        record["pending_suggestion"] = dict(asdict(suggestion), followup=followup,
                                            revision=record["revision"], episode=record["episode"])
        record["delivery_pending"] = True
        return suggestion

    def evaluate(self, state, thread_reader=None, llm_evaluator=None):
        report = ObserverReport()
        try:
            items = self.extract_blocked_items(state)
        except (ValueError, TypeError, AttributeError):
            report.errors.append("blocked_topic_identity_unknown")
            return report
        current = {item.topic for item in items}
        for topic, record in self._history.items():
            if record.get("version") == 3 and topic not in current:
                record["eligible"] = False
        for item in items:
            report.evaluated_items.append(item.topic)
            record = self._history.get(item.topic, {})
            if record and record.get("version") != 3:
                report.errors.append(f"legacy_state:{item.topic}")
                continue
            try:
                messages = normalize_messages(thread_reader(item.channel_id)) if thread_reader else []
                messages = self._topic_messages(state, item, messages, record)
            except Exception:
                report.errors.append(f"thread_read_failed:{item.topic}")
                continue
            material = [m for m in messages if author_id(m) != self.observer_bot_id and not pure_ack(m)]
            context = self._context(state, item, material, record)
            # Identity and full source evidence own revision; transport, cursor and ACK do not.
            hash_input = {"topic": item.topic, "channel": item.channel_id, "anchor": item.message_id,
                          "goal": state.get("scope"), "policy": state.get("policy"),
                          "note": item.note, "entry": item.list_entry,
                          "messages": [{"id": m["id"], "author": author_id(m), "content": m.get("content"),
                                        "attachments": m.get("attachments", []), "embeds": m.get("embeds", [])}
                                       for m in material]}
            digest = hashlib.sha256(json.dumps(hash_input, sort_keys=True).encode()).hexdigest()
            self._observe_responses(state, record, messages)
            if record.get("revision") != digest or not record.get("eligible"):
                self._start_revision(record, item, digest)
            self._history[item.topic] = record
            record["cursor"] = str(messages[-1]["id"])
            if record.get("pending_suggestion"):
                report.delivery_topics.append(item.topic)
                report.unchanged_topics.append(item.topic)
                continue
            receipt = record.get("delivery_receipt")
            if receipt:
                if not record.get("followup_sent") and time.time() >= receipt["delivered_at"] + self.idle_followup_seconds:
                    report.suggestions.append(self._queue(item, record, state.get("guild_id", ""), True))
                    report.delivery_topics.append(item.topic)
                else:
                    report.unchanged_topics.append(item.topic)
                continue
            if record.get("analysis") and not record.get("retry_at"):
                report.unchanged_topics.append(item.topic)
                continue
            if time.time() < record.get("retry_at", 0):
                report.unchanged_topics.append(item.topic)
                continue
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
                    report.delivery_topics.append(item.topic)
            except Exception:
                failures = min(record.get("failures", 0) + 1, 5)
                record.update(failures=failures, retry_at=time.time() + min(600 * 2 ** (failures - 1), 9600))
                record["analysis"] = {"status": "error", "reason": "analysis_failed", "evidence_ids": []}
                report.errors.append(f"analysis_failed:{item.topic}")
        return report
