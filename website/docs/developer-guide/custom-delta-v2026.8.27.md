# Hermes v2026.8.27 retained custom delta

This manifest describes the local candidate rebuilt on the exact upstream base
`416a8177c25d87aa9929dfcf31f7964137d7fcdd` (live `origin/main`), then merged
with the fork base `fork/main = bd3ce420dc85af5344c9729f784675f08cc2441d` so the
candidate carries both the current upstream and the retained fork delta. The
earlier bases `ee4452991d17534aa561f31ee55596d082aa94e7` and
`v2026.8.27^{commit}=5fc308a70719a83cccdbba4c0e39c23f5a8239d5` remain ancestors;
neither is the base this candidate is built on.
The candidate commit containing this file is the review subject; its exact SHA
is recorded in the Kanban completion handoff and bound evidence receipt.

The prior custom runtime `e2e0642166c02a682d071b89be9ef73109f88ec5` was used
only as behavioral evidence. Shared upstream behavior was not replayed.

Governing direction is indexed by `website/docs/developer-guide/ADR.md` and recorded in
`website/docs/developer-guide/0001-retained-custom-runtime-delta.md`, with the cron delivery
contract in `website/docs/developer-guide/adr/0001-explicit-cron-delivery-binding.md`. The
backend/renderer seam
routes through `apps/desktop/DESIGN.md` so future contributors reach the same
contracts.

## 1. Captain inbox, durable signals, and session recovery

- Upstream base: keeps the upstream TUI/session loop, notification poller, and
  renderer authority.
- Retained delta: durable profile-Captain inbox and signals; lease/ack fencing;
  idempotent receipt persistence; task-only turn isolation; cross-session
  recovery; deterministic transcript ordering; renderer-side deduplication.
- Runtime paths: `hermes_cli/kanban.py`, `hermes_cli/kanban_db.py`,
  `tools/kanban_tools.py`, `tui_gateway/server.py`, `hermes_state.py`,
  `agent/codex_runtime.py`, `agent/conversation_loop.py`,
  `agent/turn_context.py`, `agent/turn_finalizer.py`, `run_agent.py`,
  `ui-tui/src/app/createGatewayEventHandler.ts`, `ui-tui/src/gatewayTypes.ts`,
  and the Desktop message-stream files under
  `apps/desktop/src/app/session/hooks/use-message-stream/`.
- Custom/seam tests: `tests/tui_gateway/test_kanban_captain_inbox.py`,
  `tests/tui_gateway/test_kanban_captain_signals.py`,
  `tests/tui_gateway/test_kanban_notify_poller.py`,
  `tests/tui_gateway/test_failed_turn_retention.py`,
  `tests/tui_gateway/test_tui_gateway_server.py`,
  `tests/hermes_state/test_hermes_state.py`,
  the changed turn/runtime tests under `tests/agent/`,
  `ui-tui/src/__tests__/createGatewayEventHandler.test.ts`, and the Desktop
  message-stream tests under
  `apps/desktop/src/app/session/hooks/use-message-stream/`.
- Keep rationale: upstream has no equivalent durable Captain ownership,
  receipt, signal, or recovery contract. The retained code extends upstream
  seams instead of replacing its base session lifecycle.

## 2. Kanban review provenance and lifecycle gates

- Upstream base: remains authoritative for core task/review state transitions,
  gateway dispatch, and dashboard transport.
- Retained delta: review-run provenance and phase-specific skills; goal-mode
  review remains intermediate until approval; exact source-run lineage for
  terminal alerts; parent invalidation and lifecycle fencing retained where
  upstream lacks the contract.
- Runtime paths: `hermes_cli/kanban_db.py`, `hermes_cli/kanban.py`,
  `hermes_cli/kanban_alerts.py`, `gateway/kanban_watchers.py`,
  `tui_gateway/server.py`, `plugins/kanban/dashboard/plugin_api.py`, and the
  generated dashboard bundle `plugins/kanban/dashboard/dist/index.js`.
