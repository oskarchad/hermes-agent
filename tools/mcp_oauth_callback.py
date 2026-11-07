"""Cancellable console input and atomic terminal results for MCP OAuth callbacks."""
from __future__ import annotations

import os
import sys
import threading
from typing import Any

_PASTE_READER_POLL_INTERVAL_SECONDS = 0.05


def latch_callback_result(result: dict, **values: Any) -> bool:
    """HTTP workers and the paste reader share one first-terminal-result latch."""
    with result.setdefault("_lock", threading.Lock()):
        if result.get("auth_code") is not None or result.get("error") is not None:
            return False
        result.update(values)
        return True


def _read_windows_paste_line(stop_event: threading.Event) -> str | None:
    """Poll a Windows console for one line without an uncancellable blocking read."""
    try:
        import msvcrt as _msvcrt
    except ImportError:
        return None
    msvcrt_mod: Any = _msvcrt

    chars: list[str] = []
    while not stop_event.wait(_PASTE_READER_POLL_INTERVAL_SECONDS):
        while msvcrt_mod.kbhit():
            if stop_event.is_set():
                return None
            char = msvcrt_mod.getwch()
            if char in {"\r", "\n"}:
                return "".join(chars) + "\n"
            if char == "\x03":
                raise KeyboardInterrupt
            if char == "\x1a":  # Ctrl-Z = EOF
                return ""
            if char in {"\x00", "\xe0"}:  # function/arrow key: drop the scan code too
                if msvcrt_mod.kbhit():
                    msvcrt_mod.getwch()
                continue
            if char == "\b":
                if chars:
                    chars.pop()
                continue
            chars.append(char)
    return None

def _read_paste_line(stop_event: threading.Event) -> str | None:
    """Wait for one TTY line while staying responsive to cancellation.

    A bare ``sys.stdin.readline()`` cannot be cancelled: after the HTTP listener wins, the reader
    thread stays parked on stdin and swallows the NEXT flow's (or the CLI's) input."""
    if os.name == "nt":
        return _read_windows_paste_line(stop_event)

    try:
        import select

        stdin_fd = sys.stdin.fileno()
    except (AttributeError, OSError, TypeError, ValueError):
        return None

    line = bytearray()
    while not stop_event.is_set():
        try:
            readable, _, _ = select.select([stdin_fd], [], [], _PASTE_READER_POLL_INTERVAL_SECONDS)
        except (OSError, TypeError, ValueError):
            return None
        if stop_event.is_set():
            return None
        if readable:
            try:
                char = os.read(stdin_fd, 1)
            except OSError:
                return None
            if not char:
                return ""  # EOF
            line.extend(char)
            if char in {b"\r", b"\n"}:
                return line.decode(getattr(sys.stdin, "encoding", None) or "utf-8", errors="replace")
    return None
