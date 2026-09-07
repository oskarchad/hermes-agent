"""Opt-in command selectors, separate from the legacy whole-text deny globs.

This is a shell guardrail, not an interpreter or a sandbox. Keep argument data
out of command position and never execute a command to discover its meaning.
"""

import os

from tools.approval_detection import (
    _bash_exec_payload,
    _command_parser_limit_exceeded,
    _deobfuscate_shell_word_for_detection,
    _iter_shell_command_word_spans,
    _MALFORMED_EXEC_DESCRIPTION,
    _PARSER_LIMIT_DESCRIPTION,
    _scan_shell,
    _shell_tokens_with_spans,
)


class CommandDenyParseError(ValueError):
    """An enabled selector could not safely inspect an executable payload."""


def _command_tokens(command: str, start: int) -> list[str]:
    end = len(command)
    scope_start = 0
    # A command inside a substitution ends before its closing delimiter. Starting
    # a fresh scan at the command word would misread a closing backtick as open.
    while True:
        for kind, i, j, _ in _scan_shell(command, scope_start, end, subst="uq", stop_unterminated=True):
            if j is None:
                raise CommandDenyParseError(_MALFORMED_EXEC_DESCRIPTION)
            if kind == "subst" and i < start < j:
                scope_start = i + (1 if command[i] == "`" else 2)
                end = j - 1
                break
        else:
            break
    for kind, i, j, quote in _scan_shell(command, start, end, subst="uq", stop_unterminated=True):
        if j is None:
            raise CommandDenyParseError(_MALFORMED_EXEC_DESCRIPTION)
        if kind == "char" and quote is None and command[i] in ";&|\n(){}":
            end = i
            break
    tokens = _shell_tokens_with_spans(command[start:end], 0)
    if tokens is None:
        raise CommandDenyParseError(_MALFORMED_EXEC_DESCRIPTION)
    return [token[0] for token in tokens]


def _is_gh_pr_merge(args: list[str]) -> bool:
    # gh's inherited repo option owns its value, even when that value looks like
    # a subcommand. Do not search the remaining operands after a different verb.
    expected = iter(("pr", "merge"))
    word = next(expected)
    index = 0
    while index < len(args):
        token = args[index]
        if token in {"--help", "-h", "--version"}:
            return False
        if token in {"--repo", "-R"}:
            index += 2
            continue
        if token.startswith("--repo=") or (token.startswith("-R") and len(token) > 2):
            index += 1
            continue
        if token != word:
            return False
        word = next(expected, None)
        if word is None:
            return True
        index += 1
    return False


def matches_gh_pr_merge(command: str) -> bool:
    """Inspect executable positions and shell-carried payloads, never prose.

    Use raw quote state: global deobfuscation can turn escaped data into live
    substitutions. The existing scanner also exposes substitutions inside double
    quotes, but never those inside single-quoted data.
    """
    pending, seen = [command], set()
    inspected = 0
    while pending:
        payload = pending.pop()
        if payload in seen:
            continue
        seen.add(payload)
        inspected += len(payload)
        if inspected > 128_000 or _command_parser_limit_exceeded(payload):
            raise CommandDenyParseError(_PARSER_LIMIT_DESCRIPTION)
        # Shell line continuations disappear before word splitting.
        payload = payload.replace("\\\n", "").replace("\\\r\n", "")
        try:
            for start, _, raw_word in _iter_shell_command_word_spans(payload):
                name = os.path.basename(_deobfuscate_shell_word_for_detection(raw_word))
                tokens = _command_tokens(payload, start)
                if not tokens:
                    continue
                if name in {"gh", "gh.exe"} and _is_gh_pr_merge(tokens[1:]):
                    return True
                if name in {"bash", "sh", "zsh", "ksh", "dash"}:
                    found, nested = _bash_exec_payload(tokens[1:])
                    if found:
                        if nested is None:
                            raise CommandDenyParseError(_MALFORMED_EXEC_DESCRIPTION)
                        pending.append(nested)
                elif name == "eval":
                    pending.append(" ".join(tokens[1:]))
        except RecursionError:
            raise CommandDenyParseError(_PARSER_LIMIT_DESCRIPTION) from None
    return False