- Custom/seam tests: changed `tests/hermes_cli/test_kanban_review_*.py`, plus
  the unchanged upstream regression tests this delta must keep green —
  `tests/hermes_cli/test_kanban_host_cap.py`,
  `tests/hermes_cli/test_kanban_notify.py`,
  `tests/hermes_cli/test_kanban_parent_reopen_invalidation.py`,
  `tests/hermes_cli/test_kanban_worker_lifecycle_hooks.py`, and
  `tests/plugins/test_kanban_dashboard_plugin.py`.
- Keep rationale: these provenance and goal/review invariants are absent from
  the frozen upstream database and alert surfaces.

## 3. Task toolsets and worker ownership/cleanup

- Upstream base: keeps upstream tool registry/discovery and ordinary process
  spawning.
- Retained delta: task-level bounded toolset allowlists with phase projection;
  compact worker context; host-cap validation; PID/run CAS; Linux systemd-scope
  isolation with process-session fallback; teardown fencing before same-card
  review/repair handoff; deterministic cleanup and stale-worker recovery.
- Runtime paths: `hermes_cli/kanban.py`, `hermes_cli/kanban_db.py`,
  `tools/kanban_tools.py`, `model_tools.py`, `toolsets.py`,
  `plugins/kanban/dashboard/plugin_api.py`, and the dashboard bundle.
- Custom/seam tests: `tests/hermes_cli/test_kanban_task_toolset_surfaces.py`,
  `tests/hermes_cli/test_kanban_task_toolsets.py`,
  `tests/hermes_cli/test_kanban_worker_cgroup_isolation.py`, plus the changed
  dashboard, lifecycle, and review tests.
- Keep rationale: frozen upstream has neither task-scoped allowlists nor the
  retained run/PID/scope ownership contract.

## 4. Headless MCP OAuth ownership

- Upstream base: keeps upstream MCP transport, reconnect, discovery, and token
  storage.
- Retained delta: one-attempt OAuth ownership, local callback server
  coordination, bounded waiting, and headless-safe completion so concurrent
  callers do not start duplicate interactive flows.
- Runtime paths: `tools/mcp_oauth.py`, `tools/mcp_oauth_manager.py`.
- Custom/seam tests: `tests/tools/test_mcp_oauth_single_attempt.py` and the
  retained cases in `tests/tools/test_mcp_oauth.py`.
- Keep rationale: the ownership/single-flight contract is missing upstream;
  transport/reconnect code is not duplicated.

## 5. Delegated terminal marker hygiene

- Upstream base: keeps environment snapshot and terminal execution behavior.
- Retained delta: excludes `HERMES_DELEGATED_CHILD_CONTEXT` from shared
  environment snapshots so a delegated child marker cannot leak into later
  sessions.
- Runtime path: `tools/environments/base.py`.
- Custom/seam test: retained regression in
  `tests/tools/test_snapshot_session_id_leak.py`.
- Keep rationale: this is a one-line upstream-seam fix with no parallel
  terminal implementation.

## 6. Governed Kanban lifecycle and explicit resume

- Upstream base: keeps upstream task/run state transitions and dispatch.
- Retained delta: a governed intake/caller boundary for lifecycle mutations
  (`governance.evaluate_tx` consulted before status changes and completion),
  explicit continuation past a PR checkpoint for ready tasks, terminal-action
  handling for `kanban_request_review` in the stop guard, and the
  child-tenant-conflict guard on Captain inheritance.
- Runtime paths: `hermes_cli/kanban_governance.py`,
  `hermes_cli/kanban_governance_store.py`, `hermes_cli/kanban_db.py`,
  `agent/kanban_stop.py`, `gateway/kanban_watchers_notifier.py`, and
  `plugins/kanban/dashboard/plugin_api.py`.
- Custom/seam tests: `tests/hermes_cli/test_kanban_governance.py`,
  `tests/hermes_cli/test_kanban_explicit_resume.py`,
  `tests/gateway/test_kanban_governance_notifications.py`,
  `tests/plugins/test_kanban_governance_callers.py`, and
  `tests/agent/test_kanban_stop.py`.
- Keep rationale: upstream has no governed-intake contract for these lifecycle
  callers; the retained code gates existing transitions instead of replacing
  them.

## 7. Opt-in command-aware approval deny

