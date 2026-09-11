"""discord-radar backend plugin.

Provides:
- Inbound Discord admission & authorization guard via `pre_gateway_dispatch` hook:
  admitting configured trusted bot IDs (e.g. Radar 1547333242502520872) in specified
  guild/thread while dropping untrusted bots, enforcing non-controlling status
  (`allow_gateway_control=False`), and preventing command/approval injection or self-echo loops.
- Blocker observer logic, hook functions, and verification boundaries for Radar.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any, Dict, Optional, Set

from gateway.session import Platform

from .observer import (
    DiscordBlockerObserver,
    ObserverReport,
    BlockerItem,
    BlockerSuggestion,
    RADAR_ID,
    MODEL,
    PROVIDER,
    author_id,
    timestamp,
    pure_ack,
    validate_analysis,
    validate_history,
)
from .hook import run_discord_blocker_hook, gemini_evaluator

logger = logging.getLogger(__name__)

DEFAULT_ALLOWED_BOTS: Set[str] = {RADAR_ID}
DEFAULT_ALLOWED_GUILD: str = "1546331229240954923"
DEFAULT_ALLOWED_THREAD: str = "1546608648438943794"


def _get_allowed_bots(ctx) -> Set[str]:
    """Retrieve allowed bot IDs from plugin settings or default."""
    cfg_bots = ctx.get_config("allowed_bots", None)
    if cfg_bots is None:
        return set(DEFAULT_ALLOWED_BOTS)
    if isinstance(cfg_bots, list):
        return {str(b).strip() for b in cfg_bots if str(b).strip()}
    s = str(cfg_bots).strip()
    return {part.strip() for part in s.split(",") if part.strip()} if s else set()


def _get_allowed_guild(ctx) -> str:
    return str(ctx.get_config("allowed_guild_id", DEFAULT_ALLOWED_GUILD) or "").strip()


def _get_allowed_thread(ctx) -> str:
    return str(ctx.get_config("allowed_thread_id", DEFAULT_ALLOWED_THREAD) or "").strip()


def make_pre_gateway_dispatch_hook(ctx):
    def pre_gateway_dispatch_hook(event: Any, gateway: Any = None, session_store: Any = None, **kwargs) -> Optional[Dict[str, Any]]:
        source = getattr(event, "source", None)
        if not source or getattr(source, "platform", None) != Platform.DISCORD:
            return None

        # Only inspect bot-origin messages; human messages pass through untouched
        if not getattr(source, "is_bot", False):
            return None

        bot_id = str(getattr(source, "user_id", "") or "")
        allowed_bots = _get_allowed_bots(ctx)

        # 1. Reject untrusted bot IDs
        if bot_id not in allowed_bots:
            logger.info("discord-radar: dropping message from untrusted bot %s", bot_id)
            return {"action": "skip", "reason": f"untrusted_bot:{bot_id}"}

        # 2. Check guild boundary if configured
        allowed_guild = _get_allowed_guild(ctx)
        msg_guild = str(getattr(source, "guild_id", "") or getattr(source, "scope_id", "") or "")
        if allowed_guild and msg_guild and msg_guild != allowed_guild:
            logger.info("discord-radar: dropping bot %s message from unauthorized guild %s", bot_id, msg_guild)
            return {"action": "skip", "reason": "unauthorized_guild"}

        # 3. Check thread boundary if configured
        allowed_thread = _get_allowed_thread(ctx)
        msg_thread = str(getattr(source, "thread_id", "") or getattr(source, "chat_id", "") or "")
        if allowed_thread and msg_thread and msg_thread != allowed_thread:
            logger.info("discord-radar: dropping bot %s message from unauthorized thread %s", bot_id, msg_thread)
            return {"action": "skip", "reason": "unauthorized_thread"}

        # 4. Enforce non-controlling authority: cannot execute slash commands, approvals, or gateway control
        if hasattr(event, "allow_gateway_control"):
            event.allow_gateway_control = False

        # Tag content as observational evidence so downstream LLM and operator recognize bot origin
        raw_text = getattr(event, "text", "") or ""
        # Neutralize leading slash so text cannot be parsed as a command by any legacy fallback
        if raw_text.lstrip().startswith("/"):
            raw_text = raw_text.lstrip()[1:]
        
        tagged_text = f"[Bot Evidence from Radar <@{bot_id}>]: {raw_text}"
        return {"action": "rewrite", "text": tagged_text}

    return pre_gateway_dispatch_hook


def register(ctx) -> None:
    """Register discord-radar plugin hooks and tools."""
    hook = make_pre_gateway_dispatch_hook(ctx)
    ctx.register_hook("pre_gateway_dispatch", hook)
    logger.info("discord-radar plugin registered successfully")
