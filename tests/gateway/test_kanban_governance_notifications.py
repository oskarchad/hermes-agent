"""Governance notices use actual subscriptions, cursors and notifier delivery."""
import asyncio

from gateway.config import Platform
from gateway.kanban_watchers import GatewayKanbanWatchersMixin
from gateway.kanban_watchers_notifier import _KanbanNotification, _notifier_collect
from gateway.platforms.base import SendResult
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_notify as notify
from hermes_cli.kanban_db_connect import connect, write_txn


class Sink:
    def __init__(self):
        self.messages = []
        self.fail = True

    async def send(self, chat_id, text, metadata=None):
        if self.fail:
            self.fail = False
            return SendResult(success=False, error="fixture unavailable")
        self.messages.append((chat_id, text))
        return SendResult(success=True, message_id="simulation-message")


class Runner(GatewayKanbanWatchersMixin):
    def __init__(self, sink):
        self.adapters = {Platform.TELEGRAM: sink}
        self._kanban_dispatcher_lock_handle = object()

    def _authorization_adapter(self, platform, profile):
        return self.adapters[platform]


async def tick(runner, failures):
    deliveries = _notifier_collect(runner, kb, notifier_profile=None, gc_due=False, gc_retention_days=30)
    for delivery in deliveries:
        await _KanbanNotification(runner, delivery, platform_cls=Platform, sub_fail_counts=failures).deliver()


def test_governance_notice_delivery_retries_cursor_not_work(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    conn = connect()
    task = kb.create_task(conn, title="waiting fixture", assignee="bob")
    for recipient in ("otto", "oskar"):
        notify.add_notify_sub(conn, task_id=task, platform="telegram", chat_id=recipient)
    with write_txn(conn):
        kb._append_event(conn, task, "governance_status", {"state": "queue", "impact": "audit pending",
                         "action": "check capacity", "next_checkpoint": "20"})
    sink = Sink()
    runner, failures = Runner(sink), {}
    before = len(kb.list_tasks(conn))
    asyncio.run(tick(runner, failures))
    failure_observed = sum(failures.values())
    assert failure_observed == 1
    asyncio.run(tick(runner, failures))
    assert {recipient for recipient, _ in sink.messages} == {"otto", "oskar"}
    assert len(sink.messages) == 2
    assert all("check capacity" in text and "20" in text for _, text in sink.messages)
    asyncio.run(tick(runner, failures))
    assert len(sink.messages) == 2
    assert len(kb.list_tasks(conn)) == before
    assert failures == {}
    import json
    (tmp_path / "notifications.json").write_text(json.dumps({"messages": sink.messages,
        "failure_observed": failure_observed, "remaining_failures": failures, "task_id": task}))
    conn.close()
