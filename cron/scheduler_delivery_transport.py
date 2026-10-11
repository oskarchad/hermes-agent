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

import cron.scheduler_delivery as _delivery
from dataclasses import replace
from cron import scheduler_preflight as _preflight
from gateway.config import PlatformConfig


logger = logging.getLogger(__name__)

def _resolve_target_transport(job: dict, platform, platform_name: str, target: dict, adapters, config, *, routes=None):
    """Resolve ``(transport, pconfig, runtime_adapter, target_adapters)`` for one target, or
    ``(None, error)`` when it cannot be served (relay-fronted with no live transport, or not
    configured/enabled)."""
    from gateway.delivery import DeliveryTransport, resolve_delivery_transport
    from cron.delivery_routes import ExplicitRouteAdapters, configured_routes, route_for
    try:
        route = route_for(target, configured_routes() if routes is None else routes)
    except Exception:
        logger.debug('runtime boundary failed', exc_info=True)
        return (None, 'explicit cron delivery route configuration unavailable or invalid')
    expected = route_for(target, job.get('_cron_delivery_route_bindings', []))
    expected_profile = expected['adapter_profile'] if expected is not None else None
    live_profile = route['adapter_profile'] if route is not None else None
    if '_cron_delivery_route_bindings' in job and live_profile != expected_profile:
        return (None, 'explicit cron delivery route changed since dispatch')
    if route is not None:
        adapter = adapters.get_explicit(platform, route) if isinstance(adapters, ExplicitRouteAdapters) else None
        if adapter is None:
            return (None, 'explicit cron delivery route has no active allowed adapter')
        transport = DeliveryTransport(adapter, adapter.config, platform)
        return ((transport, adapter.config, adapter, {platform: adapter}), None)
    if isinstance(adapters, ExplicitRouteAdapters):
        adapters = adapters.legacy
    target_adapters = adapters
    transport = None
    if isinstance(adapters, _preflight.SharedRouteAdapters):
        shared = adapters.get(platform, target)
        target_adapters = {platform: shared} if shared is not None else {}
        if shared is not None:
            from dataclasses import replace
            from gateway.config import PlatformConfig
            own = config.platforms.get(platform)
            transport = DeliveryTransport(shared, replace(own, enabled=True) if own is not None else PlatformConfig(enabled=True), platform)
    if transport is None:
        transport = resolve_delivery_transport(platform, config, target_adapters)
    if transport is not None:
        pconfig = transport.config
        runtime_adapter = transport.adapter
    else:
        from gateway.relay import relay_fronted_platforms
        if platform_name in relay_fronted_platforms():
            return (None, f"platform '{platform_name}' is relay-fronted and has no live gateway transport; start the gateway (its ticker owns relay-fronted delivery and will fire the job on schedule)")
        pconfig = config.platforms.get(platform)
        runtime_adapter = None
    if transport is not None and (transport.is_relay or pconfig is None):
        if pconfig is None:
            from gateway.config import PlatformConfig
            pconfig = PlatformConfig(enabled=True)
    elif not pconfig or not pconfig.enabled:
        return (None, f"platform '{platform_name}' not configured/enabled")
    return ((transport, pconfig, runtime_adapter, target_adapters), None)
