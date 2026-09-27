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
from cron.scheduler import (  # noqa: F401  (re-exported for import compatibility)
    _launch_external_cron_worker,
    _run_external_worker_payload,
)

__all__ = ["_launch_external_cron_worker", "_run_external_worker_payload"]
