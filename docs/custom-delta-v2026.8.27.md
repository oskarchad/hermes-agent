# Hermes v2026.8.27 retained custom delta

This manifest describes the local candidate rebuilt on the exact upstream base
`v2026.8.27^{commit}=5fc308a70719a83cccdbba4c0e39c23f5a8239d5`.
The candidate commit containing this file is the review subject; its exact SHA
is recorded in the Kanban completion handoff and bound OCR receipt.

The prior custom runtime `e2e0642166c02a682d071b89be9ef73109f88ec5` was used
only as behavioral evidence. Shared upstream behavior was not replayed.

Governing direction is indexed by `docs/ADR.md` and recorded in
`docs/0001-retained-custom-runtime-delta.md`. The backend/renderer seam routes
through `apps/desktop/DESIGN.md`, `docs/kanban/multi-gateway.md`, and
`docs/session-lifecycle.md` so future contributors reach the same contracts.

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
  `tests/test_tui_gateway_server.py`, `tests/test_hermes_state.py`,
  the changed turn/runtime tests under `tests/agent/` and `tests/run_agent/`,
  `ui-tui/src/__tests__/createGatewayEventHandler.test.ts`, and
  `apps/desktop/src/app/session/hooks/use-message-stream/captain-report-deduplication.test.tsx`.
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
- Custom/seam tests: changed `tests/hermes_cli/test_kanban_review_*.py`,
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
  `tests/hermes_cli/test_kanban_worker_context_projection.py`,
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
