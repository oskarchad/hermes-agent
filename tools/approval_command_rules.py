"""Opt-in command selectors, separate from the legacy whole-text deny globs.

This is a shell guardrail, not an interpreter or a sandbox. Keep argument data
out of command position and never execute a command to discover its meaning.
"""

import os
import re

from tools.approval_detection import (
    _bash_exec_payload,
    _command_parser_limit_exceeded,
    _deobfuscate_shell_word_for_detection,
    _iter_shell_command_word_spans,
    _iter_top_level_shell_segments,
    _MALFORMED_EXEC_DESCRIPTION,
    _PARSER_LIMIT_DESCRIPTION,
    _scan_shell,
    _shell_tokens_with_spans,
    _splice,
)

_SHELLS = {"bash", "sh", "zsh", "ksh", "dash"}
_CONTROL_PREFIXES = {"if", "then", "elif", "else", "while", "until", "do", "!"}
_REDIRECT = re.compile(r"(?:<<-|<<<|<<|>>|<>|>&|<&|>\||>|<)")


class CommandDenyParseError(ValueError):
    """An enabled selector could not safely inspect an executable payload."""


def _command_tokens(command: str, start: int) -> list[str]:
    end = len(command)
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


def _command_words(source: str):
    # Reuse wrapper/assignment handling after unquoted shell control keywords.
    for _ in range(12):
        words = list(_iter_shell_command_word_spans(source))
        edits = [(start, end, " " * (end - start))
                 for start, end, word in words if word in _CONTROL_PREFIXES]
        if not edits:
            yield from words
            return
        source = _splice(source, edits)
    raise CommandDenyParseError(_PARSER_LIMIT_DESCRIPTION)


def _heredoc_expansions(body: str):
    # Unlike shell source, heredoc quotes are data, not expansion suppressors.
    i = 0
    while i < len(body):
        if body[i] == "\\":
            i += 2
            continue
        if body[i] == "`" or body.startswith("$(", i):
            kind, _, end, _ = next(_scan_shell(body, i, subst="u", stop_unterminated=True))
            if kind != "subst" or end is None:
                raise CommandDenyParseError(_MALFORMED_EXEC_DESCRIPTION)
            yield body[i + (1 if body[i] == "`" else 2):end - 1]
            i = end
        else:
            i += 1


def _command_source(source: str, pending: list[str]) -> str:
    """Remove redirection-owned data while retaining separately executed source.

    Substitutions are queued before masking, including redirection operands.
    Heredocs are consumed only after the header newline and in declaration order.
    These edits are local to the new selector, never legacy detection variants.
    """
    chars = list(source)
    cursor = segment_start = 0
    heredocs = []
    while cursor < len(source):
        skip = cursor
        for kind, i, j, quote in _scan_shell(source, cursor, subst="uq", stop_unterminated=True):
            if i < skip:
                continue
            if j is None:
                raise CommandDenyParseError(_MALFORMED_EXEC_DESCRIPTION)
            if kind == "subst":
                pending.append(source[i + (1 if source[i] == "`" else 2):j - 1])
                # Preserve an operand, not whitespace that could shift argv.
                chars[i:j] = "X" * (j - i)
            if kind != "char" or quote is not None:
                continue
            if source[i] in "<>" and (match := _REDIRECT.match(source, i)):
                op = match.group()
                start = match.end()
                while start < len(source) and source[start] in " \t":
                    start += 1
                end = start
                for k, a, b, q in _scan_shell(source, start, subst="uq", stop_unterminated=True):
                    if b is None:
                        raise CommandDenyParseError(_MALFORMED_EXEC_DESCRIPTION)
                    if k == "char" and q is None and (source[a].isspace() or source[a] in ";&|<>()"):
                        break
                    end = b
                raw = source[start:end]
                words = _shell_tokens_with_spans(raw, 0)
                if not words:
                    raise CommandDenyParseError(_MALFORMED_EXEC_DESCRIPTION)
                if op in {"<<", "<<-"}:
                    heredocs.append((words[0][0], raw != words[0][0], op == "<<-", segment_start))
                else:
                    # The outer iterator skips this operand, so retain its active expansions here.
                    for k, a, b, _ in _scan_shell(raw, subst="uq", stop_unterminated=True):
                        if b is None:
                            raise CommandDenyParseError(_MALFORMED_EXEC_DESCRIPTION)
                        if k == "subst":
                            pending.append(raw[a + (1 if raw[a] == "`" else 2):b - 1])
                fd_start = i
                while fd_start > 0 and source[fd_start - 1].isdigit():
                    fd_start -= 1
                if fd_start and not (source[fd_start - 1].isspace() or source[fd_start - 1] in ";&|()"):
                    fd_start = i
                chars[fd_start:end] = " " * (end - fd_start)
                skip = end
            elif source[i] == "\n" and heredocs:
                cursor = i + 1
                for delimiter, quoted, strip_tabs, header_start in heredocs:
                    body_start = cursor
                    while cursor < len(source):
                        line_end = source.find("\n", cursor)
                        line_end = len(source) if line_end < 0 else line_end
                        line = source[cursor:line_end]
                        if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                            break
                        cursor = min(line_end + 1, len(source))
                    else:
                        raise CommandDenyParseError(_MALFORMED_EXEC_DESCRIPTION)
                    body = source[body_start:cursor]
                    header = next(_iter_top_level_shell_segments("".join(chars[header_start:i])), "")
                    shell_input = any(os.path.basename(_deobfuscate_shell_word_for_detection(w)) in _SHELLS
                                      for _, _, w in _command_words(header))
                    if shell_input:
                        pending.append(body)
                    elif not quoted:
                        pending.extend(_heredoc_expansions(body))
                    cursor = min(line_end + 1, len(source))
                    chars[body_start:cursor] = " " * (cursor - body_start)
                heredocs.clear()
                segment_start = cursor
                break
            elif source[i] in ";&|\n":
                segment_start = i + 1
        else:
            if heredocs:
                raise CommandDenyParseError(_MALFORMED_EXEC_DESCRIPTION)
            break
    return "".join(chars)


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
            payload = _command_source(payload, pending)
            for start, _, raw_word in _command_words(payload):
                name = os.path.basename(_deobfuscate_shell_word_for_detection(raw_word))
                tokens = _command_tokens(payload, start)
                if not tokens:
                    continue
                if name in {"gh", "gh.exe"} and _is_gh_pr_merge(tokens[1:]):
                    return True
                if name in _SHELLS:
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
