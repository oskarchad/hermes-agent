"""Opt-in, exact-target cron transport bindings; never load another profile's secrets.

The gateway supplies its existing allowed profile set and live registry. Worker snapshots
are preflight evidence only: final delivery always resolves the live view again.
"""
from __future__ import annotations
import logging

from contextlib import contextmanager
from contextvars import ContextVar

_PREFLIGHT: ContextVar = ContextVar("cron_delivery_route_preflight", default=())


logger = logging.getLogger(__name__)

def configured_routes():
    from hermes_cli.config import load_config_readonly

    raw = load_config_readonly()
    cron = raw.get("cron", {})
    routes = cron.get("delivery_routes", []) if isinstance(cron, dict) else []
    if not isinstance(routes, list):
        raise ValueError("cron.delivery_routes must be a list")
    seen = set()
    for route in routes:
        if not isinstance(route, dict) or set(route) != {"platform", "chat_id", "adapter_profile"}:
            raise ValueError("cron.delivery_routes requires platform, chat_id, adapter_profile")
        if any(not isinstance(v, str) or not v or v != v.strip() or any(c in v for c in "*?[]")
               for v in route.values()):
            raise ValueError("cron.delivery_routes requires literal nonempty strings (no wildcards)")
        from gateway.config import Platform
        Platform(route["platform"])
        if route["platform"] != route["platform"].lower():
            raise ValueError("cron.delivery_routes platform must be lowercase")
        key = (route["platform"], route["chat_id"])
        if key in seen:
            raise ValueError("cron.delivery_routes has duplicate targets")
        seen.add(key)
    return routes


def route_for(target, routes):
    if target.get("thread_id"):
        return None
    return next((route for route in routes
                 if route["platform"] == target["platform"]
                 and route["chat_id"] == str(target["chat_id"])), None)


def route_binding_error(job, target, route):
    if "_cron_delivery_route_bindings" not in job:
        return None
    expected = route_for(target, job["_cron_delivery_route_bindings"])
    if (expected["adapter_profile"] if expected else None) != (route["adapter_profile"] if route else None):
        return "explicit cron delivery route changed since dispatch"
    return None


class ExplicitRouteAdapters:
    """Target-aware overlay on the existing own/satellite-to-primary adapter view."""

    def __init__(self, legacy, profile_adapters, allowed_profiles):
        self.legacy = legacy
        self.profiles = profile_adapters
        self.allowed_profiles = allowed_profiles

    def get_explicit(self, platform, route, default=None):
        """Resolve exactly the caller-validated route; never reread config or use legacy."""
        name = route["adapter_profile"]
        if name not in self.allowed_profiles():
            return default
        adapter = self.profiles.get(name, {}).get(platform)
        if (adapter is None or getattr(adapter, "is_connected", False) is not True
                or not getattr(getattr(adapter, "config", None), "enabled", False)):
            return default
        return adapter


def overlay_adapters(legacy, profiles, allowed_profiles):
    """Keep upstream own-bot/satellite selection unchanged when no binding is configured."""
    try:
        routes = configured_routes()
    except (ValueError, TypeError):
        routes = True  # invalid configuration must reach the mandatory fail-closed check
    return ExplicitRouteAdapters(legacy, profiles or {}, allowed_profiles) if routes else legacy


def bind_delivery_routes(job):
    """Keep dispatch intent only in this attempt/queue payload, never in the job registry."""
    return {**job, "_cron_delivery_route_bindings": list(_PREFLIGHT.get())}


def delivery_route_digest(job):
    """A caller can constrain admission to its dispatch intent, never supply an adapter grant."""
    import hashlib
    import json
    routes = [{key: record[key] for key in ("platform", "chat_id", "adapter_profile")}
              for record in job.get("_cron_delivery_route_bindings", [])]
    routes.sort(key=lambda route: (route["platform"], route["chat_id"]))
    return hashlib.sha256(json.dumps(routes, sort_keys=True).encode()).hexdigest()


def ensure_delivery_bindings(job):
    if "_cron_delivery_route_bindings" not in job:
        job.update(bind_delivery_routes(job))


def preflight_snapshot(adapters):
    from gateway.config import Platform
    records = []
    for route in configured_routes():
        adapter = (adapters.get_explicit(Platform(route["platform"]), route)
                   if isinstance(adapters, ExplicitRouteAdapters) else None)
        records.append({**route, "available": adapter is not None})
    return records


@contextmanager
def delivery_preflight_scope(adapters=None, *, snapshot=None):
    # A detached worker keeps its payload evidence when run_one_job has no adapters.
    if adapters is None and snapshot is None:
        yield
        return
    try:
        evidence = snapshot if snapshot is not None else preflight_snapshot(adapters)
    except (ValueError, TypeError):
        evidence = []
    token = _PREFLIGHT.set(evidence)
    try:
        yield
    finally:
        _PREFLIGHT.reset(token)


def check_explicit_delivery(job):
    """Return (error, covered platforms); bypass native preflight only for fully covered targets."""
    from cron.scheduler_delivery import _resolve_delivery_targets
    try:
        routes = configured_routes()
        if not routes and not job.get("_cron_delivery_route_bindings"):
            return None, set()
        targets = _resolve_delivery_targets(job) + _resolve_delivery_targets(job, for_failure=True)
        covered, uncovered = set(), set()
        for target in targets:
            route = route_for(target, routes)
            changed = route_binding_error(job, target, route)
            if changed:
                return changed, set()
            if route is None:
                uncovered.add(target["platform"])
                continue
            if {**route, "available": True} not in _PREFLIGHT.get():
                return "explicit cron delivery route has no active allowed adapter", set()
            covered.add(target["platform"])
        return None, covered - uncovered
    except Exception:
        # Configuration/read/target errors cannot authorize a different bot.
        logger.debug("runtime boundary failed", exc_info=True)
        return "explicit cron delivery route configuration unavailable or invalid", set()
