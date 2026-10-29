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

import hermes_cli.kanban_db as _kb


def worker_logs_dir(board: Optional[str]=None) -> Path:
    """Per-board worker log dir (logs follow the board so ``hermes kanban log``
    is unambiguous when two boards share a task id)."""
    return _kb._board_path(None, board, ('kanban', 'logs'), 'logs')


def worker_log_path(task_id: str, *, board: Optional[str]=None) -> Path:
    """Worker log path (may not exist). The dispatcher always passes ``board``
    explicitly to avoid resolution ambiguity."""
    return _kb.worker_logs_dir(board=board) / f'{task_id}.log'
