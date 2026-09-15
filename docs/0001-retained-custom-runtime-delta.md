# ADR 0001: Retain only upstream-missing custom runtime behavior

Status: Accepted

## Context

The local Hermes runtime historically accumulated custom Captain, Kanban,
worker-ownership, OAuth, and session behavior while upstream evolved the same
shared lifecycle seams. Replaying the custom branch wholesale onto a newer
release would create parallel implementations and make future updates unsafe.

The operator selected upstream `v2026.8.27` at peeled commit
`5fc308a70719a83cccdbba4c0e39c23f5a8239d5` as the canonical base. Equivalent
or shared behavior supplied by upstream supersedes local code. The prior custom
runtime is evidence for missing contracts, not an implementation source of
record.

## Decision

- Build the local candidate directly on the frozen upstream release commit.
- Retain only behavior absent from upstream: durable Captain inbox and fenced
  receipts/recovery; Kanban review provenance and lifecycle safeguards;
  task-bounded toolsets and worker ownership/cleanup; headless MCP OAuth
  single-flight ownership; and the minimum session/renderer seams those
  contracts require.
- Extend upstream seams instead of replacing watcher wake behavior, delegation
  schema/registry, base TUI/session lifecycle, updater gates, terminal/plugin
  registry, MCP transport/reconnect, or other native upstream behavior.
- Validate only retained custom behavior and its direct integration seams. Full
  upstream-only regression coverage belongs to upstream and is not duplicated
  by this candidate.
- Require a clean exact-SHA candidate, one bounded implementation-time OCR pass,
  and independent Gauge review before any later operator-owned release action.

## Consequences

The local diff is reviewable as a retained delta rather than a second Hermes
implementation. Future updates repeat the same comparison against upstream and
drop any custom behavior that has become equivalent. No push, merge, update,
restart, deployment, or active-profile change is authorized by this decision.

The exact retained groups, paths, tests, and keep/drop rationale are recorded in
[`custom-delta-v2026.8.27.md`](custom-delta-v2026.8.27.md).
