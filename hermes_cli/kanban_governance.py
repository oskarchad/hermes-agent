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
    needs_input: str | None = None


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
        if order.needs_input:
            from hermes_cli import kanban_db as kb
            conn.execute("UPDATE tasks SET block_kind='needs_input' WHERE id=?", (task_id,))
            kb._append_event(conn, task_id, "blocked", {"kind": "needs_input", "reason": order.needs_input})
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


def bind_linked_tx(conn, parent_id, child_id):
    parent = store.binding(conn, parent_id)
    child = store.binding(conn, child_id)
    if parent is None:
        return
    if child is None:
        bind_created_tx(conn, child_id, (parent_id,))
    elif child["workflow_id"] != parent["workflow_id"]:
        recovery_tx(conn, parent["workflow_id"], "conflicting membership")
        recovery_tx(conn, child["workflow_id"], "conflicting membership")
        child["role"] = "unclassified"
        conn.execute("UPDATE kanban_task_bindings SET binding_json=?,source_action=NULL WHERE task_id=?",
                     (store.canonical_bytes(child).decode(), child_id))
    for row in conn.execute("SELECT child_id FROM task_links WHERE parent_id=?", (child_id,)).fetchall():
        bind_linked_tx(conn, child_id, row[0])


def evaluate_tx(conn, task_id, operation, *, expected_run_id=None):
    if not conn.in_transaction:
        raise RuntimeError("evaluation requires transaction")
    binding = store.binding(conn, task_id)
    if binding is None:
        known = conn.execute("SELECT workflow_id FROM kanban_dispositions WHERE task_id=?", (task_id,)).fetchone()
        if known:
            return recovery_tx(conn, known[0], "issued binding missing")
        return TransitionDisposition(True, "not governed")
    if binding["role"] == "recovery":
        return TransitionDisposition(True, "independent recovery")
    wf_id = binding["workflow_id"]
    if operation == "decompose":
        return recovery_tx(conn, wf_id, "decomposition requires trusted issuance")
    if operation.startswith("assign:"):
        source = conn.execute(
            "SELECT d.payload_json FROM kanban_task_bindings b "
            "JOIN kanban_dispositions d ON d.action_key=b.source_action WHERE b.task_id=?",
            (task_id,),
        ).fetchone()
        if source is None or json.loads(source[0])["assignee"] != operation[len("assign:"):]:
            return recovery_tx(conn, wf_id, "issued owner change denied")
    if binding["role"] == "unclassified":
        return recovery_tx(conn, wf_id, "unclassified descendant")
    validator = _validators.get(conn, {}).get(binding["contract"])
    if validator is None:
        return recovery_tx(conn, wf_id, "validator unavailable")
    try:
        result = validator.evaluate(conn, task_id, operation)
    except (ValueError, KeyError, TypeError, OSError, RuntimeError):
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
    except (ValueError, KeyError, TypeError, OSError, RuntimeError):
        recovery_tx(conn, workflow_id, "validator error")
        return
    for order in work:
        reserve_tx(conn, order)


def _next_batch_tx(conn, lane, limit):
    table, key, condition = {
        "workflows": ("kanban_workflows", "workflow_id", "1"),
        "dispositions": ("kanban_dispositions", "action_key", "state='pending'"),
    }[lane]
    cursor = conn.execute("SELECT last_rowid FROM kanban_governance_cursors WHERE lane=?", (lane,)).fetchone()
    after = cursor[0] if cursor else 0
    rows = conn.execute(f"SELECT {key},rowid FROM {table} WHERE {condition} "
                        "ORDER BY (rowid > ?) DESC, rowid LIMIT ?", (after, limit)).fetchall()
    if rows:
        conn.execute("INSERT INTO kanban_governance_cursors VALUES (?,?) "
                     "ON CONFLICT(lane) DO UPDATE SET last_rowid=excluded.last_rowid", (lane, rows[-1][1]))
    return rows


def drain(conn, *, limit=64):
    from hermes_cli import kanban_db as kb

    issued = []
    with write_txn(conn):
        rows = _next_batch_tx(conn, "dispositions", limit)
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
                                         initial_status="blocked" if order.needs_input else "running",
                                         workspace_kind="dir", workspace_path=store.scope(conn, wf["workflow_id"])["workspace"])
            finally:
                _issuance.reset(token)
            conn.execute("UPDATE kanban_dispositions SET task_id=?,state='applied' WHERE action_key=?",
                         (task_id, row[0]))
            issued.append(task_id)
    return tuple(issued)


def _observe_liveness_tx(conn, workflow_id, now):
    from hermes_cli import kanban_db as kb

    policy = store.scope(conn, workflow_id).get("liveness")
    if not policy:
        return
    interval = policy["report_interval"]
    if not isinstance(interval, int) or interval <= 0:
        recovery_tx(conn, workflow_id, "invalid reporting interval")
        return
    rows = conn.execute("SELECT t.* FROM tasks t JOIN kanban_task_bindings b ON b.task_id=t.id "
                        "WHERE b.workflow_id=?", (workflow_id,)).fetchall()
    for row in rows:
        task_id = row["id"]
        if store.binding(conn, task_id)["role"] == "recovery":
            continue
        latest = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='governance_status' "
                              "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
        previous = json.loads(latest[0]) if latest else None
        if row["status"] in {"done", "archived"}:
            state, parents = "resolved", []
            if previous is None or previous["state"] == state:
                continue
        else:
            if now < row["created_at"] + interval:
                continue
            parents = [p[0] for p in conn.execute("SELECT p.id FROM task_links l JOIN tasks p ON p.id=l.parent_id "
                                                  "WHERE l.child_id=? AND p.status!='done'", (task_id,))]
            state = "parents" if parents else "queue"
            if row["status"] == "running":
                heartbeat = row["last_heartbeat_at"] or row["started_at"] or row["created_at"]
                state = "heartbeat_gap" if now - heartbeat > policy["heartbeat_gap"] else "active"
            if row["status"] == "blocked":
                state = "blocked"
        checkpoint = row["created_at"] + ((now - row["created_at"]) // interval + 1) * interval
        if previous and previous["state"] == state and previous["next_checkpoint"] == checkpoint:
            continue
        actions = {"parents": "inspect prerequisite tasks", "queue": "check routing and capacity",
                   "active": "await next heartbeat; do not terminate", "heartbeat_gap": "diagnose worker identity",
                   "blocked": "resolve operator-owned block", "resolved": "continue current governed chain"}
        kb._append_event(conn, task_id, "governance_status",
                         {"workflow_id": workflow_id, "state": state, "parents": parents,
                          "impact": "workflow progress observation", "action": actions[state],
                          "next_checkpoint": checkpoint}, run_id=row["current_run_id"])
        if state != "resolved":
            recovery_tx(conn, workflow_id, "report deadline:" + task_id + ":" + state)


def reconcile(conn, *, now, limit=64):
    with write_txn(conn):
        for row in _next_batch_tx(conn, "workflows", limit):
            _reconcile_tx(conn, row[0])
            _observe_liveness_tx(conn, row[0], now)
    drain(conn, limit=limit)


def consume_audit_result(conn, task_id):
    binding = store.binding(conn, task_id)
    with write_txn(conn):
        _reconcile_tx(conn, binding["workflow_id"])
    return drain(conn)
