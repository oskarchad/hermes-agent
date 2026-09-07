# ADR 0003: Opt-in command-aware approval deny

Status: Accepted

## Context

Whole-command deny globs also match quoted PR descriptions. Changing their
meaning would silently weaken existing operator policy. The operator authorized
a bounded new selector through Captain task t_c49aa2bb and implementation task
t_83ff0905, not a runtime release or a change to live policy.

## Decision

- Add `approvals.deny_commands`, a list of strings with default `[]`. The only
  supported selector in this change is exactly `gh pr merge`; invalid types or
  unsupported entries return an explicit fail-closed configuration error.
- Keep `approvals.deny` glob semantics unchanged. Both are unconditional floors
  before yolo, mode off, and permanent allowlists in the existing approval path.
- Match executable command positions, including binary paths, gh repository
  options, shell command strings and active substitutions. Quoted PR body data
  is not a command. Reuse the existing shell scanner; never execute a sample to
  discover its meaning. Malformed/over-limit inspection fails closed.
- Keep profile ownership and the mtime-keyed `load_config_readonly` source. No
  policy mirror, new tool, external service, DSL, or dependency is introduced.
- This is the same honest-but-wrong-agent guardrail as legacy deny, not a shell
  interpreter or adversarial sandbox. It does not resolve arbitrary variables,
  aliases, scripts on disk, or programs that invoke GitHub through other APIs.

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
deny entry. Adding the selector while retaining the defective glob does not fix
false positives. No automatic migration or live config write is part of this PR.
For rollback to a runtime without this key, restore the former merge glob first
so protection is not silently lost, then remove the unsupported key. This ADR
does not authorize merge, deployment, restart, or PR6 verdict publication.
