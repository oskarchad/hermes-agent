"""Transactional governance storage; no domain, model or transport dependencies."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS kanban_workflows (
        workflow_id TEXT PRIMARY KEY, intake_kind TEXT NOT NULL, contract TEXT,
        decision_id TEXT NOT NULL, lineage_id TEXT NOT NULL,
        evidence_mode TEXT NOT NULL CHECK(evidence_mode IN ('simulation','real')),
        state TEXT NOT NULL CHECK(state IN ('active','recovery_required')),
        revision INTEGER NOT NULL DEFAULT 1, scope_json TEXT NOT NULL,
        intake_sha256 TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS kanban_task_bindings (
        task_id TEXT PRIMARY KEY REFERENCES tasks(id),
        workflow_id TEXT NOT NULL REFERENCES kanban_workflows(workflow_id),
        binding_json TEXT NOT NULL, source_action TEXT, issued_revision INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS kanban_dispositions (
        action_key TEXT PRIMARY KEY,
        workflow_id TEXT NOT NULL REFERENCES kanban_workflows(workflow_id),
        kind TEXT NOT NULL, target_key TEXT NOT NULL, payload_json TEXT NOT NULL,
        task_id TEXT REFERENCES tasks(id),
        state TEXT NOT NULL CHECK(state IN ('pending','applied')),
        UNIQUE(workflow_id,kind,target_key))""",
    """CREATE TABLE IF NOT EXISTS kanban_invocations (
        invocation_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
        run_id INTEGER NOT NULL REFERENCES task_runs(id),
        workflow_id TEXT NOT NULL REFERENCES kanban_workflows(workflow_id),
        binding_json TEXT NOT NULL, profile TEXT NOT NULL, claim_lock TEXT NOT NULL,
        evidence_mode TEXT NOT NULL CHECK(evidence_mode IN ('simulation','real')),
        request_sha256 TEXT NOT NULL, artifacts_json TEXT NOT NULL,
        response_sha256 TEXT, runner_version TEXT NOT NULL,
        verdict TEXT CHECK(verdict IN ('PASS','FAIL','UNRESOLVED')),
        state TEXT NOT NULL CHECK(state IN ('started','finished','rejected')),
        UNIQUE(task_id,run_id))""",
    """CREATE TABLE IF NOT EXISTS kanban_lineage_budget (
        lineage_id TEXT NOT NULL, category TEXT NOT NULL CHECK(category IN ('repair','reaudit')),
        source_id TEXT NOT NULL, source_sha256 TEXT NOT NULL,
        limit_count INTEGER NOT NULL CHECK(limit_count >= 0),
        used_count INTEGER NOT NULL CHECK(used_count >= 0 AND used_count <= limit_count),
        PRIMARY KEY(lineage_id,category))""",
)


def init_schema(conn):
    for statement in _SCHEMA:
        conn.execute(statement)


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def workflow(conn, workflow_id):
    row = conn.execute("SELECT * FROM kanban_workflows WHERE workflow_id=?", (workflow_id,)).fetchone()
    return dict(row) if row else None


def scope(conn, workflow_id):
    return json.loads(workflow(conn, workflow_id)["scope_json"])


def binding(conn, task_id):
    row = conn.execute("SELECT binding_json FROM kanban_task_bindings WHERE task_id=?", (task_id,)).fetchone()
    return json.loads(row[0]) if row else None


def object_path(conn, task_id, sha256):
    if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
        raise ValueError("invalid content digest")
    if not task_id.startswith("t_") or any(c not in "0123456789abcdef" for c in task_id[2:]):
        raise ValueError("invalid task identity")
    db = Path(conn.execute("PRAGMA database_list").fetchone()[2]).resolve(strict=True)
    root = db.parent / "kanban" / "attachments"
    path = root / task_id / "guardian" / sha256
    if path.resolve().is_relative_to(root.resolve()) is False:
        raise ValueError("artifact outside attachment root")
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("symlink artifact")
    return path


def put_object(conn, task_id, value: bytes):
    sha = hashlib.sha256(value).hexdigest()
    path = object_path(conn, task_id, sha)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as output:
            output.write(value)
    except FileExistsError:
        if path.read_bytes() != value:
            raise ValueError("immutable object changed")
    return sha


def read_object(conn, task_id, sha):
    value = object_path(conn, task_id, sha).read_bytes()
    if hashlib.sha256(value).hexdigest() != sha:
        raise ValueError("artifact content changed")
    return value


def mark_transport_unresolved(conn, invocation_id):
    from hermes_cli.kanban_db import _append_event
    from hermes_cli.kanban_db_connect import write_txn

    with write_txn(conn):
        row = conn.execute("SELECT * FROM kanban_invocations WHERE invocation_id=?", (invocation_id,)).fetchone()
        if row is None or row["state"] != "started":
            raise ValueError("invocation is not started")
        conn.execute("UPDATE kanban_invocations SET state='finished',verdict='UNRESOLVED' WHERE invocation_id=?",
                     (invocation_id,))
        _append_event(conn, row["task_id"], "governance_transport_unresolved",
                      {"invocation_id": invocation_id}, run_id=row["run_id"])


def charge_tx(conn, lineage_id, category):
    if category not in {"repair", "reaudit"}:
        raise ValueError("unknown budget category")
    return conn.execute(
        "UPDATE kanban_lineage_budget SET used_count=used_count+1 "
        "WHERE lineage_id=? AND category=? AND used_count < limit_count",
        (lineage_id, category),
    ).rowcount == 1
