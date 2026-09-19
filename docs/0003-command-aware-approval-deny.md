# ADR 0003: Opt-in command-aware approval deny

Status: Accepted

## Context

Whole-command deny globs also match quoted PR descriptions. Changing their
meaning would silently weaken existing operator policy. The operator authorized
a bounded new selector through Captain task t_c49aa2bb and implementation task
t_83ff0905, not a runtime release or a change to live policy. Task t_c9e6d262
explicitly extends the original gh-only scope to an explicit Git push destination
selector after private policy probes proved that whole-script globs could not
preserve both legal feature publication and main protection.

## Decision

- Add `approvals.deny_commands`, a list of strings with default `[]`. Supported
  selectors are exactly `gh pr merge` and `git push main`; invalid types or
  unsupported entries return an explicit fail-closed configuration error.
- Keep `approvals.deny` glob semantics unchanged. Both are unconditional floors
  before yolo, mode off, and permanent allowlists in the existing approval path.
- Match executable command positions, including binary paths, gh repository
  options, shell command strings and active substitutions. Quoted PR body data
  is not a command. Reuse the existing shell scanner; never execute a sample to
  discover its meaning. Malformed/over-limit inspection fails closed.
- `git push main` matches explicit destination `main` or `refs/heads/main`,
  including `source:destination`, a leading `+`, and deletion refspecs/options.
  Repository operands and option values are not refs; `main:feature` and
  `tag main` do not name the protected branch. Later commands' `--base main`
  and quoted PR descriptions remain data. Both selectors use one shell scanner.
- Shell stdin with unresolved program provenance (including FD duplication)
  fails closed, rather than emulating descriptor state. Unused non-stdin FDs
  and stdin for a shell running `-c` or a script remain input data. Unsupported
  executable quoting fails closed only when decoding execution-relevant words;
  ordinary escaped ANSI-C PR body data is not decoded as executable source.
- Keep profile ownership and the mtime-keyed `load_config_readonly` source. No
  policy mirror, new tool, external service, DSL, or dependency is introduced.
- This is the same honest-but-wrong-agent guardrail as legacy deny, not a shell
  interpreter or adversarial sandbox. It does not resolve arbitrary variables,
  aliases, scripts on disk, Git config/remotes/default ref resolution, wildcard
  or implicit pushes (`--all`, `--mirror`, or omitted refspecs), or programs that
  invoke GitHub through other APIs. Explicit refs are case-sensitive. Unknown
  Git options whose operand ownership cannot be established fail closed.

This explicitly extends the retained-behavior scope of ADR 0001 for this bounded
approval feature only; the upstream-seam preference and exact-SHA review/release
gates remain unchanged. ADR 0002's overlay maintenance direction still applies.

## Contract and migration

The [configuration defaults](../hermes_cli/config_defaults.py) are the settings
catalog; the [security reference](../website/docs/user-guide/security.md) defines
user-facing semantics. The [tools rules](../tools/AGENTS.md) govern integration.

After independent review and a separately authorized runtime release, the policy
owner may replace only the defective merge glob (or its three-glob attempted
replacement) with `deny_commands: ['gh pr merge']`, preserving every unrelated
deny entry. For the bounded Git extension, the owner may similarly replace only
the defective git-main globs with `git push main`; do not enable a previously
absent merge policy as a side effect. Adding selectors while retaining defective globs does not fix
false positives. No automatic migration or live config write is part of this PR.
For rollback to a runtime without a selector, restore its former globs first
so protection is not silently lost, then remove the unsupported selector. This ADR
does not authorize merge, deployment, restart, or PR6 verdict publication.
