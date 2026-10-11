"""Retained runtime contracts, separated from the native facade."""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import secrets
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Optional

import hermes_cli.kanban_db_dispatch as _kbd
from hermes_cli import kanban_db as _kb


def worker_log_rotation_config(kanban_cfg: Optional[dict]=None) -> tuple[int, int]:
    """Return ``(rotate_bytes, backup_count)`` for worker log rotation.
    Defaults: rotate at 2 MiB, keep one backup (``.log.1``); both overridable
    from ``config.yaml``.
    """
    if kanban_cfg is None:
        try:
            from hermes_cli.config import load_config
            kanban_cfg = load_config().get('kanban') or {}
        except Exception:
            kanban_cfg = {}
    kanban_cfg = kanban_cfg or {}
    max_bytes = _kbd._positive_int(kanban_cfg.get('worker_log_rotate_bytes'), _kbd.DEFAULT_LOG_ROTATE_BYTES, minimum=1)
    backup_count = _kbd._positive_int(kanban_cfg.get('worker_log_backup_count'), _kbd.DEFAULT_LOG_BACKUP_COUNT, minimum=0)
    return (max_bytes, backup_count)


