"""What a finite one-shot session (``hermes chat -q`` / ``--oneshot``, ``hermes -z``) does NOT do.

A one-shot run has no later session in its HERMES_HOME to learn for: the process answers one query and
exits. The interactive self-improvement loop is pure overhead there, and a measured one — across 21
one-shot benchmark trajectories the agent authored 7 new skills and patched a bundled one mid-task, 37 of
~215 tool calls were ``skill_view``/``skill_manage``, and skill text was 34% of every tool-result byte fed
back into context. Three rules follow, all keyed on the same session marker the approval gate and the
delegation dispatcher already read (``HERMES_SINGLE_QUERY_SESSION``), so interactive sessions are untouched.
A dispatcher-owned Kanban worker is also launched via ``-q`` but keeps the skill surface (first two rules;
see :func:`skill_footprint_applies`): its card may be skill maintenance.

* ``skill_manage`` is not offered (``skills_list``/``skill_view`` stay: reading a domain skill can still win);
* the ## Skills prompt drops the "record it / patch it / offer to save" coaching and the "load process skills
  even for tasks you already know" push, keeping only "load a skill when it adds knowledge you lack";
* delegation is capped per session (``delegation.oneshot_max_children``): subagents each re-pay a cold
  system prompt and re-explore the repo, and the observed spawns were mostly "independent review of my own
  work" rather than parallel work.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List

ONESHOT_HIDDEN_TOOLS = frozenset({"skill_manage"})


def is_single_query_session() -> bool:
    """The finite ``-q`` marker, read through the session env so gateway-bound sessions never see it."""
    try:
        from gateway.session_context import get_session_env
    except Exception:
        import os
        get_session_env = os.environ.get
    return str(get_session_env("HERMES_SINGLE_QUERY_SESSION", "") or "") == "1"


def skill_footprint_applies() -> bool:
    """One-shot, minus the dispatcher-owned Kanban worker: it also runs via ``chat -q``, but its card may be
    skill maintenance (fleet rules and lessons live in skills), so it keeps ``skill_manage`` and the full
    ## Skills guidance. Only this footprint is lifted; approval, delegation cap and metrics still key on
    :func:`is_single_query_session`."""
    from agent.delegation_context import owned_kanban_task
    return is_single_query_session() and not owned_kanban_task()


def prune_oneshot_tools(tools: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """*tools* minus ``ONESHOT_HIDDEN_TOOLS``; identity when the skill footprint does not apply."""
    tools = list(tools)
    if not skill_footprint_applies():
        return tools
    return [t for t in tools if (t.get("function") or {}).get("name") not in ONESHOT_HIDDEN_TOOLS]


ONESHOT_SKILLS_LOAD_GUIDANCE = (
    "## Skills\n"
    "Scan the skills below and load one with skill_view(name) only when it carries domain knowledge you lack "
    "for THIS task (an API, a tool's commands, a project's conventions). Do not load general process skills "
    "(testing, debugging, review methodology) for work you already know how to do, and do not create or edit "
    "skills: this is a one-shot run with no later session to reuse them.\n"
)
