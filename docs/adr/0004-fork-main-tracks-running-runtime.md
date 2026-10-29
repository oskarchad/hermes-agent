# ADR 0004: Fork `main` tracks the running runtime line

Status: Accepted (Oskar, Discord 1565206895629705228; task t_2313e6e4)

## Context

Fork `main` (`a8391c76`, 13 fork PR merges on upstream `79445a49`) and the
running runtime (`44fdc241`, `backup/live-*` on upstream `97a9ae8e` plus
PR30-32) diverged. `main` was not an ancestor of the runtime, so a main-only
release would have rolled the runtime back about 19.5k upstream commits, and
runtime fixes could only land as PRs against `backup/live-*` branches.

## Decision

- One adoption merge joins both histories. Its parents are the old `main` and
  the runtime commit, and its tree is the runtime tree. History is not
  rewritten and nothing is force-pushed. The two lines stay reachable from
  `preserve/main-a8391c76-t_2313e6e4` and `preserve/live-44fdc241-t_2313e6e4`.
- From then on, fork `main` is the only release source for the runtime
  checkout. New work branches from `main`, and PRs target `main`, not
  `backup/live-*`.
- Main-only custom additions are carried forward only when a real consumer
  needs them in this tree. The adoption PR records the KEEP /
  ALREADY_EQUIVALENT / RETIRED disposition for each one.
- `docs/ADR.md` is retired. Upstream deleted it (`0c0875b746`), its index
  entries are covered by this directory's `README.md`, and its remaining
  entry describes upstream plugin-manager code. It stays readable in
  history through the old `main` parent.
- Fork CI keeps the upstream workflows unchanged except for
  `github.repository == 'oskarchad/hermes-agent'` guards: standard-runner
  fallbacks (the fork has no larger hosted runners) and, in `tests.yml`,
  `timeout-minutes: 60` and `HERMES_TEST_WORKERS=4` sized for those runners.
- Main-only Honcho HER-155 noise filter (`plugins/memory/honcho`, source
  `b7a722c6`) is NOT_CARRIED and remains an OPEN GAP: the contract is still
  required, but the active consumer is the out-of-repo catalog plugin
  `profiles/otto/plugins/honcho`, which lacks the filter in the running
  runtime as well. Porting it is a separate card owned by Otto.

## Consequences

Installing the adoption merge does not change any runtime file. Upgrading
upstream stays a separate overlay decision (ADR 0002). This ADR authorizes no
merge, deploy or restart; those stay with the operator gates.
