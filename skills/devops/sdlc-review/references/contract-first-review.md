# Contract-First Code Review

Gauge uses this method for the independent review of a frozen code candidate. It
turns acceptance criteria into operation contracts before implementation detail
can frame the review; it does not create another reviewer, workflow, or verdict
source.

## Responsibility topology

1. The author implements and tests the change.
2. OCR eligibility and obligation remain owned by
   `open-code-review-worker-helper`. When that gate requires Open Code Review
   for high-risk work or at least eight changed source/test files, the author
   runs it at most once before freezing the final candidate. OCR is supporting
   evidence, never a verdict, never authoritative `APPROVED`, and is not rerun
   after every correction.
3. Gauge performs one independent cold-read review of the final exact SHA using
   this contract-first method. Gauge writes its own analysis before reading any
   OCR report, then may use that report as supporting evidence rather than a
   substitute for review.
4. Gauge is the only verdict authority. CI and tests are technical evidence,
   not approval.
5. A dependency map or graph is an optional navigation aid only when manual
   contract closure leaves unresolved callers or boundaries between modules or
   languages. Never treat a graph as a reviewer or verdict source. Never pass a
   large raw graph; retain only the relevant paths and unresolved questions.

## Method

### 1. Freeze the contract before the diff

Pin the task, acceptance criteria, and exact SHA. Before reading detailed diff
implementation, write 3–7 operation invariants that must hold across success,
failure, retry, and recovery. Each invariant states:

- the accepted input and validation boundary;
- the authoritative state transition or explicit no-write outcome;
- what the consumer may observe;
- the terminal event or operator-visible outcome.

### 2. Trace every material flow

For each material operation, follow the real production path end to end:

`producer → validation → authoritative/stored state → consumer → event/operator-visible outcome`

Name concrete symbols, schemas, stores, adapters, and emitted events. A helper
or unit test with no reachable production caller is not closure evidence.
Include queue or dispatch, persistence, retry and recovery checkpoints, and
the externally visible effect whenever they are part of the operation.

### 3. Exercise the failure matrix

Check every row that can apply. Mark a row not applicable only with a concrete
contract reason, not because the diff or tests omit it.

| Case | Required question |
|---|---|
| success | Does validated state reach the consumer and visible outcome once? |
| transport exception | Is the exception typed, retryable only where intended, and observable? |
| typed/returned error | Can an error value be mistaken for a successful transport result? |
| malformed payload | Does validation reject it before authoritative mutation? |
| empty result | Is empty distinct from failure and from successful content? |
| partial result | Is partial progress stored, resumed, or rejected without false success? |
| timeout/rate limit | Who retries, with what budget/backoff, and what becomes visible? |
| crash/reclaim | Which durable checkpoint and lease authority permit safe recovery? |
| retry exhaustion | Which terminal state and event prevent an endless or false-success loop? |

### 4. Assign owners and prove boundaries

Record one accountable owner for each item:

- retry owner;
- terminal-state owner;
- checkpoint owner;
- exactly-once/external-write owner;
- operator-visible-outcome owner.

For every critical boundary, run at least one negative probe or cite concrete
equivalent evidence that demonstrates rejection, preservation, or recovery on
the real path. Green happy-path tests alone do not satisfy this requirement.

### 5. Return one bounded result

Return all material findings in one batch bound to one exact SHA. A HIGH blocks;
Gauge may approve only with the literal `APPROVED`. For each blocking finding,
name the path and line (or nearest stable symbol), violated invariant, reachable
scenario, observed or statically proven behavior, material impact, and minimum
correction boundary. Missing or contradictory proof is an evidence gap: record
it separately from an implementation defect, and never produce approval from
an unresolved gap.

After corrections, Gauge performs at most one focused closure on the new exact
SHA. The closure covers the prior findings, changed contract boundaries, and
relevant regressions. A second full review is permitted only when the correction
diff changes an operation contract and invalidates earlier evidence; record the
exact contract and changed path. If two permitted full-review rounds reveal new
classes of HIGH findings, declare an architecture stop, open a new architecture
task or issue, and stop returning the same task for another review.

## Review worksheet

```text
TARGET_SHA: <exact SHA>
INVARIANTS (3–7): <input; state/no-write; consumer; visible outcome>
MATERIAL_FLOWS: <producer → validation → state → consumer → event/outcome>
FAILURE_MATRIX: <success through retry exhaustion; evidence or reason N/A>
OWNERS: <retry; terminal state; checkpoint; exactly-once/write; visible outcome>
NEGATIVE_PROBES: <one or equivalent evidence per critical boundary>
FINDINGS: <one batch bound to TARGET_SHA>
DECISION: APPROVED | CHANGES_REQUIRED | ESCALATION_REQUIRED
```

## Red flags

- Reading an OCR report, implementer summary, or large graph before writing the
  invariants.
- Letting deadline, author seniority, or sunk implementation cost replace the
  contract derivation and cold read.
- Treating a transport exception test as coverage of returned errors, malformed
  payloads, empty or partial results, or retry exhaustion.
- Approving from green CI or tests, helper consensus, or absence of findings.
- Starting a second full review when the correction diff does not change an
  operation contract.
