"""Captain reporting inbox & durable signal handling for TUI Gateway."""

from __future__ import annotations

_KANBAN_SILENT_KINDS = frozenset({"archived", "unblocked"})

import contextlib
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .method_ctx import bind_module

_log = logging.getLogger("tui_gateway.captain_inbox")

_CAPTAIN_LEASE_SECONDS = 120
_CAPTAIN_LEASE_RENEW_SECONDS = max(1.0, _CAPTAIN_LEASE_SECONDS / 3)
_CAPTAIN_RECEIVER_TTL_SECONDS = 15
# Hard context/SQLite guards. One poll leases at most this many event rows and
# the joined synthetic prompt never exceeds this UTF-8 byte budget.
_CAPTAIN_POLL_ROW_CAP = 16
_CAPTAIN_TURN_ROW_CAP = 1
_CAPTAIN_POLL_BYTE_CAP = 8 * 1024
_CAPTAIN_SIGNAL_BODY_BYTE_CAP = 2 * 1024


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """Bound text by UTF-8 bytes without splitting a code point."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    encoded = encoded[:max_bytes]
    while encoded:
        try:
            return encoded.decode("utf-8") + "\n[comment truncated]"
        except UnicodeDecodeError:
            encoded = encoded[:-1]
    return "[comment truncated]"


def _format_captain_signal_text(task, ev, board_slug: str, comment) -> str:
    """Render a bounded signal from its authoritative durable comment."""
    from agent.redact import redact_sensitive_text
    from hermes_cli.kanban_alerts import render_kanban_alert

    payload = getattr(ev, "payload", None) or {}
    task_id = getattr(ev, "task_id", "")
    title = (getattr(task, "title", None) or task_id)[:120]
    board_tag = f"[{board_slug}] " if board_slug else ""
    signal_class = str(payload.get("signal_class") or "signal").replace("_", " ")
    comment_id = payload.get("comment_id")
    if comment is None:
        body = "[authoritative source comment unavailable]"
        author = str(payload.get("author") or "unknown")[:120]
    else:
        author = str(comment.author or payload.get("author") or "unknown")[:120]
        body = _truncate_utf8(
            redact_sensitive_text(str(comment.body or ""), force=True),
            _CAPTAIN_SIGNAL_BODY_BYTE_CAP,
        )
    return render_kanban_alert(
        f"⚑ {board_tag}Captain {signal_class} on Kanban {task_id} — {title}\n"
        f"comment #{comment_id} by {author}:\n{body}"
    )


def _session_captain_profile(session: dict) -> str:
    """Resolve the profile that owns this session's Captain reports.

    Bound to the session's own profile — ``profile_home`` for a multiplexed
    Desktop session, otherwise the process launch profile — never a
    process-global env guess, so a Desktop session pinned to another profile
    reads that profile's ledger.
    """
    from hermes_cli.profiles import normalize_profile_name

    try:
        if isinstance(session, dict) and session.get("profile_home"):
            return normalize_profile_name(Path(session["profile_home"]).name)
    except Exception:
        pass
    return normalize_profile_name(_current_profile_name())


def _captain_row_eligible(
    conn,
    *,
    profile: str,
    this_key: str,
    origin_key,
    row_tenant,
    now: int,
) -> bool:
    """Whether this session may claim a Captain row with the given origin.

    - No origin (gateway/CLI/cron/unattached) → same-profile only when untenant.
    - We ARE the origin → always ours while live.
    - A different origin → only if that origin is not live and the row is
      untenant. Tenant-tagged fallback fails closed until session transport
      carries authoritative tenant identity.
    """
    origin = str(origin_key).strip() if origin_key else ""
    if not origin:
        return not row_tenant
    if origin == str(this_key or ""):
        return True
    if row_tenant:
        return False
    from hermes_cli import kanban_db as _kb

    return not _kb.captain_receiver_is_live(
        conn,
        profile=profile,
        session_key=origin,
        now=now,
        max_age_seconds=_CAPTAIN_RECEIVER_TTL_SECONDS,
    )


def _collect_captain_reports(
    conn,
    slug: str,
    session: dict,
    captain_profile: str,
    claimed_event_ids: set,
    claim_records: Optional[list[dict]],
    texts: list,
    row_limit: int = _CAPTAIN_POLL_ROW_CAP,
) -> int:
    """Claim eligible Captain inbox rows for this session's profile.

    Runs alongside the exact-origin ``kanban_notify_subs`` route on the same
    board connection. Rows whose ``task_event.id`` was already turned into text
    by the exact-origin route are settled but not re-rendered (dedup); the rest
    are formatted. Leases are recorded in ``claim_records`` so the caller acks
    them on a successful terminal turn or releases them on rejection/failure.

    A Captain row is acked ONLY after the synthetic turn reaches a successful
    terminal outcome and its transcript is durable, immediately before the
    stable-id assistant completion is emitted. Leasing is meaningless without
    a settlement callback.
    When ``claim_records`` is ``None`` (a probe-style call with no way to
    ack/release), this helper is a no-op and leaves every row ``pending`` —
    it never leases or acks merely because it was invoked.
    """
    from hermes_cli import kanban_db as _kb

    if claim_records is None:
        return 0

    # A synthetic turn carries one Captain event, giving every retry the same
    # event-derived completion identity even when another board's settlement
    # fails independently.
    row_limit = max(
        0,
        min(int(row_limit), _CAPTAIN_POLL_ROW_CAP, _CAPTAIN_TURN_ROW_CAP),
    )
    if row_limit == 0:
        return 0

    this_key = str(session.get("session_key") or "")
    now = int(time.time())
    try:
        candidates = _kb.read_captain_candidates(
            conn,
            profile=captain_profile,
            now=now,
            limit=row_limit,
            receiver_session_key=this_key,
            receiver_max_age_seconds=_CAPTAIN_RECEIVER_TTL_SECONDS,
        )
    except Exception:
        return 0
    eligible = [
        int(c["event_id"])
        for c in candidates
        if _captain_row_eligible(
            conn,
            profile=captain_profile,
            this_key=this_key,
            origin_key=c.get("origin_session_key"),
            row_tenant=c.get("tenant"),
            now=now,
        )
    ]
    if not eligible:
        return 0
    task_cache: dict = {}
    used_bytes = sum(len(t.encode("utf-8")) + 1 for t in texts)
    leased_count = 0
    for event_id in eligible[:row_limit]:
        if used_bytes >= _CAPTAIN_POLL_BYTE_CAP:
            break
        # One event per lease token makes byte paging lossless: rows that do
        # not fit this prompt remain pending/unleased for the next poll instead
        # of sharing an ack token with the visible prefix.
        owner = this_key or captain_profile
        token, events = _kb.lease_captain_reports(
            conn,
            profile=captain_profile,
            owner=owner,
            event_ids=[event_id],
            now=now,
            lease_seconds=_CAPTAIN_LEASE_SECONDS,
            receiver_session_key=this_key,
            receiver_max_age_seconds=_CAPTAIN_RECEIVER_TTL_SECONDS,
        )
        if not events:
            continue
        leased_count += 1
        ev = events[0]
        deliveries: list[dict] = []
        if ev.id in claimed_event_ids:
            claim_records.append({
                "route": "captain",
                "board": slug,
                "token": token,
                "owner": owner,
                "expected_count": len(events),
                "task_ids": {ev.task_id},
                "signal_task_ids": (
                    {ev.task_id} if ev.kind == "captain_signal" else set()
                ),
                "deliveries": deliveries,
            })
            continue  # already delivered via the exact-origin route this batch
        task = task_cache.get(ev.task_id)
        if task is None:
            task = _kb.get_task(conn, ev.task_id)
            task_cache[ev.task_id] = task
        # Captain payloads are a stricter privacy boundary than the existing
        # exact-origin notification: never copy the raw spawn/tool error, and
        # force-redact credential-shaped text even when global redaction is off.
        if ev.kind == "captain_signal":
            payload = ev.payload or {}
            try:
                comment_id = int(payload.get("comment_id"))
            except (TypeError, ValueError):
                comment_id = 0
            comment = _kb.get_comment(conn, comment_id) if comment_id else None
            if comment is not None and comment.task_id != ev.task_id:
                comment = None
            text = _format_captain_signal_text(task, ev, slug, comment)
        else:
            text = _format_kanban_event_text(
                {"task_id": ev.task_id},
                task,
                ev,
                slug,
                include_error_detail=False,
            )
        if text:
            from agent.redact import redact_sensitive_text

            text = redact_sensitive_text(text, force=True)
            remaining = _CAPTAIN_POLL_BYTE_CAP - used_bytes
            if remaining <= 0:
                _kb.release_captain_reports(conn, token=token, owner=owner)
                break
            encoded = text.encode("utf-8")
            if len(encoded) > remaining:
                encoded = encoded[:remaining]
                while encoded:
                    try:
                        text = encoded.decode("utf-8")
                        break
                    except UnicodeDecodeError:
                        encoded = encoded[:-1]
                if not encoded:
                    _kb.release_captain_reports(conn, token=token, owner=owner)
                    break
            claimed_event_ids.add(ev.id)
            texts.append(text)
            used_bytes += len(text.encode("utf-8")) + 1
            deliveries.append({
                "id": f"kanban:{slug}:{ev.id}",
                "event_id": ev.id,
                "text": text,
            })
        claim_records.append({
            "route": "captain",
            "board": slug,
            "token": token,
            "owner": owner,
            "expected_count": len(events),
            "task_ids": {ev.task_id},
            "signal_task_ids": (
                {ev.task_id} if ev.kind == "captain_signal" else set()
            ),
            "deliveries": deliveries,
        })
    return leased_count


def _touch_captain_receivers(session: dict) -> None:
    """Publish this live receiver on every board, even while its turn is busy."""
    session_key = str(session.get("session_key") or "")
    if not session_key or session.get("_finalized"):
        return
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_connect as kbc

    captain_profile = _session_captain_profile(session)
    try:
        boards = _kb.list_boards(include_archived=False)
    except Exception:
        boards = [{"slug": _kb.DEFAULT_BOARD}]
    seen_db_paths: set[str] = set()
    for board_meta in boards:
        slug = (board_meta or {}).get("slug") or _kb.DEFAULT_BOARD
        db_path = (board_meta or {}).get("db_path")
        try:
            resolved = (
                str(Path(db_path).expanduser().resolve())
                if db_path else str(_kb.kanban_db_path(slug).resolve())
            )
        except Exception:
            resolved = f"slug:{slug}"
        if resolved in seen_db_paths:
            continue
        seen_db_paths.add(resolved)
        conn = None
        try:
            conn = kbc.connect(board=slug)
            _kb.touch_captain_receiver(
                conn,
                profile=captain_profile,
                session_key=session_key,
                tenant=None,
            )
        except Exception:
            continue
        finally:
            if conn is not None:
                conn.close()


def _format_kanban_event_text(
    sub: dict,
    task,
    ev,
    board_slug: str,
    latest_run=None,
    *,
    include_error_detail: bool = True,
) -> Optional[str]:
    """Single-line notification text for one kanban event.

    Wording mirrors the gateway notifier (gateway/kanban_watchers.py) so a
    task completion reads the same in the TUI as it does on Telegram.
    Returns None for kinds that are claimed but intentionally silent.
    """
    from hermes_cli.kanban_alerts import (
        project_kanban_event_lineage,
        render_kanban_alert,
    )

    kind = getattr(ev, "kind", "")
    if not kind or kind in _KANBAN_SILENT_KINDS:
        return None
    task_id = sub.get("task_id", "")
    title = (getattr(task, "title", None) or task_id)[:120]
    board_tag = f"[{board_slug}] " if board_slug else ""
    who = getattr(task, "assignee", None) or ""
    tag = f"@{who} " if who else ""
    payload = getattr(ev, "payload", None) or {}
    if kind == "completed":
        handoff = ""
        summary = payload.get("summary")
        if summary:
            lines = str(summary).strip().splitlines()
            handoff = f"\n{lines[0][:200]}" if lines else ""
        elif getattr(task, "result", None):
            lines = str(task.result).strip().splitlines()
            handoff = f"\n{lines[0][:160]}" if lines else ""
        return render_kanban_alert(
            f"✔ {board_tag}{tag}Kanban {task_id} done — {title}{handoff}"
        )
    if kind == "blocked":
        reason = f": {str(payload.get('reason'))[:160]}" if payload.get("reason") else ""
        return render_kanban_alert(
            f"⏸ {board_tag}{tag}Kanban {task_id} blocked{reason}"
        )
    if kind == "gave_up":
        lineage = project_kanban_event_lineage(task, ev, latest_run)
        err = (
            f"\n{str(payload.get('error'))[:200]}"
            if include_error_detail and payload.get("error")
            else ""
        )
        blocked = "; task is blocked" if lineage.block_is_current else ""
        return render_kanban_alert(
            f"✖ {board_tag}{tag}Kanban {task_id} gave up after repeated "
            f"spawn failures{blocked}{err}",
            lineage=lineage,
        )
    if kind == "crashed":
        lineage = project_kanban_event_lineage(task, ev, latest_run)
        recovery = ""
        if lineage.retry_is_current:
            recovery = "; dispatcher will retry"
        elif lineage.block_is_current:
            recovery = "; task is blocked"
        return render_kanban_alert(
            f"✖ {board_tag}{tag}Kanban {task_id} worker crashed (pid gone)"
            f"{recovery}",
            lineage=lineage,
        )
    if kind == "timed_out":
        limit = 0
        try:
            limit = int(payload.get("limit_seconds") or 0)
        except (TypeError, ValueError):
            pass
        return render_kanban_alert(
            f"⏱ {board_tag}{tag}Kanban {task_id} timed out "
            f"(max_runtime={limit}s); will retry"
        )
    if kind == "status":
        return render_kanban_alert(
            f"🔄 {board_tag}{tag}Kanban {task_id} → {payload.get('status') or ''}"
        )
    return None


def _collect_kanban_notifications(
    session: dict,
    *,
    claim_records: Optional[list[dict]] = None,
    include_captain: bool = True,
) -> list:
    """Claim unseen terminal kanban events for this TUI session's subscriptions.

    ``kanban_create`` auto-subscribes TUI/desktop sessions with
    ``platform="tui"`` and ``chat_id=HERMES_SESSION_KEY`` (see
    tools/kanban_tools.py ``_maybe_auto_subscribe``). The gateway notifier
    can't deliver those — there is no "tui" messaging adapter — so this
    poller is the delivery path for them (issue #59890). Uses the same
    atomic cursor-claim (``claim_unseen_events_for_sub``) as the gateway
    notifier, so a subscription is delivered exactly once even if a gateway
    and a TUI poll the same board DB.

    Returns the list of formatted notification texts (may be empty). When
    ``claim_records`` is provided, each durable claim is appended there so
    the caller can either rewind it on rejection or finalize archive cleanup
    after the synthetic turn has been accepted.
    """
    session_key = str(session.get("session_key") or "")
    if not session_key or session.get("_finalized"):
        return []
    try:
        from hermes_cli import kanban_db as _kb
        from hermes_cli import kanban_db_connect as kbc
        from hermes_cli import kanban_db_notify as kbn
    except Exception:
        return []
    captain_profile = _session_captain_profile(session)
    texts: list = []
    try:
        boards = _kb.list_boards(include_archived=False)
    except Exception:
        try:
            boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
        except Exception:
            return []
    # Poll each resolved DB path once — multiple slugs can point at the same
    # DB when HERMES_KANBAN_DB pins the board path (same guard as the gateway
    # notifier).
    seen_db_paths: set = set()
    captain_rows_leased = 0
    for board_meta in boards:
        slug = (board_meta or {}).get("slug") or _kb.DEFAULT_BOARD
        db_path = (board_meta or {}).get("db_path")
        try:
            resolved = (
                str(Path(db_path).expanduser().resolve())
                if db_path else str(_kb.kanban_db_path(slug).resolve())
            )
        except Exception:
            resolved = f"slug:{slug}"
        if resolved in seen_db_paths:
            continue
        seen_db_paths.add(resolved)
        # A poller runs per live TUI/Desktop session. Avoid opening this board
        # writable unless it has an exact-origin subscription owned by this
        # session OR a Captain report waiting for this session's profile;
        # everything else is not actionable here.
        open_writable = False
        try:
            open_writable = kbn.count_notify_subs(
                board=slug,
                platform="tui",
                chat_id=session_key,
            ) > 0
        except Exception:
            # Preserve delivery if the read-only probe cannot inspect a
            # locked, corrupt, or otherwise unusual database.
            open_writable = True
        if not open_writable and claim_records is not None and include_captain:
            try:
                open_writable = _kb.count_captain_pending(
                    board=slug, profile=captain_profile
                ) > 0
            except Exception:
                open_writable = True
        if not open_writable:
            continue
        try:
            conn = kbc.connect(board=slug)
        except Exception:
            continue
        try:
            claimed_event_ids: set = set()
            try:
                subs = kbn.list_notify_subs(conn)
            except Exception:
                subs = []
            # Once this poll has leased its one Captain event, leave ordinary
            # subscription cursors untouched for the next turn. Conversely, an
            # earlier ordinary delivery prevents leasing a Captain row below.
            for sub in (() if captain_rows_leased else subs):
                if (sub.get("platform") or "").lower() != "tui":
                    continue
                if sub.get("chat_id") != session_key:
                    continue
                old_cursor, claimed_cursor, events = kbn.claim_unseen_events_for_sub(
                    conn,
                    task_id=sub["task_id"],
                    platform=sub["platform"],
                    chat_id=sub["chat_id"],
                    thread_id=sub.get("thread_id") or "",
                    kinds=_KANBAN_NOTIFY_KINDS,
                )
                if not events:
                    continue
                claim_record = None
                if claim_records is not None:
                    claim_record = {
                        "board": slug,
                        "sub": sub,
                        "old_cursor": old_cursor,
                        "claimed_cursor": claimed_cursor,
                        "unsubscribe": False,
                        "deliveries": [],
                    }
                    claim_records.append(claim_record)
                task = _kb.get_task(conn, sub["task_id"])
                latest_run = _kb.latest_run(conn, sub["task_id"])
                if claim_record is not None:
                    claim_record["unsubscribe"] = bool(
                        task and getattr(task, "status", "") == "archived"
                    )
                for ev in events:
                    captain_backed = conn.execute(
                        "SELECT 1 FROM kanban_captain_inbox WHERE event_id = ?",
                        (ev.id,),
                    ).fetchone() is not None
                    captain_owner = conn.execute(
                        "SELECT profile, origin_session_key "
                        "FROM kanban_captain_registry WHERE task_id = ?",
                        (ev.task_id,),
                    ).fetchone()
                    captain_owned_here = bool(
                        captain_owner
                        and captain_owner["profile"] == captain_profile
                        and (
                            not captain_owner["origin_session_key"]
                            or captain_owner["origin_session_key"] == session_key
                        )
                    )
                    if (
                        include_captain
                        and captain_backed
                        and captain_owned_here
                        and claim_records is not None
                    ):
                        # When this session owns both routes, the Captain lease
                        # is the single delivery receipt. Other creator sessions
                        # retain their independent exact-origin notification.
                        continue
                    # Record every event id the exact-origin route claimed so
                    # the Captain route can settle-but-not-duplicate the same
                    # task_event.id (both routes may see it for the origin).
                    claimed_event_ids.add(ev.id)
                    text = _format_kanban_event_text(
                        sub, task, ev, slug, latest_run,
                    )
                    if text:
                        texts.append(text)
                        if claim_record is not None:
                            claim_record["deliveries"].append({
                                "id": f"kanban:{slug}:{ev.id}",
                                "event_id": ev.id,
                                "text": text,
                            })
                # Unsubscribe only on archive. ``done`` is reversible in
                # review/controller flows, so retaining the subscription lets
                # a later reopen notify the same originating TUI/Desktop
                # session. The claimed cursor prevents historical replay.
                if (
                    claim_records is None
                    and task
                    and getattr(task, "status", "") == "archived"
                ):
                    try:
                        kbn.remove_notify_sub(
                            conn,
                            task_id=sub["task_id"],
                            platform=sub["platform"],
                            chat_id=sub["chat_id"],
                            thread_id=sub.get("thread_id") or "",
                        )
                    except Exception:
                        pass
            # Durable, profile-scoped Captain route (fallback + generalization
            # of the exact-origin route above). Claims reportable terminal
            # events for this session's profile, deduplicating any already
            # rendered by the exact-origin route on this board.
            if include_captain:
                try:
                    captain_rows_leased += _collect_captain_reports(
                        conn, slug, session, captain_profile,
                        claimed_event_ids, claim_records, texts,
                        row_limit=(
                            0
                            if texts
                            else _CAPTAIN_TURN_ROW_CAP - captain_rows_leased
                        ),
                    )
                except Exception as exc:
                    print(
                        f"[tui_gateway] captain report collection failed: "
                        f"{type(exc).__name__}: {exc}",
                        file=sys.stderr,
                    )
        finally:
            conn.close()
    return texts


def _renew_kanban_notification_claims(
    claim_records: list[dict],
    *,
    lease_seconds: int = _CAPTAIN_LEASE_SECONDS,
    now: int | None = None,
) -> None:
    """Renew every Captain token and fail closed if any ownership fence is lost."""
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_connect as kbc

    for record in claim_records:
        if record.get("route") != "captain":
            continue
        conn = kbc.connect(board=record["board"])
        try:
            expected = max(1, int(record.get("expected_count") or 1))
            renewed = _kb.renew_captain_reports(
                conn,
                token=record["token"],
                owner=record["owner"],
                lease_seconds=lease_seconds,
                now=now,
            )
            if renewed != expected:
                raise RuntimeError(
                    f"Captain lease renewal expected {expected} row(s), got {renewed}"
                )
        finally:
            conn.close()


def _settle_kanban_notification_claims(
    claim_records: list[dict],
    *,
    accepted: bool,
    reply_author: str | None = None,
    reply_body: str | None = None,
) -> None:
    """Authoritatively ack accepted claims or rewind every rejected claim.

    Captain rowcounts are ownership receipts, not diagnostics. Any exception or
    mismatch is propagated after the still-owned failed token is released, so a
    caller cannot emit a success frame for a settlement it did not commit.
    """
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_notify as kbn

    errors: list[Exception] = []
    failed_captain_records: list[dict] = []
    for record in claim_records:
        if record.get("route") == "captain":
            conn = None
            try:
                conn = kbc.connect(board=record["board"])
                expected = max(1, int(record.get("expected_count") or 1))
                owner = record.get("owner")
                if not owner:
                    raise RuntimeError("Captain settlement claim has no fenced owner")
                if accepted:
                    signal_task_ids = set(record.get("signal_task_ids") or ())
                    if signal_task_ids:
                        if not reply_author or not reply_body or not reply_body.strip():
                            raise RuntimeError(
                                "Captain signal settlement requires a persisted reply"
                            )
                        settled = _kb.ack_captain_reports_with_reply(
                            conn,
                            token=record["token"],
                            owner=owner,
                            reply_author=reply_author,
                            reply_body=reply_body,
                            reply_task_ids=signal_task_ids,
                        )
                    else:
                        settled = _kb.ack_captain_reports(
                            conn,
                            token=record["token"],
                            owner=owner,
                        )
                    if settled != expected:
                        raise RuntimeError(
                            f"Captain ack expected {expected} row(s), got {settled}"
                        )
                    # GC is retention-only after the authoritative ack. A GC
                    # failure must not turn a committed delivery into a retry.
                    for tid in record.get("task_ids") or ():
                        try:
                            _kb.captain_gc_task_if_settled(conn, tid)
                        except Exception:
                            logger.warning(
                                "Captain settled-task GC failed for %s",
                                tid,
                                exc_info=True,
                            )
                else:
                    _kb.release_captain_reports(
                        conn,
                        token=record["token"],
                        owner=owner,
                    )
            except Exception as exc:
                errors.append(exc)
                if accepted:
                    failed_captain_records.append(record)
            finally:
                if conn is not None:
                    conn.close()
            continue
        if accepted and not record["unsubscribe"]:
            continue
        sub = record["sub"]
        conn = None
        try:
            conn = kbc.connect(board=record["board"])
            if accepted:
                kbn.remove_notify_sub(
                    conn,
                    task_id=sub["task_id"],
                    platform=sub["platform"],
                    chat_id=sub["chat_id"],
                    thread_id=sub.get("thread_id") or "",
                )
            else:
                kbn.rewind_notify_cursor(
                    conn,
                    task_id=sub["task_id"],
                    platform=sub["platform"],
                    chat_id=sub["chat_id"],
                    thread_id=sub.get("thread_id") or "",
                    claimed_cursor=record["claimed_cursor"],
                    old_cursor=record["old_cursor"],
                )
        except Exception as exc:
            errors.append(exc)
        finally:
            if conn is not None:
                conn.close()

    # Cross-board settlement cannot be one SQLite transaction. Release only
    # tokens whose ack failed; already-acked siblings stay committed and each
    # normal delivery turn carries one stable event identity.
    if accepted:
        for record in failed_captain_records:
            conn = None
            try:
                conn = kbc.connect(board=record["board"])
                _kb.release_captain_reports(
                    conn,
                    token=record["token"],
                    owner=record.get("owner"),
                )
            except Exception as exc:
                errors.append(exc)
            finally:
                if conn is not None:
                    conn.close()

    if errors:
        detail = "; ".join(f"{type(exc).__name__}: {exc}" for exc in errors)
        raise RuntimeError(f"Kanban notification settlement failed: {detail}") from errors[0]


def _settle_captain_turn_claims(
    session: dict,
    claim_records: list[dict],
    *,
    completion_id: str,
    captain_profile: str,
    succeeded: bool,
) -> None:
    """Settle a Captain turn, posting its durable reply for signal claims."""
    signal_tasks = {
        task_id
        for record in claim_records
        if record.get("route") == "captain"
        for task_id in (record.get("signal_task_ids") or ())
    }
    if not succeeded or not signal_tasks:
        _settle_kanban_notification_claims(
            claim_records,
            accepted=bool(succeeded),
        )
        return

    try:
        receipt = _persisted_captain_report(session, completion_id)
        if receipt is None:
            raise RuntimeError("Captain signal reply is not durable")
        reply_body = _captain_reply_text(receipt)
        if not reply_body:
            raise RuntimeError("Captain signal reply is empty")
    except Exception:
        _settle_kanban_notification_claims(claim_records, accepted=False)
        raise

    _settle_kanban_notification_claims(
        claim_records,
        accepted=True,
        reply_author=captain_profile,
        reply_body=reply_body,
    )


_CAPTAIN_COMPLETION_METADATA_KEY = "captain_completion_id"
_CAPTAIN_TURN_PURPOSE = "captain_report"


def _captain_completion_id(
    claim_records: list[dict], report_texts: list[str]
) -> str:
    delivery_keys = sorted({
        f"{record.get('board') or 'default'}:{delivery.get('id')}"
        for record in claim_records
        for delivery in (record.get("deliveries") or ())
        if delivery.get("id")
    })
    receipt_material = "\n".join(delivery_keys or report_texts)
    return (
        "kanban-report:"
        + hashlib.sha256(receipt_material.encode("utf-8")).hexdigest()[:24]
    )


def _persisted_captain_report(session: dict, completion_id: str):
    """Return the profile-wide canonical receipt for a Captain event."""
    agent = session.get("agent")
    db = getattr(agent, "_session_db", None)
    session_id = getattr(agent, "session_id", None) or session.get("session_key")
    if db is None or not session_id or not completion_id:
        return None
    rehome = getattr(db, "rehome_captain_report", None)
    if callable(rehome):
        try:
            return rehome(completion_id, session_id)
        except Exception:
            logger.warning(
                "Captain profile-wide transcript reconciliation failed for %s",
                session_id,
                exc_info=True,
            )
            raise

    # Compatibility for narrow SessionDB test doubles and older embedders.
    messages = db.get_messages_as_conversation(session_id)
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        metadata = message.get("display_metadata")
        if (
            isinstance(metadata, dict)
            and metadata.get(_CAPTAIN_COMPLETION_METADATA_KEY) == completion_id
        ):
            return {
                "report": message,
                "messages": [message],
                "source_session_id": session_id,
                "destination_session_id": session_id,
                "moved": False,
            }
    return None


def _captain_reply_text(receipt: object) -> str:
    """Return one bounded, force-redacted durable Captain reply."""
    from agent.redact import redact_sensitive_text

    if not isinstance(receipt, dict):
        return ""
    return _truncate_utf8(
        redact_sensitive_text(
            str((receipt.get("report") or {}).get("content") or ""),
            force=True,
        ),
        _CAPTAIN_SIGNAL_BODY_BYTE_CAP,
    ).strip()


def _reconcile_persisted_captain_report(
    sid: str,
    session: dict,
    claim_records: list[dict],
    completion_id: str,
) -> bool:
    """Ack a durable Captain report without running the model a second time."""
    receipt = _persisted_captain_report(session, completion_id)
    if receipt is None:
        return False
    report = receipt["report"]
    pending = session.setdefault("_captain_pending_projection_ids", set())
    if receipt.get("moved"):
        pending.add(completion_id)

    _settle_kanban_notification_claims(
        claim_records,
        accepted=True,
        reply_author=_session_captain_profile(session),
        reply_body=_captain_reply_text(receipt),
    )

    with session["history_lock"]:
        if not any(
            isinstance(message, dict)
            and isinstance(message.get("display_metadata"), dict)
            and message["display_metadata"].get(_CAPTAIN_COMPLETION_METADATA_KEY)
            == completion_id
            for message in session.get("history") or ()
        ):
            session.setdefault("history", []).extend(receipt.get("messages") or [report])
            session["history_version"] = int(session.get("history_version", 0)) + 1

    if isinstance(pending, set) and completion_id in pending:
        pending.discard(completion_id)
        text = str(report.get("content") or "")
        payload = {
            "id": completion_id,
            "text": text,
            "status": "complete",
            "usage": _get_usage(session.get("agent")),
        }
        rendered = render_message(text, session.get("cols", 80))
        if rendered:
            payload["rendered"] = rendered
        _emit("message.start", sid)
        _emit("message.complete", sid, payload)
    return True


def _start_captain_lease_renewer(claim_records: list[dict]):
    """Keep Captain ownership fenced for an otherwise unbounded model turn."""
    if not any(record.get("route") == "captain" for record in claim_records):
        return None
    stop = threading.Event()
    failures: list[Exception] = []

    def _renew_loop() -> None:
        while not stop.wait(_CAPTAIN_LEASE_RENEW_SECONDS):
            try:
                _renew_kanban_notification_claims(claim_records)
            except Exception as exc:
                failures.append(exc)
                return

    thread = _RealThread(target=_renew_loop, daemon=True)
    thread.start()
    return stop, thread, failures


def _stop_captain_lease_renewer(handle) -> None:
    if handle is None:
        return
    stop, thread, failures = handle
    stop.set()
    thread.join()
    if failures:
        raise RuntimeError("Captain lease renewal failed") from failures[0]


def _notif_poll_kanban(sid: str, session: dict) -> None:
    """One kanban poll for this session: exact-origin subscriptions + the durable Captain route.

    Called by the upstream notification poller (``session_notifications._notification_poller_loop``)
    on its idle cadence; this module owns the name because the Captain route adds durable leases,
    transcript reconciliation and receipt settlement that the plain subscription poll has no notion
    of. The turn is reserved BEFORE the durable cursor is claimed and rewound if the claim yields
    nothing, so RAM is never the sole copy of a claimed event.
    """
    captain_runtime_deferred = getattr(session.get("agent"), "api_mode", "") == "codex_app_server"
    if not captain_runtime_deferred:
        try:
            _touch_captain_receivers(session)
        except Exception as heartbeat_exc:
            _notif_log_failure("Captain receiver heartbeat failed", heartbeat_exc)
    claims: list[dict] = []
    texts: list[str] = []
    kb_exc = None
    reserved = False
    with session["history_lock"]:
        if not session.get("running"):
            # Hold the lock through the DB claim. A concurrent user submit waits here instead of
            # observing a transient busy state on empty polls; when events exist, running=True is
            # already reserved before their cursor advances.
            session["running"] = True
            reserved = True
            try:
                texts = _collect_kanban_notifications(
                    session, claim_records=claims, include_captain=not captain_runtime_deferred,
                )
            except Exception as exc:
                kb_exc = exc
            if kb_exc is not None or not texts:
                try:
                    _settle_kanban_notification_claims(claims, accepted=kb_exc is None)
                except Exception as settle_exc:
                    kb_exc = settle_exc
                session["running"] = False
    if not reserved:
        return
    if kb_exc is not None:
        _notif_log_failure("kanban notification poll failed", kb_exc)
        return
    if not texts:
        return
    _dispatch_captain_turn(sid, session, claims, texts)


def _dispatch_captain_turn(
    sid: str, session: dict, claims: list[dict], texts: list[str]
) -> None:
    """Run the claimed (running=True) turn for one batch of kanban/Captain reports."""
    rid = f"__notif__{int(time.time() * 1000)}"
    settled = threading.Event()
    settled_lock = threading.Lock()
    completion_id = _captain_completion_id(claims, texts)
    has_captain_claim = any(record.get("route") == "captain" for record in claims)
    try:
        reconciled = _reconcile_persisted_captain_report(
            sid, session, claims, completion_id
        ) if has_captain_claim else False
    except Exception as reconcile_exc:
        release_exc = None
        try:
            _settle_kanban_notification_claims(claims, accepted=False)
        except Exception as exc:
            release_exc = exc
        _notif_log_failure("Captain receipt reconciliation failed", reconcile_exc)
        if release_exc is not None:
            _notif_log_failure("Captain reconciliation release failed", release_exc)
        _notif_release_turn(session)
        return
    if reconciled:
        _notif_release_turn(session)
        return
    lease_renewer = _start_captain_lease_renewer(claims)

    def _settle_after_terminal(
        succeeded: Optional[bool],
        *,
        _claim_records=list(claims),
        _completion_id=completion_id,
        _lease_handle=lease_renewer,
        _settled_event=settled,
        _settlement_lock=settled_lock,
    ) -> None:
        # The prompt turn is asynchronous. Bind every per-dispatch value now so later poll
        # intervals cannot rebind this closure to another claim set.
        with _settlement_lock:
            if _settled_event.is_set():
                return
            try:
                _stop_captain_lease_renewer(_lease_handle)
            except Exception:
                if succeeded is None:
                    _settled_event.set()
                    raise
                try:
                    _settle_kanban_notification_claims(_claim_records, accepted=False)
                finally:
                    _settled_event.set()
                raise
            if succeeded is None:
                # Rollback of the crash-staged input failed. Leave the claim leased rather than
                # making a dirty retry immediately eligible; normal lease expiry bounds the retry.
                _settled_event.set()
                return
            try:
                _settle_captain_turn_claims(
                    session,
                    _claim_records,
                    completion_id=_completion_id,
                    captain_profile=_session_captain_profile(session),
                    succeeded=bool(succeeded),
                )
            finally:
                # Mark after the authoritative attempt, not before it. Callback failures must
                # propagate through _run_prompt_submit and suppress the visible success frame.
                _settled_event.set()

    try:
        _emit("message.start", sid)
        accepted = _run_prompt_submit(
            rid, sid, session, "\n".join(texts),
            on_terminal=_settle_after_terminal,
            completion_id=completion_id,
            require_persisted=has_captain_claim,
            turn_purpose=_CAPTAIN_TURN_PURPOSE if has_captain_claim else "ordinary",
        )
        if accepted is False:
            raise RuntimeError("synthetic turn was not accepted")
    except Exception as exc:
        _settle_after_terminal(False)
        _notif_log_failure("kanban notification dispatch failed", exc)
        _notif_release_turn(session)
        _drain_queued_prompt(rid, sid, session)


def register(server) -> None:
    """Publish this module's helpers onto ``server``, rebound to its globals.

    ``override`` names the helpers this module deliberately takes over from
    ``session_notifications``: the Captain route replaces the plain subscription poll with a
    durable, leased, profile-scoped one, so its collector/formatter/poll entry point must win.
    Anything NOT listed here stays upstream's — see tests/tui_gateway/test_split_module_collisions.py.
    """
    bind_module(globals(), server, skip=(), override=(
        "_collect_kanban_notifications", "_format_kanban_event_text", "_notif_poll_kanban",
    ))
