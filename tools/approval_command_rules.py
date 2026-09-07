"""Opt-in command selectors, separate from the legacy whole-text deny globs.

This is a shell guardrail, not an interpreter or a sandbox. Keep argument data
out of command position and never execute a command to discover its meaning.
"""

import os
import re
import shlex

from tools.approval_detection import (
    _bash_exec_payload,
    _BASH_OPTIONS_WITH_ARG,
    _COMMAND_WRAPPER_WORDS,
    _command_parser_limit_exceeded,
    _ENV_ASSIGNMENT_RE,
    _iter_shell_command_starts,
    _iter_top_level_shell_segments,
    _MALFORMED_EXEC_DESCRIPTION,
    _PARSER_LIMIT_DESCRIPTION,
    _scan_shell,
    _shell_tokens_with_spans,
    _splice,
    _SUDO_OPTIONS_WITH_ARG,
)

_SHELLS = {"bash", "sh", "zsh", "ksh", "dash"}
_CONTROL_PREFIXES = {"if", "then", "elif", "else", "while", "until", "do", "!"}
_REDIRECT = re.compile(r"(?:<<-|<<<|<<|>>|<>|>&|<&|>\||>|<)")


class CommandDenyParseError(ValueError):
    """An enabled selector could not safely inspect an executable payload."""


def _literal_word(raw: str) -> str:
    # Decode literal ANSI-C spelling without promoting its contents to syntax.
    # Escaped ANSI-C forms are deliberately unsupported, rather than guessed.
    edits = []
    for kind, i, _, quote in _scan_shell(raw):
        if kind == "char" and quote is None and raw.startswith("$'", i):
            end = raw.find("'", i + 2)
            if end < 0 or "\\" in raw[i + 2:end]:
                raise CommandDenyParseError(_MALFORMED_EXEC_DESCRIPTION)
            edits.append((i, i + 1, ""))
    try:
        words = shlex.split(_splice(raw, edits), posix=True)
    except ValueError:
        raise CommandDenyParseError(_MALFORMED_EXEC_DESCRIPTION) from None
    if len(words) != 1:
        raise CommandDenyParseError(_MALFORMED_EXEC_DESCRIPTION)
    return words[0]


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
    return [_literal_word(command[start + a:start + b]) for _, a, b, _ in tokens]


def _command_words(source: str):
    # A case arm's closing delimiter starts a command, just like a subshell's
    # opening delimiter. Keep this stricter traversal local to the opt-in rule.
    starts = set(_iter_shell_command_starts(source))
    starts.update(i + 1 for kind, i, _, quote in _scan_shell(source, subst="uq")
                  if kind == "char" and quote is None and source[i] == ")")
    for start in sorted(starts):
        end = start
        for kind, i, j, quote in _scan_shell(source, start, subst="uq"):
            if kind == "char" and quote is None and source[i] in ";&|\n(){}":
                break
            end = j
        tokens = _shell_tokens_with_spans(source[start:end], 0)
        if tokens is None:
            raise CommandDenyParseError(_MALFORMED_EXEC_DESCRIPTION)
        wrapper = None
        skip_arg = False
        for index, (_, a, b, _) in enumerate(tokens):
            if index >= 12:
                raise CommandDenyParseError(_PARSER_LIMIT_DESCRIPTION)
            raw = source[start + a:start + b]
            word = _literal_word(raw)
            if skip_arg:
                skip_arg = False
                continue
            if wrapper and word == "--":
                wrapper = None
                continue
            if wrapper and word.startswith("-"):
                if wrapper == "command" and word in {"-v", "-V"}:
                    break  # Lookup, not invocation.
                skip_arg = wrapper in {"sudo", "env"} and word in _SUDO_OPTIONS_WITH_ARG
                continue
            if raw in _CONTROL_PREFIXES or _ENV_ASSIGNMENT_RE.fullmatch(word):
                continue
            yield start + a, start + b, raw
            name = os.path.basename(word)
            if name not in _COMMAND_WRAPPER_WORDS:
                break
            wrapper = name
        else:
            if skip_arg:
                raise CommandDenyParseError(_MALFORMED_EXEC_DESCRIPTION)


def _shell_stdin_source(header: str) -> bool:
    header = next(_iter_top_level_shell_segments(header), "")
    words = list(_command_words(header))
    if not words:
        return False
    start, _, raw = words[-1]
    if os.path.basename(_literal_word(raw)) not in _SHELLS:
        return False
    args = _command_tokens(header, start)[1:]
    if _bash_exec_payload(args)[0]:
        return False
    index, stdin = 0, False
    while index < len(args):
        token = args[index]
        if token == "--":
            index += 1
            break
        if not token.startswith(("-", "+")) or token == "-":
            break
        if token in _BASH_OPTIONS_WITH_ARG:
            index += 2
            continue
        if not token.startswith("--"):
            stdin |= token.startswith("-") and "s" in token[1:]
            index += int(bool(set(token[1:]) & {"o", "O"}))
        index += 1
    return stdin or index == len(args) or args[index:] == ["-"]


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
    herestrings = []
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
                if op not in {"<<", "<<-"}:
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
                stdin = source[fd_start:i] in {"", "0"}
                if op in {"<<", "<<-"}:
                    heredocs.append((words[0][0], raw != words[0][0], op == "<<-", segment_start, stdin))
                elif op == "<<<" and stdin:
                    herestrings.append((_literal_word(raw), segment_start))
                chars[fd_start:end] = " " * (end - fd_start)
                skip = end
            elif source[i] == "\n" and heredocs:
                cursor = i + 1
                for delimiter, quoted, strip_tabs, header_start, stdin in heredocs:
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
                    header = "".join(chars[header_start:i])
                    if stdin and _shell_stdin_source(header):
                        pending.append(body)
                    if not quoted:
                        pending.extend(_heredoc_expansions(body))
                    cursor = min(line_end + 1, len(source))
                    chars[body_start:cursor] = " " * (cursor - body_start)
                heredocs.clear()
                segment_start = cursor
                break
            elif source[i] in ";&|\n()":
                segment_start = i + 1
        else:
            if heredocs:
                raise CommandDenyParseError(_MALFORMED_EXEC_DESCRIPTION)
            break
    cleaned = "".join(chars)
    for body, header_start in herestrings:
        # Redirections may precede the executable or its -c/script arguments.
        header_end = len(cleaned)
        for kind, i, _, quote in _scan_shell(cleaned, header_start, subst="uq"):
            if kind == "char" and quote is None and cleaned[i] in ";&|\n()":
                header_end = i
                break
        if _shell_stdin_source(cleaned[header_start:header_end]):
            pending.append(body)
    return cleaned


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
                name = os.path.basename(_literal_word(raw_word))
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
                    args = tokens[2:] if tokens[1:2] == ["--"] else tokens[1:]
                    pending.append(" ".join(args))
        except RecursionError:
            raise CommandDenyParseError(_PARSER_LIMIT_DESCRIPTION) from None
    return False
