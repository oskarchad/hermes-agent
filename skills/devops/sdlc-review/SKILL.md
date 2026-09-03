---
name: sdlc-review
description: Review Kanban handoffs and route verified outcomes.
version: 1.2.0
author: Jakub Wolniewicz (@frizikk) + Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [kanban, review, quality, verification]
    category: devops
    requires_toolsets: [kanban]
environments:
  - kanban
---

# SDLC Review Skill

Independently verify work handed from a Kanban implementation run to the review lane, then approve it, request changes, or escalate. This skill reviews the deliverable and its evidence; it does not take over the implementer's work.

## When to Use

Use this skill when the dispatcher spawned you as the independent reviewer and
the task needs a verdict before delivery can continue. Both supported routes
use the same method:

- a task claimed from the `review` lane uses its latest `review_requested`
  handoff;
- a downstream review card uses the completed parent handoff and the frozen
  exact SHA named by its own specification.

## Prerequisites

- A Kanban worker context with the current task and run identifiers.
- Native Kanban tools: `kanban_show`, `kanban_comment`, `kanban_complete`, `kanban_request_changes`, and `kanban_block`.
- Workspace access through `read_file`, `search_files`, and `terminal` when the deliverable is code.
- The task's original specification, acceptance criteria, handoff summary, and prior run history must be available through `kanban_show`.

## How to Run

This skill is loaded automatically by the review dispatcher. Start with `kanban_show` before inspecting files or choosing a verdict.

1. Read the task specification and the latest `review_requested` handoff.
2. **REQUIRED METHOD:** Before inspecting the detailed diff, read
   `references/contract-first-review.md`, pin the exact SHA, and write the
   operation invariants required there.
3. Inspect the actual deliverable through those contracts and run relevant
   verification.
4. Choose exactly one verdict: approve, request changes, or escalate.
5. Record concrete evidence in the terminal Kanban transition.

## Quick Reference

| Verdict | When | Final action |
|---|---|---|
| Approve | Acceptance criteria and contract-first verification pass; record literal `APPROVED` | `kanban_complete` |
| Request changes | Correctable implementation defects remain | `kanban_comment`, then `kanban_request_changes` |
| Escalate | A human decision or external prerequisite is required | `kanban_block` |

A requested-changes transition returns the task to its original implementer. When that implementer requests review again without naming a reviewer, the persisted reviewer provenance routes the re-review back to the same reviewer profile.

## Review Lenses

Contract-first is mandatory; lenses only change the supporting emphasis. Count
the `changes_requested` entries in "Prior attempts on this task" to distinguish
the independent review from its one permitted closure.

| Stage | Supporting lens | How to apply it |
|---|---|---|
| Independent review | Artifact | Use the Contract method to write invariants, then cold-read the exact SHA before the implementer's narrative or OCR report. Return one findings batch. |
| Focused closure | Execution | On the new exact SHA, use `terminal` to verify prior findings, changed boundaries, and relevant regressions; do not repeat the full review without a documented concrete cause. |
| Further HIGH class | Architecture stop | If two rounds reveal new classes of HIGH findings, stop the review loop and escalate the contract boundary instead of launching a third review. |

The contract, not a lens or helper, governs the verdict. Do not dispatch
parallel reviewers with `delegate_task`; Gauge is the sole independent reviewer
and may use a dependency map only as the optional navigation aid defined in the
reference.

## Procedure

### 1. Orient from the durable task record

Call `kanban_show` and identify:

- the original task body and acceptance criteria;
- the latest implementation summary and structured metadata;
- changed files, commit identifiers, and test evidence;
- comments and decisions from earlier runs;
- findings from prior review rounds.

Treat the handoff as a claim to verify, not as proof that the work is correct.

### 2. Compare requested behavior with delivered behavior

Map every acceptance criterion to concrete implementation or output evidence. Note omissions, changed semantics, and unrelated scope before deciding whether to run deeper checks.

For code work:

1. Apply every step in `references/contract-first-review.md` to the frozen exact
   SHA before reading helper findings.
2. Use `read_file` and `search_files` to inspect the changed paths and their callers.
3. Use `terminal` to inspect the diff and run the project's existing focused tests, lint, type checks, or build commands.
4. Exercise the reported failure path and at least one ordinary control path when practical.
5. Check error handling, edge cases, concurrency boundaries, data preservation, security boundaries, and cross-platform behavior relevant to the change.
6. Confirm that tests assert behavior rather than merely snapshotting source text or constants.

For non-code work:

1. Inspect the complete deliverable rather than only its summary.
2. Check correctness, completeness, formatting, and provenance.
3. Validate referenced URLs or external facts with the appropriate native tools when they affect the verdict.

### 3. Choose one verdict

#### Approve

Approve only when the acceptance criteria are satisfied, the contract-first
review has no HIGH finding, and the evidence is sufficient. The transition
summary must contain the literal verdict `APPROVED`. Call:

```text
kanban_complete(
    summary="APPROVED — <what was verified>",
    metadata={"review_outcome": "approved", "reviewer_checks": [...]}
)
```

Include the exact checks that passed and any bounded caveat that does not block acceptance.

#### Request changes

Use this for specific, correctable defects. First record actionable findings:

```text
kanban_comment(
    task_id="<current-task-id>",
    body="Changes requested:\n1. <file or artifact + defect>\n2. <required correction>",
)
```

Then return the same task to its implementer:

```text
kanban_request_changes(
    reason="<concise summary of the required corrections>"
)
```

State where the defect is, how it reproduces, why it violates the task, and what minimum outcome would resolve it. The transition does not use blocker recurrence accounting.

#### Escalate

Use escalation only when the reviewer and implementer cannot resolve the problem without a human decision or external prerequisite:

```text
kanban_block(
    reason="escalation: <decision or prerequisite required>"
)
```

Explain the blocked decision and the smallest information needed to continue.

### 4. Preserve role separation

Do not edit the implementation while acting as reviewer. Request changes and let the implementer produce the next candidate; then independently verify that candidate in the next review run.

## Pitfalls

- **Rubber-stamping:** A passing handoff summary is not independent evidence.
- **Reviewer implementation:** Editing the deliverable hides ownership and weakens the re-review boundary.
- **Vague findings:** “Needs work” does not give the implementer a reproducible correction target.
- **Style-only blocking:** Do not request changes for preference-level nits when behavior and repository standards are satisfied.
- **Skipping prior rounds:** Re-review must confirm both the requested corrections and preservation of previously passing behavior.
- **Using blockers for ordinary rework:** Correctable defects belong in `kanban_request_changes`; reserve `kanban_block` for genuine external blockers or human decisions.
- **Completing without evidence:** Every approval summary must name the checks or artifacts actually inspected.

## Verification

Before submitting the verdict, confirm:

- [ ] `kanban_show` was read for the current task and run.
- [ ] Every acceptance criterion was mapped to evidence.
- [ ] The actual deliverable was inspected.
- [ ] Relevant focused checks were run or an explicit reason was recorded when execution was impossible.
- [ ] Prior requested changes were re-tested on re-review.
- [ ] Unrelated regressions and scope changes were considered.
- [ ] The verdict uses exactly one terminal action.
- [ ] The summary contains concrete, non-secret evidence.
- [ ] No implementation files were edited by the reviewer.
