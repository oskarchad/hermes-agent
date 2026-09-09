from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set


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
    evaluated_items: List[str] = field(default_factory=list)
    suggestions: List[BlockerSuggestion] = field(default_factory=list)
    unchanged_topics: List[str] = field(default_factory=list)
    settled_topics: List[str] = field(default_factory=list)
    model_calls: int = 0
    errors: List[str] = field(default_factory=list)


class DiscordBlockerObserver:
    """Observer that checks blocked items from Discord 'Teraz robimy' state,
    verifies the thread context, deduplicates unchanged blockers (0 model calls),
    and formats actionable suggestions for Otto from a trusted second bot.
    """

    def __init__(
        self,
        target_bot_id: str = "1546329595446296658",
        idle_followup_seconds: int = 1200,  # 20 minutes
    ) -> None:
        self.target_bot_id = str(target_bot_id).strip()
        self.idle_followup_seconds = idle_followup_seconds
        # In-memory or persisted state tracking: {topic: {"hash": str, "last_suggested_at": float, "settled": bool}}
        self._history: Dict[str, Dict[str, Any]] = {}

    def extract_blocked_items(self, state: Dict[str, Any]) -> List[BlockerItem]:
        """Extract only topics marked as blocked from state['evidence']."""
        evidence = state.get("evidence", {})
        blocked: List[BlockerItem] = []
        for topic, info in evidence.items():
            if not isinstance(info, dict):
                continue
            status = str(info.get("status", "")).lower()
            note = str(info.get("note", ""))
            # Check either status field or note containing 'zablokowan'
            if status == "blocked" or "zablokowan" in note.lower() or "zablokowane" in status:
                blocked.append(
                    BlockerItem(
                        topic=topic,
                        channel_id=str(info.get("channel_id", "")),
                        message_id=str(info.get("message_id", "")),
                        note=note,
                        status="blocked",
                    )
                )
        return blocked

    def _compute_thread_hash(self, messages: List[Dict[str, Any]]) -> str:
        """Deterministic fingerprint of relevant thread messages."""
        norm = []
        for m in messages:
            norm.append({
                "id": str(m.get("id", "")),
                "author": str(m.get("author", "")),
                "content": str(m.get("content", "")).strip(),
            })
        raw = json.dumps(norm, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _check_if_replied_or_acted(self, messages: List[Dict[str, Any]], last_suggested_time: float) -> bool:
        """Check if Otto or operator answered/acted in the thread after the suggestion."""
        for m in reversed(messages):
            author = str(m.get("author", "")).lower()
            content = str(m.get("content", "")).lower()
            # If Otto or operator replied or mentioned action
            if "otto" in author or self.target_bot_id in author:
                return True
            if any(act in content for act in ["kanban_unblock", "odblokowan", "wznawiam", "przyjęto", "zgoda"]):
                return True
        return False

    def evaluate(
        self,
        state: Dict[str, Any],
        thread_reader: Optional[Callable[[str], List[Dict[str, Any]]]] = None,
        llm_evaluator: Optional[Callable[[str, List[Dict[str, Any]]], Dict[str, str]]] = None,
    ) -> ObserverReport:
        """Evaluate blocked items, verify thread history, dedupe unchanged items,
        and generate structured suggestions for Otto.
        """
        report = ObserverReport()
        guild_id = str(state.get("guild_id", "1546331229240954923"))
        blocked_items = self.extract_blocked_items(state)

        for item in blocked_items:
            report.evaluated_items.append(item.topic)
            channel_id = item.channel_id
            if not channel_id:
                report.errors.append(f"Missing channel_id for {item.topic}")
                continue

            # Read thread messages if reader provided
            messages = thread_reader(channel_id) if thread_reader else []
            thread_hash = self._compute_thread_hash(messages)
            prev_record = self._history.get(item.topic)

            now = time.time()
            if prev_record:
                prev_hash = prev_record.get("hash")
                last_time = prev_record.get("last_suggested_at", 0.0)

                # Check if thread was settled/acted upon
                if self._check_if_replied_or_acted(messages, last_time):
                    report.settled_topics.append(item.topic)
                    prev_record["settled"] = True
                    continue

                # Deduplicate: unchanged thread context -> 0 model calls, 0 suggestions
                if prev_hash == thread_hash:
                    # Check if 20-min deadline passed for a single follow-up
                    followup_sent = prev_record.get("followup_sent", False)
                    if now - last_time >= self.idle_followup_seconds and not followup_sent:
                        prev_record["followup_sent"] = True
                        # Send 1 follow-up ping
                        sugg = self._build_suggestion(
                            guild_id=guild_id,
                            channel_id=channel_id,
                            item=item,
                            messages=messages,
                            is_followup=True,
                        )
                        report.suggestions.append(sugg)
                        # Static rule evaluation; 0 LLM calls for unchanged follow-up ping
                    else:
                        report.unchanged_topics.append(item.topic)
                    continue

            # If here, this is a new blocker or new messages in thread
            # Evaluate using LLM or structured heuristic
            blocker_reason = item.note
            proof_link = ""
            allowed_action = "Zweryfikuj przyczynę blokady w wątku i wykonaj kanban_unblock lub zgłoś decyzję operatorowi."

            if messages:
                latest_m = messages[-1]
                m_id = str(latest_m.get("id", ""))
                proof_link = f"https://discord.com/channels/{guild_id}/{channel_id}/{m_id}"

            if llm_evaluator:
                report.model_calls += 1
                try:
                    eval_res = llm_evaluator(item.topic, messages)
                    if isinstance(eval_res, dict):
                        blocker_reason = eval_res.get("reason", blocker_reason)
                        allowed_action = eval_res.get("action", allowed_action)
                except Exception as e:
                    report.errors.append(f"LLM evaluator error on {item.topic}: {e}")
            else:
                # Deterministic heuristic if no LLM passed
                pass

            sugg = self._build_suggestion(
                guild_id=guild_id,
                channel_id=channel_id,
                item=item,
                messages=messages,
                proof_link=proof_link,
                reason=blocker_reason,
                action=allowed_action,
                is_followup=False,
            )
            report.suggestions.append(sugg)
            self._history[item.topic] = {
                "hash": thread_hash,
                "last_suggested_at": now,
                "followup_sent": False,
                "settled": False,
            }

        return report

    def _build_suggestion(
        self,
        guild_id: str,
        channel_id: str,
        item: BlockerItem,
        messages: List[Dict[str, Any]],
        proof_link: str = "",
        reason: str = "",
        action: str = "",
        is_followup: bool = False,
    ) -> BlockerSuggestion:
        target_mention = f"<@{self.target_bot_id}>"
        reason = reason or item.note
        action = action or "Sprawdź wymagane wejście lub zależność i wznów zadanie albo przekaż Oskarowi."
        if not proof_link and messages:
            last_id = str(messages[-1].get("id", ""))
            proof_link = f"https://discord.com/channels/{guild_id}/{channel_id}/{last_id}"
        elif not proof_link:
            proof_link = f"https://discord.com/channels/{guild_id}/{channel_id}/{item.message_id}"

        prefix = "⚠️ [OBSERWATOR BLOKAD · FOLLOW-UP]" if is_followup else "⚠️ [OBSERWATOR BLOKAD]"
        content = (
            f"{prefix} {target_mention}\n"
            f"**Temat:** {item.topic}\n"
            f"**Przyczyna blokady:** {reason}\n"
            f"**Dowód w wątku:** {proof_link}\n"
            f"**Dozwolony następny krok:** {action}\n"
            f"*(Brak automatycznej eskalacji bez zmian; odpowiedź lub podjęcie akcji rozlicza tę sugestię)*"
        )

        return BlockerSuggestion(
            topic=item.topic,
            channel_id=channel_id,
            target_mention=target_mention,
            blocker_reason=reason,
            proof_link=proof_link,
            allowed_action=action,
            content=content,
        )
