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
        """Extract only topics marked as blocked.
        Prioritize the authoritative list content '**Zablokowane**' section if present.
        If list_content has a '**Zablokowane**' section, derive candidates strictly from it
        (never falling back to searching historical notes when the section is empty).
        If list_content is absent, only include items with explicit status == 'blocked'.
        Never match 'done' items.
        """
        evidence = state.get("evidence", {})
        list_content = state.get("list_content", "") or ""
        blocked: List[BlockerItem] = []

        blocked_topics_from_list: Optional[Set[str]] = None
        if "**zablokowane**" in list_content.lower():
            # Parse sections in list_content
            lines = list_content.splitlines()
            in_blocked = False
            found_topics: Set[str] = set()
            for line in lines:
                l_str = line.strip()
                if l_str.startswith("**") and l_str.endswith("**"):
                    sec_name = l_str.strip("*").strip().lower()
                    if sec_name == "zablokowane":
                        in_blocked = True
                        continue
                    else:
                        in_blocked = False
                elif in_blocked and (l_str.startswith("☐") or l_str.startswith("-") or l_str.startswith("*")):
                    # Item line: e.g. "☐ Audyt wizualny PDP — luki..."
                    content_after_bullet = l_str.lstrip("☐-* ").strip()
                    topic = content_after_bullet.split("—", 1)[0].split("-", 1)[0].strip()
                    if topic:
                        found_topics.add(topic.lower())
            blocked_topics_from_list = found_topics

        for topic, info in evidence.items():
            if not isinstance(info, dict):
                continue
            status = str(info.get("status", "")).lower().strip()
            note = str(info.get("note", ""))

            # Explicit exclusion: done/completed status is NEVER blocked
            if status in {"done", "completed", "resolved", "zakończone"}:
                continue

            is_blocked = False
            if blocked_topics_from_list is not None:
                # Strictly match against topics found in the authoritative list section
                is_blocked = topic.lower() in blocked_topics_from_list or any(
                    bt in topic.lower() or topic.lower() in bt for bt in blocked_topics_from_list
                )
            else:
                # If no list_content section available, require explicit blocked status or note starting with Zablokowane
                if status in {"blocked", "zablokowane"}:
                    is_blocked = True
                elif not status and note.strip().lower().startswith("zablokowan") and not any(
                    ex in note.lower() for ex in ["wcześniej zablokowan", "odblokowan", "zakończon"]
                ):
                    is_blocked = True

            if is_blocked:
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

    def _check_if_replied_or_acted(
        self,
        messages: List[Dict[str, Any]],
        last_suggested_time: float,
        prev_last_id: Optional[str] = None,
    ) -> bool:
        """Check if Otto or operator answered/acted in the thread strictly after the suggestion was sent.
        Exclude observer's own messages. Plain ACK is not an action.
        Requires valid timestamp > last_suggested_time or cursor > prev_last_id.
        """
        for m in reversed(messages):
            # Check message timestamp strictly
            ts = m.get("timestamp")
            m_time = 0.0
            if ts is not None:
                try:
                    if isinstance(ts, (int, float)):
                        m_time = float(ts)
                    elif isinstance(ts, str):
                        from datetime import datetime
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        m_time = dt.timestamp()
                except Exception:
                    m_time = 0.0

            # If last_suggested_time is set, strictly check timestamp if present;
            # if timestamp is not present, check message id / cursor vs last seen message id
            if last_suggested_time > 0:
                if ts is not None:
                    if m_time <= last_suggested_time:
                        continue
                elif prev_last_id:
                    try:
                        if int(str(m.get("id", "0"))) <= int(prev_last_id):
                            continue
                    except Exception:
                        pass

            author_field = m.get("author", "")
            author_str = ""
            author_id = ""
            if isinstance(author_field, dict):
                author_str = str(author_field.get("username", "")).lower()
                author_id = str(author_field.get("id", "")).strip()
            else:
                author_str = str(author_field).lower()
                author_id = str(author_field).strip()

            # Ignore observer's own suggestions
            content_str = str(m.get("content", ""))
            if "obserwator blokad" in content_str.lower() or "observerbot" in author_str:
                continue

            content_lower = content_str.lower()
            # Distinguish real action/readback from plain ACK
            is_actor = "otto" in author_str or (self.target_bot_id and self.target_bot_id == author_id)
            if not is_actor:
                continue

            # Real action / readback keywords (plain ACK like 'przyjęto', 'ok', 'widzę' does NOT count)
            real_actions = ["kanban_unblock", "odblokowan", "wykonano", "odblokowano", "wznawiam"]
            # Exclude plain conversational notes without actual command or action execution
            if any(act in content_lower for act in real_actions) and not ("czeka" in content_lower and "nie wykonano" in content_lower):
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

            if not thread_reader:
                report.errors.append(f"Thread reader unavailable for {item.topic}; cannot verify thread")
                continue

            # Read thread messages if reader provided
            try:
                messages = thread_reader(channel_id)
            except Exception as e:
                report.errors.append(f"Thread read failed for {item.topic}: {e}")
                continue

            if messages is None:
                report.errors.append(f"Thread read returned None for {item.topic}")
                continue

            thread_hash = self._compute_thread_hash(messages)
            prev_record = self._history.get(item.topic)

            now = time.time()
            if prev_record:
                prev_hash = prev_record.get("hash")
                last_time = prev_record.get("last_suggested_at", 0.0)

                prev_last_id = prev_record.get("last_message_id")

                # Check if thread was settled/acted upon
                if self._check_if_replied_or_acted(messages, last_time, prev_last_id=prev_last_id):
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
            allowed_action = "Zweryfikuj przyczynę blokady w wątku i zgłoś decyzję operatorowi."

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
                    allowed_action = f"Błąd analizy wątku ({e}); wymagana manualna weryfikacja."
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
            last_msg_id = str(messages[-1].get("id", "")) if messages else ""
            self._history[item.topic] = {
                "hash": thread_hash,
                "last_suggested_at": now,
                "last_message_id": last_msg_id,
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
