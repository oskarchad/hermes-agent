"""Domain-neutral transactional policy seam for trusted workflow adapters.

Public task payloads cannot issue authority. Adapters are installed explicitly on
one connection; absent adapters deny only their workflow and issue recovery.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import asdict, dataclass
import json
from typing import Protocol
from weakref import WeakKeyDictionary

from hermes_cli.kanban_db_connect import write_txn
from hermes_cli import kanban_governance_store as store


class GovernanceDenied(ValueError):
    pass


class EvidenceModeMismatch(GovernanceDenied):
    pass


@dataclass(frozen=True)
class Binding:
    workflow_id: str
    contract: str
    stage: str
    target: str
    fingerprint: str
    role: str
    evidence_mode: str


@dataclass(frozen=True)
class WorkOrder:
    binding: Binding
    kind: str
    title: str
    assignee: str
    parents: tuple[str, ...]
    budget_charge: str | None = None


@dataclass(frozen=True)
class TransitionDisposition:
    allowed: bool
    reason: str
    work: tuple[WorkOrder, ...] = ()


@dataclass(frozen=True)
class ClaimIdentity:
    task_id: str
    run_id: int
    profile: str
    claim_lock: str


@dataclass(frozen=True)
class BudgetDecision:
    lineage_id: str
    source_id: str
    source_sha256: str
    limits: dict[str, int]
    used: dict[str, int]
    finding_ids: tuple[str, ...]


class Validator(Protocol):
    def evaluate(self, conn, task_id: str, operation: str) -> TransitionDisposition: ...
    def reconcile(self, conn, workflow_id: str) -> tuple[WorkOrder, ...]: ...


_validators = WeakKeyDictionary()
_issuance = ContextVar("kanban_governance_issuance", default=None)


def register_validator(conn, contract: str, validator: Validator) -> None:
    conn.execute("SELECT 1")  # Closed connections cannot retain authority.
    registry = _validators.setdefault(conn, {})
    if contract in registry and registry[contract] is not validator:
        raise GovernanceDenied("different validator already registered")
    registry[contract] = validator


def unregister_validators(conn):
    _validators.pop(conn, None)


def target_key(binding: Binding):
    return store.digest([binding.workflow_id, binding.contract, binding.stage,
                         binding.target, binding.fingerprint])


def action_key(order: WorkOrder):
    return store.digest([order.kind, target_key(order.binding)])


def reserve_tx(conn, order: WorkOrder):
    if not conn.in_transaction:
        raise RuntimeError("reservation requires transaction")
    conn.execute(
        "INSERT OR IGNORE INTO kanban_dispositions "
        "(action_key,workflow_id,kind,target_key,payload_json,state) VALUES (?,?,?,?,?,'pending')",
        (action_key(order), order.binding.workflow_id, order.kind, target_key(order.binding),
         store.canonical_bytes(asdict(order)).decode()),
    )


def recovery_tx(conn, workflow_id, reason):
    wf = store.workflow(conn, workflow_id)
    order = WorkOrder(Binding(workflow_id, wf["contract"] or "unregistered", "recovery",
                             workflow_id, store.digest([workflow_id, reason]), "recovery",
                             wf["evidence_mode"]), "recovery", "recovery:" + reason, "otto", ())
    reserve_tx(conn, order)
    return TransitionDisposition(False, reason, (order,))


def bind_created_tx(conn, task_id, parents):
    capability = _issuance.get()
    if capability is not None and capability[0] is conn:
        order = capability[1]
        binding = asdict(order.binding)
        source = action_key(order)
    else:
        inherited = [store.binding(conn, parent) for parent in parents]
        inherited = [b for b in inherited if b]
        if not inherited:
            return
        binding = dict(inherited[0], role="unclassified")
        source = None
        for b in inherited:
            if b["workflow_id"] != binding["workflow_id"]:
                recovery_tx(conn, b["workflow_id"], "conflicting membership")
        recovery_tx(conn, binding["workflow_id"], "unclassified descendant")
    wf = store.workflow(conn, binding["workflow_id"])
    conn.execute("INSERT INTO kanban_task_bindings VALUES (?,?,?,?,?)",
                 (task_id, binding["workflow_id"], store.canonical_bytes(binding).decode(), source, wf["revision"]))


def evaluate_tx(conn, task_id, operation, *, expected_run_id=None):
    if not conn.in_transaction:
        raise RuntimeError("evaluation requires transaction")
    binding = store.binding(conn, task_id)
    if binding is None:
        return TransitionDisposition(True, "not governed")
    if binding["role"] == "recovery":
        return TransitionDisposition(True, "independent recovery")
    wf_id = binding["workflow_id"]
    if binding["role"] == "unclassified":
        return recovery_tx(conn, wf_id, "unclassified descendant")
    validator = _validators.get(conn, {}).get(binding["contract"])
    if validator is None:
        return recovery_tx(conn, wf_id, "validator unavailable")
    try:
        result = validator.evaluate(conn, task_id, operation)
    except (ValueError, KeyError, TypeError, OSError):
        return recovery_tx(conn, wf_id, "validator error")
    for order in result.work:
        reserve_tx(conn, order)
    if not result.allowed:
        recovery_tx(conn, wf_id, result.reason)
    return result


def record_transition_tx(conn, task_id, operation, run_id):
    binding = store.binding(conn, task_id)
    if binding and binding["role"] != "recovery":
        _reconcile_tx(conn, binding["workflow_id"])


def _reconcile_tx(conn, workflow_id):
    wf = store.workflow(conn, workflow_id)
    validator = _validators.get(conn, {}).get(wf["contract"])
    if validator is None or wf["state"] != "active":
        recovery_tx(conn, workflow_id, "validator unavailable")
        return
    try:
        work = validator.reconcile(conn, workflow_id)
    except (ValueError, KeyError, TypeError, OSError):
        recovery_tx(conn, workflow_id, "validator error")
        return
    for order in work:
        reserve_tx(conn, order)


def drain(conn, *, limit=64):
    from hermes_cli import kanban_db as kb

    issued = []
    rows = conn.execute("SELECT action_key FROM kanban_dispositions WHERE state='pending' LIMIT ?", (limit,)).fetchall()
    for row in rows:
        with write_txn(conn):
            current = conn.execute("SELECT * FROM kanban_dispositions WHERE action_key=?", (row[0],)).fetchone()
            if current["state"] == "applied":
                issued.append(current["task_id"])
                continue
            raw = json.loads(current["payload_json"])
            raw["binding"] = Binding(**raw["binding"])
            raw["parents"] = tuple(raw["parents"])
            order = WorkOrder(**raw)
            wf = store.workflow(conn, order.binding.workflow_id)
            if order.binding.role != "recovery" and (
                wf["state"] != "active" or order.binding.contract not in _validators.get(conn, {})
            ):
                recovery_tx(conn, wf["workflow_id"], "validator unavailable")
                continue
            if order.budget_charge and not store.charge_tx(conn, wf["lineage_id"], order.budget_charge):
                recovery_tx(conn, wf["workflow_id"], "budget exhausted")
                continue
            token = _issuance.set((conn, order))
            try:
                task_id = kb.create_task(conn, title=order.title, assignee=order.assignee,
                                         parents=order.parents, created_by="governance",
                                         workspace_kind="dir", workspace_path=store.scope(conn, wf["workflow_id"])["workspace"])
            finally:
                _issuance.reset(token)
            conn.execute("UPDATE kanban_dispositions SET task_id=?,state='applied' WHERE action_key=?",
                         (task_id, row[0]))
            issued.append(task_id)
    return tuple(issued)


def reconcile(conn, *, now, limit=64):
    with write_txn(conn):
        for row in conn.execute("SELECT workflow_id FROM kanban_workflows LIMIT ?", (limit,)).fetchall():
            _reconcile_tx(conn, row[0])
    drain(conn, limit=limit)


def consume_audit_result(conn, task_id):
    binding = store.binding(conn, task_id)
    with write_txn(conn):
        _reconcile_tx(conn, binding["workflow_id"])
    return drain(conn)
