# Multi-gateway deployment

Architecture source: [ADR 0001 and the ADR index](../ADR.md). The exact local
retained behavior is catalogued in the
[v2026.8.27 custom-delta manifest](../custom-delta-v2026.8.27.md).

Hermes supports multiple gateway processes running concurrently — one per profile
(default, writer, admin, coder, researcher). Each gateway opens its own connection
to platform APIs and delivers messages for its profile's subscribers.

Task subscriptions also cover review feedback. A `changes_requested` review
event is delivered as an actionable review-BLOCK notification. Subscriptions
using `notify+wake` additionally wake the exact originating chat/thread/session
so the controller inspects the existing card and current run; `notify` remains
passive-only and `wake` remains wake-only. Review feedback never creates,
unblocks, requeues, or otherwise mutates a task.

## Single-dispatcher posture

Only one gateway owns the kanban dispatcher. The owning gateway keeps
`kanban.dispatch_in_gateway: true` (the default); every other gateway sets it
to `false`.

**Why this matters:** dispatching is single-owner so multiple gateways do not
race to spawn the same work. Notification delivery is profile-owned instead:
each gateway polls only subscriptions for profiles whose platform adapters it
hosts. The atomic event claim prevents duplicate delivery across watcher
processes.

## Configuration

On the dispatch-owning gateway (typically the `default` profile), no change is
needed. On every other profile gateway, add to `~/.hermes/config.yaml`:

```yaml
kanban:
  dispatch_in_gateway: false
```

Or set the env var: `HERMES_KANBAN_DISPATCH_IN_GATEWAY=false`

## What each gateway does

| Gateway role | dispatch_in_gateway | Opens subscribed board DBs? | Dispatcher | Notifier |
|---|---|---|---|---|
| default (confirmed dispatch-lock owner) | true (default) | yes | yes | owned profiles + legacy unstamped subscriptions |
| writer, admin, coder, etc. | false | yes, when the profile has subscriptions | no | that gateway's owned profiles |

Non-dispatch gateways still deliver messages for their own platform adapters
(Telegram, Discord, etc.). They do not dispatch tasks, and they skip boards
that have no subscriptions owned by their profiles.

## Captain reporting is a separate TUI/Desktop path (not a relay)

Everything above is about **gateway** processes delivering chat messages for
their owned profiles. The TUI/Desktop **Captain reporting inbox** (see the
[Kanban feature guide](../../website/docs/user-guide/features/kanban.md), "TUI &
Desktop Captain reports") is a different, local mechanism: it durably reports a
task's terminal event to an active TUI/Desktop session bound to the
**same normalized profile**, exactly once. Every session publishes receiver
liveness independently of pending work; the live exact origin wins, and an
untenant row may fall back to another same-profile session only after that
origin heartbeat expires. Eligibility is rechecked atomically with the lease.
Tenant-tagged fallback fails closed because current transport sessions do not
carry authoritative tenant identity; those rows may return only to their exact
origin.

It is **not a cross-gateway relay**. A Captain report is never routed across
gateways, profiles, tenants, boards, or accounts, and it invents no chat/human
destination. A gateway subscription and a Captain report are independent paths
that never duplicate each other — a task can notify its gateway chat *and*
report to its local Captain without either standing in for the other.

During an unbounded synthetic model turn, Hermes renews the durable lease and
fences every renewal and acknowledgement by both opaque token and owner. Each
turn carries one Captain event, so retries keep one stable event-derived ID even
when another board settles independently. Model deltas remain buffered (including
TTS) and a visible assistant `message.complete` is emitted only after an explicit
successful transcript-persistence receipt and a verified, unexpired owner-fenced
acknowledgement rowcount; persistence, lock, ownership, and settlement failures
remain recoverable. The poller reports reconciliation failures, releases every
still-owned unsettled token, clears its turn reservation, and keeps polling and
refreshing receiver heartbeats.

The persistence receipt is searched across the session database owned by the
normalized profile, not only the session that currently holds the lease. If a
different same-profile fallback session claims after the original session
disappears, Hermes atomically rehomes the committed synthetic turn to that
destination before acknowledgement instead of running the model or copying the
assistant report again. The profile-owned database and the board-qualified event
identity are the hard receipt boundaries; no lookup crosses a profile, tenant, or
board. A transport crash after commit therefore cannot leave two canonical
session projections of one event; reconnect hydration recovers a missed frame,
and both TUI and Desktop also deduplicate the stable ID defensively.

Captain generation receives only the bounded task/event report prompt plus the
agent's approved static system/persona context. Ordinary history from whichever
TUI/Desktop session happened to claim the event is not sent to the model. The
generated turn alone is persisted into the destination transcript and then
merged back into that session's in-memory history. The candidate row cap is
global across all boards in one poll, as is the UTF-8 byte cap.