- Upstream base: keeps upstream approval flow, floors, and yolo/mode handling.
- Retained delta: `approvals.deny_commands` selectors that match executable
  commands rather than whole-text globs, so a quoted `merge`/`push` inside a PR
  description cannot trigger a deny; shell control/input boundary handling and
  positional push repository/tag ref normalization.
- Runtime paths: `tools/approval.py`, `tools/approval_command_rules.py`,
  `tools/approval_detection.py`, `tools/approval_floors.py`,
  `tools/shell_heredoc.py`, and `hermes_cli/config_defaults.py`.
- Governing decision: `website/docs/developer-guide/0003-command-aware-approval-deny.md`.
- Custom/seam test: `tests/tools/test_approval_command_rules.py`.
- Keep rationale: the upstream deny rule is a whole-text glob; the false-positive
  class it creates is not addressed upstream.

## 8. Explicit cron delivery binding and restart-safe worker

- Upstream base: keeps upstream scheduling, catch-up, and delivery transport.
- Retained delta: delivery targets bound to allowed live profile adapters with
  a preflight route snapshot, a closed delivery-lookup race with blocked-alert
  dedup, and the restart-safe external cron worker handoff (durable execution
  ownership transfer, transient user scope with a documented non-scope
  fallback, and adoption-grace-aligned acknowledgement).
- Runtime paths: `cron/delivery_routes.py`, `cron/scheduler_worker.py`,
  `cron/scheduler_delivery.py`, `cron/scheduler.py`, `cron/lifecycle_guard.py`,
  `gateway/run_cron_delivery.py`, and `gateway/run.py`.
- Governing decision: `website/docs/developer-guide/adr/0001-explicit-cron-delivery-binding.md`.
- Custom/seam tests: `tests/cron/test_explicit_delivery_routes.py`,
  `tests/cron/test_restart_safe_worker.py`,
  `tests/cron/test_lifecycle_guard_budget.py`, and
  `tests/gateway/test_cron_delivery_housekeeping.py`.
- Keep rationale: upstream binds deliveries at send time and runs cron jobs
  inside the gateway process, so neither the exact-target contract nor the
  restart-safe ownership handoff exists upstream.

## 9. Split-module ownership in the TUI gateway

- Upstream base: keeps upstream's notification poller, loop/heartbeat ticks,
  delegation display metadata and desktop sinks in
  `tui_gateway/session_notifications.py`.
- Retained delta: the Captain route is expressed as `_notif_poll_kanban` plus
  `_dispatch_captain_turn` — the hook upstream's poller already calls — instead
  of a second copy of the poller. `bind_module` now tags a name's owner on first
  publication and requires an explicit `override=(...)` for any takeover, so a
  retained module can no longer silently revert an upstream helper.
- Runtime paths: `tui_gateway/captain_inbox.py`, `tui_gateway/method_ctx.py`,
  `tui_gateway/methods_session.py`.
- Custom/seam tests: `tests/tui_gateway/test_split_module_collisions.py`,
  `tests/tui_gateway/test_kanban_captain_inbox.py`,
  `tests/tui_gateway/test_kanban_notify_poller.py`, plus the upstream
  regressions this delta must keep green —
  `tests/hermes_cli/test_completion_backlog.py`,
  `tests/tui_gateway/test_heartbeat_tui_tick.py`,
  `tests/tui_gateway/test_loop_command.py`.
- Keep rationale: the durable Captain lease/receipt contract is absent upstream,
  but the poller itself is not — carrying a pre-extraction copy of it dropped
  upstream's completion batching (12 ready completions produced 12 autonomous
  turns instead of 1), bot live-delivery poll and heartbeat tick.

## Deliberately dropped

The candidate does not replay custom copies of watcher wake behavior,
delegation schema/registry, base TUI/session lifecycle, updater control socket
or package/image gates, update-branch strategy, terminal/plugin registry, MCP
transport/reconnect, cron fixes, browser/model/provider/messaging/security
behavior, or other functionality already present in the frozen upstream.

## Verification boundary

Only the changed custom tests and direct upstream seams listed above are run.
The full Hermes suite and unrelated upstream-only model, browser, cron,
provider, and messaging tests are intentionally outside this candidate's
operator-approved test contract.
