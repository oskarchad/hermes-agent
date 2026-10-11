"""Parent-first native tree shutdown with retained owned process-group verification."""
from __future__ import annotations

import logging
import os
import signal
import time

logger = logging.getLogger(__name__)


def _owned_group_snapshot(pid):
    if os.name == 'nt':
        return None
    try:
        import psutil
        if os.getpgid(pid) != pid:
            return None
        members = []
        for process in psutil.process_iter():
            try:
                if os.getpgid(process.pid) == pid:
                    members.append(process)
            except (OSError, psutil.NoSuchProcess):
                continue
        return pid, members
    except (OSError, psutil.Error):
        logger.debug('owned worker group snapshot unavailable', exc_info=True)
        return None


def _group_members(pgid):
    import psutil
    from tools.process_registry import ProcessRegistry
    members = []
    for process in psutil.process_iter():
        try:
            if os.getpgid(process.pid) == pgid and ProcessRegistry._proc_alive(process):
                members.append(process)
        except (OSError, psutil.NoSuchProcess):
            continue
    return members


def _finish_owned_group(snapshot, info):
    """A private group is signalled only while an original process identity still proves ownership."""
    from tools.process_registry import ProcessRegistry
    if snapshot is None:
        return
    pgid, original = snapshot
    info['termination_target'] = 'process_group'
    remaining = _group_members(pgid)
    if not remaining:
        info['group_cleanup_verified'] = True
        return
    if not any(ProcessRegistry._proc_alive(proc) for proc in original):
        info['group_cleanup_verified'] = False
        return
    try:
        os.killpg(pgid, signal.SIGTERM)  # windows-footgun: ok
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and _group_members(pgid):
            time.sleep(0.05)
        if _group_members(pgid):
            os.killpg(pgid, signal.SIGKILL)  # windows-footgun: ok
            info['sigkill'] = True
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline and _group_members(pgid):
                time.sleep(0.05)
    except OSError:
        logger.debug('owned worker group cleanup failed', exc_info=True)
    info['group_cleanup_verified'] = not _group_members(pgid)


def _terminate_native_worker_tree(pid, started_at, info):
    from gateway.status import get_process_start_time
    from tools.process_registry import ProcessRegistry
    snapshot = _owned_group_snapshot(pid)
    # The caller already verified the composed boot epoch + start fingerprint;
    # the native helper independently checks the numeric kernel start witness.
    expected_start = get_process_start_time(pid)
    ProcessRegistry._terminate_host_pid(pid, expected_start=expected_start)
    _finish_owned_group(snapshot, info)
