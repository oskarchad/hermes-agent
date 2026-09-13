# ADR 0001: Explicit cron final-hop profile binding

- Status: Accepted (Captain-approved fork contract; release remains separately gated)
- Date: 2026-09-07
- Decision source: Kanban t_33dc0bce, Captain decision accepting METHOD DELTA #7822;
  production migration ownership remains with t_24150633.

## Context

A job-owning profile without a Discord adapter needs to deliver one exact target
through an already-running multiplex profile. Moving jobs or inheriting another
profile's credentials would violate ownership and isolation. Managed gateways
execute cron jobs in restart-safe subprocesses, with final delivery performed by
the gateway's existing durable delivery queue drain.

## Decision

The owning home's `cron.delivery_routes` list contains literal records
`{platform, chat_id, adapter_profile}`. A record binds only that resolved channel,
without wildcard or thread expansion, to an enabled, connected adapter admitted
by the gateway's existing served-profile allowlist/gate. No mapping means existing
own-adapter and satellite-to-primary behavior remains unchanged.

The ticker and gateway drain share selection. The existing subprocess handoff
carries secret-free exact-route availability evidence captured at dispatch.
Preflight validates it before side effects, including script-only jobs. The
existing attempt/queue payload retains dispatch intent, not new job-registry
state. Final delivery rereads the owning mapping, checks the current profile gate
and live adapter, and cannot switch bot on removal/rebinding or send failure.
The single validated route drives transport selection and its delivery policies.

Only the selected live adapter's platform configuration is used for that target;
no other profile configuration or secrets are loaded for routing. The owning job
retains its store, schedule, prompt, and session ownership. No new queue, schema,
daemon, model tool or credential fallback is introduced. Mandatory route failures
reuse the persisted alert-once state and recovery behavior.

## Consequences and gates

Manual/standalone execution lacking live dispatch evidence fails closed for a
bound target. Dispatch evidence alone cannot authorize a final send. Cross-profile
routing neither creates threads nor seeds another profile's conversation.

Tests use temporary homes, a real worker subprocess/adoption/queue/drain, and a
network-free adapter transport. They are not proof of live Discord delivery.
Independent exact-SHA review is required. Merge, release, restart, production
configuration changes and a delivery-only canary remain operator-owned gates;
this ADR grants none of those permissions.

See the [cron implementation contract](../../website/docs/developer-guide/cron-internals.md).
