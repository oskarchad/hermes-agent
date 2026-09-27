# Hermes v2026.9.24 retained custom delta

This manifest describes the local candidate rebuilt on the upstream release
`v2026.9.24^{commit}=f97608f178` (Hermes Agent v0.21.5), following
[`0002-upstream-first-overlay-update.md`](0002-upstream-first-overlay-update.md)
and [`0001-retained-custom-runtime-delta.md`](0001-retained-custom-runtime-delta.md).
It supersedes [`custom-delta-v2026.8.27.md`](custom-delta-v2026.8.27.md) as the
list of what we keep; that file stays as history.

Source of the delta: the production overlay `30999cc039` (our delta on upstream
`69fd61b0ef`, 2026-09-15) plus fork PR #26 (HER-155), merged to fork `main` on
2026-09-27 and not yet deployed. The older fork `main` line (pre-overlay) is not
a source: decisions taken in the 2026-09-20 overlay stand.

Method: the overlay commit was applied onto `v2026.9.24`; every conflicting or
changed area was then tested with our contract tests against pure upstream. A
behavior whose contract test passes on pure upstream is upstream-native and our
code for it was dropped. Only failing behaviors were ported into upstream's
current structure.

## Kept, unchanged in substance

1. **Captain inbox, durable signals, session recovery.** Upstream still has no
   durable Captain ownership, receipt or recovery contract. Contract tests pass
   (`tests/tui_gateway/test_kanban_captain_*.py`, `test_kanban_notify_poller.py`);
   the poller wiring changed, see "Changed".
2. **Kanban review provenance and lifecycle gates.** Applied cleanly; tests pass.
   Upstream extended the stop guard's terminal-tool set with
   `kanban_request_changes`; we take upstream's set (it is a superset of ours).
3. **Explicit cron final-hop profile binding** (fork `docs/adr/0001`). Applied
   cleanly; see "Changed" for the delivery gate.
4. **Command-aware approval deny** (fork `docs/0003`). Applied cleanly; tests pass.
5. **Honcho ingest filter (HER-155, fork PR #26).** Keeps gateway notices and
   Kanban worker turns out of the user's Honcho memory. New in this candidate.

## Changed

- **Captain inbox rides upstream's poller.** The overlay carried a full copy of
  the old upstream notification poller in `tui_gateway/captain_inbox.py`.
  `bind_module` registers that module after `session_notifications`, so the copy
  replaced upstream's loop and with it profile runtime scoping, batched process
  completions, `/heartbeat`, bot mailbox delivery and the orphaned-completion
  sweep. The copies are gone. Upstream's loop now takes its kanban step through
  one fork seam, `_captain_poll_kanban` (receiver heartbeat, turn reservation
  under upstream's turn admission, durable exact-origin and Captain claims,
  settlement after the terminal turn). Our collector and formatter are named
  `_captain_collect_kanban_notifications` and `_captain_format_kanban_event_text`;
  upstream's RAM-buffered `_notif_poll_kanban` stays defined but is off the
  runtime path. A test pins that `server` publishes upstream's poller functions
  and that `captain_inbox` defines no name `session_notifications` owns.
- **Cron delivery gate.** Our drain-time gate read
  `gateway.multiplex_profile_allowlist`, a key upstream removed in config v43;
  `GatewayConfig` no longer carries it, so the gate never restricted anything.
  It now re-reads upstream's served set (`profiles_to_serve`) at drain time, so a
  profile parked with `hermes -p <name> gateway stop` or made standalone stops
  receiving queued cron output. The test parks the profile instead of patching a
  field that does not exist.

## Dropped as upstream-native

- **Stop-guard terminal tool set.** Upstream's set is a superset of ours.
- **Delegated terminal marker hygiene.** Upstream now excludes
  `HERMES_DELEGATED_CHILD_CONTEXT` (and `HERMES_CRON_SESSION`) from terminal
  snapshots; our regression test passes on pure upstream.

## Ported into upstream's new structure

- **Task toolsets and worker ownership/cleanup** (`hermes_cli/kanban_db_dispatch.py`,
  one change in `kanban_db.py::reclaim_task`). Kept: per-run systemd scope with
  process-session fallback and `_worker_scope_expected`; scope-first termination with
  `cleanup_verified`; run/PID/lock compare-and-set on every reclaim path; same-card
  review/repair handoff fence; task toolset allowlists validated before claim; one-claim
  PR continuation. Reconciled with upstream: an unverified spawn fingerprint is never
  signalled by PID (stopping its scope by name is allowed); upstream's
  `reap_terminal_workers` stays and also stops the run's scope; upstream's stale and
  max-runtime bookkeeping replaces our older stale rewrite; an operator reclaim releases
  an unverified PID as upstream does. Dropped as upstream-native: failing the spawn when
  no scope exists (`RestartSafeScopeUnavailable`), our private max-runtime kill loop.
  Open: `changes_requested`/`review_reopened` lift `active_pr` for one claim only (stricter
  than upstream); archive and parent-reopen paths still terminate without a scope unit.
- **Lifecycle guard, PR17** (`cron/lifecycle_guard.py`, `tools/shell_heredoc.py`).
  Kept: quoted Python stdin heredoc bodies are split from shell source and their literal
  `os.system`/`subprocess.*` operands (string or argv) are scanned recursively, with
  `Path(...)` data blanked; unowned execution operands and unsupported wrapper grammars
  (su, runuser, nsenter, ionice) are refused; the second reference sweep runs on the text
  PR17 already cleaned. On pure upstream, `subprocess.run(['hermes','gateway','stop'])`
  inside such a heredoc is not blocked. Reconciled: upstream's owning-command masking and
  stricter marker set; upstream's `executed` flag (#113944) carried through the recursion;
  refusals set `budget.refusal` like upstream's. Dropped as upstream-native: fail-closed
  refusal inside a file that is only mentioned in inert text.
- **Headless MCP OAuth ownership** (`tools/mcp_oauth.py`, `tools/mcp_oauth_provider.py`).
  Kept: a forced headless flow never reads stdin; a stoppable TTY paste reader joined
  before the next flow; state-mismatch redaction before SDK and app logs; a 0.5 s
  per-request timeout so a stalled client cannot hold the port. Upstream-native, dropped:
  first-result-wins callback, secondary GET safety, no code/state logging, port release
  after paste and after skip/cancel, no retry after EOF. Single-flight ownership in
  `mcp_oauth_manager.py` is upstream's. The Windows paste reader was dropped.


## Verification boundary

Only our contract tests and the upstream tests of the modules we touch are run,
with and without the delta, on the same host. The full upstream suite is outside
this candidate's contract (ADR 0001).
