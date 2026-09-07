"""Restart-safe cron subprocess handoff. No gateway transport credentials cross this boundary."""
from __future__ import annotations
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time

logger = logging.getLogger("cron.scheduler")

def _launch_external_cron_worker(job: dict, *, adapters=None) -> bool:
    """Launch *job* outside a managed gateway cgroup when required.

    Returns ``False`` when the caller is not a managed systemd gateway and the
    existing in-process path should be used.  In managed topology, failure to
    establish the transient scope raises: falling back would recreate the
    restart interruption this handoff exists to prevent.
    """
    from cron.scheduler import (_get_hermes_home, mark_execution_handoff_pending, _ensure_cron_dir, windows_hide_flags, _running_lock, _restart_safe_waiter_job_ids, _wait_for_external_cron_worker)
    execution_id = str(job["execution_id"])
    job_id = str(job["id"])
    handoff_dir = _get_hermes_home() / "cron" / "external-workers"
    payload_path = handoff_dir / f"{execution_id}.json"
    ack_path = handoff_dir / f"{execution_id}.ready"
    command = [
        sys.executable,
        "-m",
        "cron.scheduler",
        "--external-worker-file",
        str(payload_path),
        "--ack-file",
        str(ack_path),
    ]

    from agent.secret_scope import is_multiplex_active
    from tools.environments.local import build_subprocess_env
    from tools.process_registry import restart_safe_gateway_child_argv

    multiplex_active = is_multiplex_active()
    scoped_command = restart_safe_gateway_child_argv(
        command,
        unit_suffix=f"cron-{job_id}-exec-{execution_id}",
    )
    if scoped_command == command:
        return False

    if mark_execution_handoff_pending(execution_id) is None:
        raise RuntimeError(
            "cron execution claim changed before external worker handoff"
        )

    from cron.delivery_routes import preflight_snapshot
    try:
        route_snapshot = preflight_snapshot(adapters)
    except Exception:
        route_snapshot = []  # worker independently validates config and blocks before running

    _ensure_cron_dir(handoff_dir)
    try:
        handoff_dir.chmod(0o700)
    except OSError:
        pass
    fd = os.open(payload_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as payload_file:
            json.dump(
                {
                    "job": job,
                    "profile_home": str(_get_hermes_home().resolve()),
                    "multiplex_active": multiplex_active,
                    "delivery_route_preflight": route_snapshot,
                },
                payload_file,
            )
            payload_file.flush()
            os.fsync(payload_file.fileno())
    except BaseException:
        payload_path.unlink(missing_ok=True)
        raise

    worker_env = build_subprocess_env(
        scrub_secrets=multiplex_active,
        inherit_profile_home=True,
        extra={"HERMES_HOME": str(_get_hermes_home().resolve())},
    )
    try:
        process = subprocess.Popen(
            scoped_command,
            cwd=str(Path(__file__).resolve().parent.parent),
            env=worker_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            creationflags=windows_hide_flags(),
        )
    except BaseException:
        payload_path.unlink(missing_ok=True)
        raise

    with _running_lock:
        _restart_safe_waiter_job_ids.add(job_id)

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if ack_path.exists():
            try:
                acknowledgement = json.loads(ack_path.read_text(encoding="utf-8"))
            except Exception:
                logger.exception(
                    "Cron external worker %s published an unreadable acknowledgement; "
                    "treating handoff as ownership-uncertain",
                    execution_id,
                )
                return _wait_for_external_cron_worker(
                    process,
                    execution_id=execution_id,
                    job_id=job_id,
                    handoff_files=(payload_path,),
                )
            finally:
                ack_path.unlink(missing_ok=True)
            if (
                not isinstance(acknowledgement, dict)
                or acknowledgement.get("execution_id") != execution_id
            ):
                logger.error(
                    "Cron external worker acknowledgement mismatch for %s; "
                    "treating handoff as ownership-uncertain",
                    execution_id,
                )
                return _wait_for_external_cron_worker(
                    process,
                    execution_id=execution_id,
                    job_id=job_id,
                    handoff_files=(payload_path,),
                )
            logger.info(
                "Cron job '%s' handed to restart-safe worker pid=%s execution=%s",
                job_id,
                acknowledgement.get("pid"),
                execution_id,
            )
            return _wait_for_external_cron_worker(
                process,
                execution_id=execution_id,
                job_id=job_id,
                handoff_files=(payload_path,),
            )
        returncode = process.poll()
        if returncode is not None:
            with _running_lock:
                _restart_safe_waiter_job_ids.discard(job_id)
            payload_path.unlink(missing_ok=True)
            raise RuntimeError(
                f"cron external worker exited before ownership acknowledgement "
                f"(exit {returncode})"
            )
        time.sleep(0.05)

    # The child may have adopted the durable row just before publishing its
    # acknowledgement.  Never fall back to in-process execution on an uncertain
    # handoff: that could duplicate side effects.  The execution owner/dead-owner
    # recovery ledger remains the authority.
    logger.warning(
        "Cron external worker for job '%s' did not acknowledge within 5s; "
        "leaving the durable execution claim untouched",
        job_id,
    )
    return _wait_for_external_cron_worker(
        process,
        execution_id=execution_id,
        job_id=job_id,
        handoff_files=(payload_path, ack_path),
    )

def _run_external_worker_payload(payload_path: Path, ack_path: Path) -> bool:
    """Adopt and execute one gateway-dispatched cron payload.

    The execution row is created by the gateway before spawn, then transferred
    here before the ready acknowledgement is published.  No side effect runs
    unless that durable ownership transfer succeeds.
    """
    from cron.scheduler import (use_cron_store, run_one_job)
    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        job = payload["job"]
        profile_home = Path(payload["profile_home"]).resolve()
        execution_id = str(job["execution_id"])
    except Exception:
        logger.exception("Cron external worker could not load payload %s", payload_path)
        return False
    finally:
        try:
            payload_path.unlink(missing_ok=True)
        except OSError:
            pass

    from agent.secret_scope import (
        build_profile_secret_scope,
        is_multiplex_active,
        reset_secret_scope,
        set_multiplex_active,
        set_secret_scope,
    )
    from cron.executions import adopt_claimed_execution
    from hermes_cli.env_loader import hydrate_profile_secret_sources
    from hermes_constants import (
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    home_token = set_hermes_home_override(profile_home)
    previous_multiplex = is_multiplex_active()
    multiplex_active = bool(payload.get("multiplex_active", False))
    set_multiplex_active(multiplex_active)
    hydrate_profile_secret_sources(profile_home)
    secret_token = set_secret_scope(build_profile_secret_scope(profile_home))
    try:
        with use_cron_store(profile_home):
            if adopt_claimed_execution(execution_id) is None:
                logger.error(
                    "Cron external worker refused execution %s: durable ownership "
                    "could not be established",
                    execution_id,
                )
                return False
            try:
                ack_path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(ack_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as ack_file:
                    json.dump({"pid": os.getpid(), "execution_id": execution_id}, ack_file)
                    ack_file.flush()
                    os.fsync(ack_file.fileno())
            except Exception:
                logger.exception(
                    "Cron external worker could not publish ready acknowledgement for %s",
                    execution_id,
                )
                return False
            old_external_execution = os.environ.get("_HERMES_CRON_EXTERNAL_WORKER")
            os.environ["_HERMES_CRON_EXTERNAL_WORKER"] = execution_id
            try:
                from cron.delivery_routes import delivery_preflight_scope
                with delivery_preflight_scope(snapshot=payload.get("delivery_route_preflight", [])):
                    return run_one_job(job, adapters=None, loop=None, verbose=False)
            finally:
                if old_external_execution is None:
                    os.environ.pop("_HERMES_CRON_EXTERNAL_WORKER", None)
                else:
                    os.environ["_HERMES_CRON_EXTERNAL_WORKER"] = old_external_execution
    finally:
        reset_secret_scope(secret_token)
        set_multiplex_active(previous_multiplex)
        reset_hermes_home_override(home_token)
