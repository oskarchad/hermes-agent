# Captain reports return to their originating conversation

Task creation through the Kanban tool captures the session-scoped notifier
profile and destination. Desktop uses its durable session key; WebUI uses
`webui:<session-id>`. The Kanban registry, event inbox, leases and subscriptions
remain the only delivery authority. There is no new scheduler or process.

An idle, closed or disconnected origin does not grant another Captain chat
permission to consume its reports. A legacy task without a registered origin
is eligible only when its subscriptions identify one unambiguous destination
in the registered profile. Ambiguous destinations remain pending; this change
does not migrate old subscriptions to a newly opened chat or platform.

A unique published compression chain identifies the continuation of the same
conversation. Branches, delegated children, profile crossings, cycles and
ambiguous compression successors do not grant destination aliases. Resolution
uses a read-only snapshot of that profile's session database. Candidate discovery
and atomic leasing still use the original registry destination.

Desktop submits through the current prompt-turn admission API with
`completion_id` and `terminal_callback`. Captain input is hidden, system-authored
context, not a human-authored chat bubble. A successful final answer must already
exist in the agent session store before receiving the stable report ID and before
inbox settlement. The same ID is emitted by message completion and history
hydration. Failed turns retain their lease until expiry; rejected admission
releases it immediately. Receipt reconciliation avoids rerunning a persisted report.

WebUI requires the companion Captain consumer change in the operator's WebUI
fork. Deploying only this core change will prevent wrong-chat fallback, but will
not add a WebUI receiver. Install both reviewed candidates together. Rollback
requires restoring both prior versions; do not clear inbox rows or receipts.

## Verification and limits

`tests/tools/test_kanban_origin_delivery.py` checks session-context capture,
exact-origin leases, legacy ambiguity and compression boundaries.
`tests/tui_gateway/test_captain_real_turn.py` exercises the real poller, prompt
admission, persistence and outbound Desktop completion frame with an offline
model fixture. Captain inbox/poller suites cover busy admission, retry, receipt
reconciliation and privacy. These host-side tests do not establish that a remote
Desktop renderer displayed a frame, nor that an operator deployed this code.

Captain Watch and blocked escalators remain separate operational consumers.
This change neither installs their candidate scripts nor enables paused jobs.
An inbox acknowledgment proves persisted processing, not device receipt; a live
release must verify loaded SHAs and a visible origin-chat canary on each surface.
