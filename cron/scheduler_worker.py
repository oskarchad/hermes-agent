"""Compatibility shim: the external cron worker entry points live in ``cron.scheduler``.

This module used to carry its own copy of ``_launch_external_cron_worker`` and
``_run_external_worker_payload``. Upstream now owns both in ``cron/scheduler.py``,
and that copy is the one that tracks upstream's evolving call contracts (for
example ``restart_safe_gateway_child_argv``'s ``require_restart_safe_scope``
keyword). Keeping a second implementation here meant the duplicate silently kept
calling the old signature — exactly the "do not duplicate upstream code" failure
ADR 0002 exists to prevent.

The module stays as a re-export so existing imports and monkeypatch targets keep
working; it must never grow a second implementation again.
"""
from cron import scheduler


def _launch_external_cron_worker(job, *, adapters=None):
    return scheduler._launch_external_cron_worker(job, adapters=adapters)


def _run_external_worker_payload(payload_path, ack_path):
    return scheduler._run_external_worker_payload(payload_path, ack_path)


__all__ = ["_launch_external_cron_worker", "_run_external_worker_payload"]
