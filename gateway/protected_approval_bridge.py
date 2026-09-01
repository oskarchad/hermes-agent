"""Gateway-owned authority for protected-write approvals.

A Kanban worker submits only a bounded operation digest over the local gateway
control socket. This broker binds the request to the task's single origin
WebUI/API session and uses the existing blocking approval primitive. The API
run transport presents a structured request through its documented approval
event/endpoint. Plaintext/headless approval remains a separate transport and
cannot resolve this broker's requests.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from tools.approval import _await_gateway_decision, resolve_gateway_approval

logger = logging.getLogger(__name__)

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_BOARD_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

@dataclass(frozen=True)
class OperatorTarget:
    """One unambiguous authenticated operator conversation."""

    profile: str
    session_id: str
    platform: str
    task_id: str


@dataclass
class _PendingRequest:
    request: dict
    target: OperatorTarget
    approval_session_key: str
    deadline_monotonic: float
    status: str = "pending"
    choice: Optional[str] = None
    reason: Optional[str] = None
    terminal_at_monotonic: Optional[float] = None


class ProtectedApprovalBridge:
    """Correlate one protected operation with one authenticated operator."""

    def __init__(
        self,
        *,
        resolve_target: Callable[[dict], Optional[OperatorTarget]],
        notify: Callable[[OperatorTarget, dict], bool],
        validate_run: Callable[[dict], bool],
        monotonic: Callable[[], float] = time.monotonic,
        terminal_retention_seconds: float = 60.0,
        max_terminal_records: int = 1024,
    ) -> None:
        self._resolve_target = resolve_target
        self._notify = notify
        self._validate_run = validate_run
        self._monotonic = monotonic
        self._terminal_retention_seconds = max(
            0.0, float(terminal_retention_seconds)
        )
        self._max_terminal_records = max(1, int(max_terminal_records))
        self._lock = threading.RLock()
        self._pending: dict[str, _PendingRequest] = {}
        self._consumed: dict[tuple[str, str], float] = {}

    def _cleanup_locked(self, now: float) -> list[str]:
        """Expire stale grants and bound retained terminal/replay state."""
        expired_session_keys: list[str] = []
        for record in self._pending.values():
            if (
                record.status in {"pending", "approved"}
                and now > record.deadline_monotonic
            ):
                record.status = "timeout"
                record.choice = None
                record.reason = "deadline_expired"
                record.terminal_at_monotonic = now
                expired_session_keys.append(record.approval_session_key)

        for request_id, record in list(self._pending.items()):
            terminal_at = record.terminal_at_monotonic
            if (
                terminal_at is not None
                and now - terminal_at > self._terminal_retention_seconds
            ):
                self._pending.pop(request_id, None)

        terminal = sorted(
            (record.terminal_at_monotonic or now, request_id)
            for request_id, record in self._pending.items()
            if record.terminal_at_monotonic is not None
        )
        for _, request_id in terminal[: -self._max_terminal_records]:
            self._pending.pop(request_id, None)

        for correlation, consumed_at in list(self._consumed.items()):
            if now - consumed_at > self._terminal_retention_seconds:
                self._consumed.pop(correlation, None)
        consumed = sorted(
            (consumed_at, correlation)
            for correlation, consumed_at in self._consumed.items()
        )
        for _, correlation in consumed[: -self._max_terminal_records]:
            self._consumed.pop(correlation, None)
        return expired_session_keys

    @staticmethod
    def _deny_expired(session_keys: list[str]) -> None:
        for session_key in session_keys:
            try:
                resolve_gateway_approval(session_key, "deny")
            except Exception:
                pass

    def _cleanup(self) -> float:
        now = self._monotonic()
        with self._lock:
            expired = self._cleanup_locked(now)
        self._deny_expired(expired)
        return now

    @property
    def control_handlers(self) -> dict[str, Callable[[dict], dict]]:
        return {
            "protected_approval.submit": self.submit,
            "protected_approval.poll": self.poll,
            "protected_approval.cancel": self.cancel,
        }

    @staticmethod
    def _validated_request(payload: dict) -> dict:
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError("unsupported protected approval request")
        request_id = payload.get("request_id")
        task_id = payload.get("task_id")
        board = payload.get("board")
        run_id = payload.get("run_id")
        claim_lock = payload.get("claim_lock")
        operation = payload.get("operation")
        fingerprint = payload.get("operation_fingerprint")
        paths = payload.get("paths")
        summary = payload.get("summary")
        if not isinstance(request_id, str) or not _REQUEST_ID_RE.fullmatch(request_id):
            raise ValueError("invalid request_id")
        if not isinstance(task_id, str) or not task_id.startswith("t_") or len(task_id) > 128:
            raise ValueError("invalid task_id")
        if not isinstance(board, str) or not _BOARD_RE.fullmatch(board):
            raise ValueError("invalid board")
        if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0:
            raise ValueError("invalid run_id")
        if (
            not isinstance(claim_lock, str)
            or not 1 <= len(claim_lock) <= 256
            or any(ord(char) < 0x20 or ord(char) == 0x7F for char in claim_lock)
        ):
            raise ValueError("invalid claim_lock")
        if operation not in {"patch", "write_file"}:
            raise ValueError("unsupported protected operation")
        if not isinstance(fingerprint, str) or not _FINGERPRINT_RE.fullmatch(fingerprint):
            raise ValueError("invalid operation fingerprint")
        if (
            not isinstance(paths, list)
            or not 1 <= len(paths) <= 16
            or any(not isinstance(path, str) or not path or len(path) > 1024 for path in paths)
        ):
            raise ValueError("invalid protected paths")
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 500:
            raise ValueError("invalid approval summary")
        try:
            timeout = float(payload.get("timeout_seconds", 300.0))
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid approval timeout") from exc
        if not 0.1 <= timeout <= 3600.0:
            raise ValueError("invalid approval timeout")
        return {
            "schema_version": 1,
            "request_id": request_id,
            "task_id": task_id,
            "board": board,
            "run_id": run_id,
            "claim_lock": claim_lock,
            "operation": operation,
            "paths": list(paths),
            "operation_fingerprint": fingerprint,
            "summary": summary.strip(),
            "timeout_seconds": timeout,
        }


    @staticmethod
    def _notification(record: _PendingRequest) -> str:
        request = record.request
        paths = ", ".join(request["paths"])
        return (
            "Protected write approval requested by Kanban task "
            f"{request['task_id']}.\n"
            f"{request['summary']}\n"
            f"Operation: {request['operation']}\n"
            f"Paths: {paths}\n"
            "Approve or deny this exact request using the authenticated API run "
            "approval controls; ordinary chat text cannot decide it.\n"
            f"Request {request['request_id']}; fingerprint "
            f"{request['operation_fingerprint'][:12]}."
        )

    def submit(self, payload: dict) -> dict:
        try:
            request = self._validated_request(payload)
        except ValueError as exc:
            return {"ok": False, "status": "invalid", "error": str(exc)}
        try:
            run_is_current = self._validate_run(request)
        except Exception:
            run_is_current = False
            logger.warning(
                "protected write run validation failed task=%s run=%s",
                request["task_id"],
                request["run_id"],
                exc_info=True,
            )
        if not run_is_current:
            logger.info(
                "protected write approval rejected task=%s run=%s request=%s reason=invalid_run",
                request["task_id"],
                request["run_id"],
                request["request_id"],
            )
            return {"ok": False, "status": "invalid_run"}
        target = self._resolve_target(request)
        if target is None or target.task_id != request["task_id"]:
            return {"ok": False, "status": "no_operator"}
        if target.platform not in {"webui", "api_server"}:
            return {"ok": False, "status": "ineligible_operator"}
        correlation = (request["request_id"], request["operation_fingerprint"])
        now = self._cleanup()
        with self._lock:
            if correlation in self._consumed:
                return {"ok": False, "status": "replayed"}
            existing = self._pending.get(request["request_id"])
            if existing is not None:
                if existing.request["operation_fingerprint"] != request["operation_fingerprint"]:
                    return {"ok": False, "status": "mismatch"}
                return self._snapshot(existing, consume=False)
            record = _PendingRequest(
                request=request,
                target=target,
                approval_session_key=f"protected-write:{request['request_id']}",
                deadline_monotonic=now + request["timeout_seconds"],
            )
            self._pending[request["request_id"]] = record

        thread = threading.Thread(
            target=self._wait_for_decision,
            args=(record,),
            name=f"protected-write-approval-{request['request_id'][:24]}",
            daemon=True,
        )
        thread.start()
        session_ref = hashlib.sha256(target.session_id.encode("utf-8")).hexdigest()[:12]
        logger.info(
            "protected write approval submitted task=%s run=%s request=%s fingerprint=%s profile=%s session_ref=%s",
            request["task_id"],
            request["run_id"],
            request["request_id"],
            request["operation_fingerprint"][:12],
            target.profile,
            session_ref,
        )
        return self._snapshot(record, consume=False)

    def _wait_for_decision(self, record: _PendingRequest) -> None:
        request = record.request

        def notify(_approval_data: dict) -> None:
            notice = {
                "request_id": request["request_id"],
                "task_id": request["task_id"],
                "worker_run_id": request["run_id"],
                "operation_fingerprint": request["operation_fingerprint"],
                "operation": request["operation"],
                "paths": list(request["paths"]),
                "summary": request["summary"],
                "timeout_seconds": request["timeout_seconds"],
                "deadline_monotonic": record.deadline_monotonic,
                "approval_session_key": record.approval_session_key,
                "message": self._notification(record),
            }
            if not self._notify(record.target, notice):
                raise RuntimeError("operator notification was not delivered")

        result = _await_gateway_decision(
            session_key=record.approval_session_key,
            approval_data={
                "request_id": request["request_id"],
                "command": request["summary"],
                "description": request["summary"],
                "operation_fingerprint": request["operation_fingerprint"],
            },
            notify_cb=notify,
            surface="protected_write_bridge",
        )
        with self._lock:
            current = self._pending.get(request["request_id"])
            if current is not record:
                return
            if record.status != "pending":
                return
            if not result.get("resolved"):
                record.status = "timeout"
                record.choice = "deny"
            else:
                record.choice = result.get("choice")
                record.reason = result.get("reason")
                record.status = "approved" if record.choice == "once" else "denied"
            record.terminal_at_monotonic = self._monotonic()


    def _snapshot(self, record: _PendingRequest, *, consume: bool) -> dict:
        result = {
            "ok": True,
            "request_id": record.request["request_id"],
            "operation_fingerprint": record.request["operation_fingerprint"],
            "status": record.status,
        }
        if record.choice is not None:
            result["choice"] = record.choice
        if consume and record.status != "pending":
            result["consumed"] = True
        return result

    def poll(self, payload: dict) -> dict:
        request_id = payload.get("request_id") if isinstance(payload, dict) else None
        fingerprint = payload.get("operation_fingerprint") if isinstance(payload, dict) else None
        now = self._cleanup()
        with self._lock:
            record = self._pending.get(request_id) if isinstance(request_id, str) else None
            if record is None or record.request["operation_fingerprint"] != fingerprint:
                return {"ok": False, "status": "stale"}
            try:
                run_is_current = self._validate_run(record.request)
            except Exception:
                run_is_current = False
                logger.warning(
                    "protected write run revalidation failed task=%s run=%s",
                    record.request["task_id"],
                    record.request["run_id"],
                    exc_info=True,
                )
            if not run_is_current:
                record.status = "invalid_run"
                record.choice = None
                record.reason = "run_reclaimed"
                record.terminal_at_monotonic = now
                resolve_gateway_approval(
                    record.approval_session_key,
                    "deny",
                    request_id=record.request["request_id"],
                )
            result = self._snapshot(record, consume=record.status != "pending")
            if record.status != "pending":
                session_ref = hashlib.sha256(
                    record.target.session_id.encode("utf-8")
                ).hexdigest()[:12]
                logger.info(
                    "protected write approval consumed task=%s run=%s request=%s fingerprint=%s profile=%s session_ref=%s status=%s choice=%s",
                    record.request["task_id"],
                    record.request["run_id"],
                    record.request["request_id"],
                    record.request["operation_fingerprint"][:12],
                    record.target.profile,
                    session_ref,
                    record.status,
                    record.choice,
                )
                self._pending.pop(record.request["request_id"], None)
                self._consumed[
                    (record.request["request_id"], record.request["operation_fingerprint"])
                ] = now
                self._cleanup_locked(now)
            return result

    def cancel(self, payload: dict) -> dict:
        request_id = payload.get("request_id") if isinstance(payload, dict) else None
        fingerprint = payload.get("operation_fingerprint") if isinstance(payload, dict) else None
        self._cleanup()
        with self._lock:
            record = self._pending.get(request_id) if isinstance(request_id, str) else None
            if record is None or record.request["operation_fingerprint"] != fingerprint:
                return {"ok": False, "status": "stale"}
        resolve_gateway_approval(
            record.approval_session_key,
            "deny",
            request_id=record.request["request_id"],
        )
        reason = (
            "worker_timeout"
            if payload.get("reason") == "worker_timeout"
            else "caller_cancelled"
        )
        session_ref = hashlib.sha256(
            record.target.session_id.encode("utf-8")
        ).hexdigest()[:12]
        logger.info(
            "protected write approval cancelled task=%s request=%s fingerprint=%s profile=%s session_ref=%s reason=%s",
            record.request["task_id"],
            record.request["request_id"],
            record.request["operation_fingerprint"][:12],
            record.target.profile,
            session_ref,
            reason,
        )
        return {"ok": True, "status": "cancelled"}


def create_runtime_bridge(runner, loop) -> ProtectedApprovalBridge:
    """Bind the broker to the gateway's Kanban subscriptions and API adapter."""

    def api_adapter_for_profile(profile: str):
        from gateway.config import Platform

        adapter = runner._authorization_adapter(Platform.API_SERVER, profile)
        if adapter is not None:
            return adapter
        config = getattr(runner, "config", None)
        if not getattr(config, "multiplex_profiles", False):
            return None
        try:
            from hermes_cli.profiles import profiles_to_serve

            served = {
                name
                for name, _home in profiles_to_serve(
                    multiplex=True,
                    profile_allowlist=getattr(
                        config, "multiplex_profile_allowlist", None
                    ),
                )
            }
        except Exception:
            return None
        if profile not in served:
            return None
        # The API server is the one intentional shared listener in multiplex
        # mode; /p/<profile> performs the profile authorization and scoping.
        return getattr(runner, "adapters", {}).get(Platform.API_SERVER)

    def validate_run(request: dict) -> bool:
        from hermes_cli import kanban_db as kb

        try:
            conn = kb.connect(board=request["board"])
            try:
                task = kb.get_task(conn, request["task_id"])
            finally:
                conn.close()
        except Exception:
            logger.warning(
                "protected approval could not validate Kanban task=%s run=%s board=%s",
                request.get("task_id"),
                request.get("run_id"),
                request.get("board"),
                exc_info=True,
            )
            return False
        if task is None or task.status != "running":
            return False
        if task.current_run_id != request["run_id"] or not task.claim_lock:
            return False
        if not hmac.compare_digest(task.claim_lock, request["claim_lock"]):
            return False
        return task.claim_expires is not None and task.claim_expires >= int(time.time())

    def resolve_target(request: dict) -> Optional[OperatorTarget]:
        from hermes_cli import kanban_db as kb

        task_id = request["task_id"]
        board = request["board"]
        candidates: list[OperatorTarget] = []
        try:
            conn = kb.connect(board=board)
            try:
                subscriptions = kb.list_notify_subs(conn, task_id=task_id)
            finally:
                conn.close()
        except Exception:
            logger.warning(
                "protected approval could not read Kanban board %s",
                board,
                exc_info=True,
            )
            return None
        for subscription in subscriptions:
            platform = str(subscription.get("platform") or "").lower()
            if platform not in {"webui", "api_server"}:
                continue
            profile = str(subscription.get("notifier_profile") or "default")
            session_id = str(subscription.get("chat_id") or "")
            if not session_id:
                continue
            adapter = api_adapter_for_profile(profile)
            if (
                adapter is None
                or getattr(adapter, "supports_async_delivery", True) is not False
            ):
                continue
            candidates.append(
                OperatorTarget(
                    profile=profile,
                    session_id=session_id,
                    platform=platform,
                    task_id=task_id,
                )
            )
        # A choice between conversations is an authorization decision.  Never
        # guess when zero or multiple eligible origin sessions exist.
        unique = {
            (target.profile, target.session_id, target.platform): target
            for target in candidates
        }
        return next(iter(unique.values())) if len(unique) == 1 else None

    def notify(target: OperatorTarget, notice: dict) -> bool:
        from gateway.config import Platform

        direct_adapter = runner._authorization_adapter(
            Platform.API_SERVER, target.profile
        )
        adapter = direct_adapter or api_adapter_for_profile(target.profile)
        if adapter is None:
            return False
        present = getattr(adapter, "present_protected_approval", None)
        if not callable(present):
            return False
        future = __import__("asyncio").run_coroutine_threadsafe(
            present(
                profile=target.profile,
                session_id=target.session_id,
                approval_session_key=notice.get("approval_session_key"),
                approval_data=notice,
            ),
            loop,
        )
        try:
            delivered = future.result(timeout=30.0)
        except Exception:
            future.cancel()
            logger.warning(
                "protected approval notification failed task=%s profile=%s",
                target.task_id,
                target.profile,
                exc_info=True,
            )
            return False
        return delivered is True

    return ProtectedApprovalBridge(
        resolve_target=resolve_target,
        notify=notify,
        validate_run=validate_run,
    )
