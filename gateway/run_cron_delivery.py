"""Gateway final-hop draining of restart-safe cron delivery queues."""

def _drain_restart_safe_cron_deliveries(adapters, loop, runner=None) -> None:
    """Drain each profile's worker queue through its matching live adapters. A credential-less satellite
    profile (empty adapter map) drains through the primary's adapters routed by its own profile routes."""
    from gateway.run import _handoff_watch_scopes, _profile_runtime_scope, get_hermes_home
    from cron import scheduler as cron_scheduler


    if runner is None:
        if adapters is not None:
            cron_scheduler.drain_delivery_queue(adapters, loop)
        return
    for profile_name, profile_home in _handoff_watch_scopes(runner):
        if profile_name is None:
            profile_adapters = adapters
        else:
            profile_adapters = getattr(runner, "_profile_adapters", {}).get(profile_name)
        if profile_adapters is None:
            continue
        with _profile_runtime_scope(profile_home or get_hermes_home()):
            from cron.delivery_routes import adapters_for_profile
            view = adapters_for_profile(
                profile_name, primary=adapters,
                profiles=getattr(runner, "_profile_adapters", {}),
                allowed_profiles=lambda: {
                    name for name, _ in _handoff_watch_scopes(runner) if name is not None})
            cron_scheduler.drain_delivery_queue(view, loop)
