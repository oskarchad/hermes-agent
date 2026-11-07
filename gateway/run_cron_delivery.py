"""Explicit cron delivery admission over upstream own-bot/satellite routing."""


def allowed_delivery_profiles(runner):
    from gateway.run import _handoff_watch_scopes, _cron_profile_gate
    cfg = runner.config
    if not getattr(cfg, "multiplex_profiles", False):
        return set()
    names = {name for name, home in _handoff_watch_scopes(runner)
             if name is not None and _cron_profile_gate(name, home)}
    allowlist = getattr(cfg, "multiplex_profile_allowlist", None)
    return names if allowlist is None else names & set(allowlist)


def owner_delivery_adapters(runner):
    """The canonical execution owner validates against its live admitted adapter registry."""
    from cron.delivery_routes import ExplicitRouteAdapters
    return ExplicitRouteAdapters({}, getattr(runner, "_profile_adapters", {}),
                                 lambda: allowed_delivery_profiles(runner))


def drain_restart_safe_cron_deliveries(adapters, loop, runner=None) -> None:
    """Drain each profile's worker queue through its matching live adapters. A credential-less satellite
    profile (no bot of its own) drains through the primary's adapters routed by its own profile routes."""
    from gateway.run import _handoff_watch_scopes, _profile_runtime_scope, get_hermes_home
    from cron import scheduler as cron_scheduler
    from cron import scheduler_preflight as sched_preflight

    if runner is None:
        if adapters is not None:
            cron_scheduler.drain_delivery_queue(adapters, loop)
        return
    for profile_name, profile_home in _handoff_watch_scopes(runner):
        profile_adapters = (adapters if profile_name is None
                            else getattr(runner, "_profile_adapters", {}).get(profile_name))
        if profile_adapters is None:
            continue
        with _profile_runtime_scope(profile_home or get_hermes_home()):
            owned = (getattr(runner, "_profile_bot_platforms", None) or {}).get(profile_name)
            if (profile_name is not None and adapters
                    and not sched_preflight.has_credential_platform(profile_adapters, owned)):
                routes = sched_preflight._primary_profile_routes_for_current_home()
                if routes:
                    profile_adapters = sched_preflight.SharedRouteAdapters(adapters, routes)
            from cron.delivery_routes import overlay_adapters
            view = overlay_adapters(profile_adapters, getattr(runner, "_profile_adapters", {}),
                                    lambda: allowed_delivery_profiles(runner))
            cron_scheduler.drain_delivery_queue(view, loop)
