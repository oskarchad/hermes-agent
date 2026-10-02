"""Conservative heredoc masking for shell-command scanners ('&' guard, blocked-command checks,
cron lifecycle_guard) that false-positive on heredoc *bodies*. Stripping every body is unsafe the
other way (a fake ``<<`` in quotes can swallow an operator; unquoted bodies expand; ``bash <<'EOF'``
executes), so a body is masked ONLY when every delimiter is quoted, every heredoc has an exact
terminator line, the owning simple command is an allowlisted non-shell interpreter, and no list
operator follows the heredoc. Otherwise the command is returned untouched: a false positive is
acceptable, hiding shell syntax from a guard is not.
Masked bodies keep their newline count (re.MULTILINE)."""

from __future__ import annotations

import re

# Non-shell interpreters whose quoted heredoc bodies are data for THAT interpreter; optional
# VAR=... assignments, ``env`` and a path prefix allowed. Narrow on purpose: unmatched = visible.
_INERT_HEREDOC_CONSUMER_RE = re.compile(
    r"^\s*(?:[A-Z_][A-Z0-9_]*=\S+\s+)*(?:env\s+)?(?:[A-Za-z0-9_./-]+/)?"
    r"(?:python(?:3(?:\.\d+)*)?|osascript|cat)(?=\s|$)",
    re.IGNORECASE)


def _span_end(command: str, cursor: int, closer: str) -> int:
    """Index just past the backslash-aware span opened at ``cursor``."""
    end = cursor + 1
    while end < len(command):
        if command[end] == closer:
            return end + 1
        end += 2 if command[end] == "\\" and end + 1 < len(command) else 1
    return end


def _mask_simple_quotes(command: str) -> str:
    """Blank inert quoted spans; keep ``$(``/backtick-bearing ones visible."""
    result = []
    cursor = 0
    while cursor < len(command):
        char = command[cursor]
        if char in "'\"":  # single quotes have no escapes; double quotes are backslash-aware
            end = (command.find("'", cursor + 1) + 1 if char == "'"
                   else _span_end(command, cursor, '"'))
            segment = command[cursor:end]
            if not segment.endswith(char):
                result.append(command[cursor:])
                break
            keep = char == '"' and ("$(" in segment or "`" in segment)
            result.append(segment if keep else char * 2)
            cursor = end
        elif char == "`":
            end = _span_end(command, cursor, "`")
            result.append(command[cursor:end])
            cursor = end
        else:
            result.append(char)
            cursor += 1
    return "".join(result)


def _parse_heredoc_operator(command: str, index: int):
    """Parse one ``<<`` opener -> ``(end_index, delimiter, strip_tabs, quoted)`` or None."""
    if not command.startswith("<<", index) or command.startswith("<<<", index):
        return None
    strip_tabs = command.startswith("-", index + 2)
    cursor = index + 3 if strip_tabs else index + 2
    while cursor < len(command) and command[cursor] in " \t":
        cursor += 1
    if cursor >= len(command) or command[cursor] in "\r\n":
        return None
    delimiter: list[str] = []
    quoted = False
    while cursor < len(command) and not (command[cursor].isspace() or command[cursor] in ";&|<>()"):
        char = command[cursor]
        if char == "\\":  # backslash-escaped char: quoted, literal
            if cursor + 1 >= len(command) or command[cursor + 1] in "\r\n":
                return None
            quoted = True
            delimiter.append(command[cursor + 1])
            cursor += 2
        elif char in "'\"":
            quoted = True
            cursor += 1
            while cursor < len(command) and command[cursor] != char:
                current = command[cursor]
                if current in "\r\n":
                    return None
                if char == '"' and current == "\\":
                    if cursor + 1 >= len(command):
                        return None
                    if command[cursor + 1] in '$`"\\\n':  # else backslash is literal in dquotes
                        cursor += 1
                        current = command[cursor]
                delimiter.append(current)
                cursor += 1
            if cursor >= len(command):  # unterminated quote
                return None
            cursor += 1
        else:
            delimiter.append(char)
            cursor += 1
    if not delimiter and not quoted:
        return None
    return cursor, "".join(delimiter), strip_tabs, quoted


def _is_fd_redirect_ampersand(command: str, index: int) -> bool:
    """Return whether ``&`` at ``index`` belongs to ``>&``/``<&``/``&>`` redirection."""
    before = command[index - 1] if index else ""
    after = command[index + 1] if index + 1 < len(command) else ""
    return before in "<>" or after == ">"


def _scan_heredoc_command_unit(
        command: str, start: int, *, spec_owners: list[int] | None = None,
        list_operators: list[int] | None = None):
    """Scan one logical command.

    Return ``(end, specs, unknown_operator, post_heredoc_list_operator, owner_start)``.
    List operators before the first heredoc select the simple command that owns it. A list
    operator after a heredoc keeps the body visible because another command may consume it.
    Optional *spec_owners* receives, per heredoc spec, the start of the simple command that
    carries its ``<<`` (PR17 per-spec ownership); *list_operators* receives every list operator.
    """
    cursor = start
    quote = None
    comment = False
    specs = []
    unknown_operator = False
    post_heredoc_list_operator = False
    owner_start = start
    simple_start = start
    while cursor < len(command):
        char = command[cursor]
        if char == "\n" and (comment or quote is None):
            break
        # Backslash escapes (incl. line continuations) outside single quotes skip the next char.
        escaped = char == "\\" and quote != "'" and not comment and cursor + 1 < len(command)
        if comment or quote is not None or escaped:
            if char == quote:
                quote = None
            cursor += 2 if escaped else 1
        elif char in "'\"`":
            quote = char
            cursor += 1
        elif char == "#" and (cursor == start or command[cursor - 1].isspace()
                              or command[cursor - 1] in ";&|()"):
            comment = True
            cursor += 1
        elif command.startswith("<<<", cursor):
            cursor += 3
        elif command.startswith("<<", cursor):
            parsed = _parse_heredoc_operator(command, cursor)
            if parsed is None:
                unknown_operator = True
                cursor += 2
            else:
                cursor, delimiter, strip_tabs, quoted = parsed
                specs.append((delimiter, strip_tabs, quoted))
                if spec_owners is not None:
                    spec_owners.append(simple_start)
        else:
            if char in ";|&" and not (
                char == "&" and _is_fd_redirect_ampersand(command, cursor)
            ):
                if specs:
                    post_heredoc_list_operator = True
                else:
                    owner_start = cursor + 1
                simple_start = cursor + 1
                if list_operators is not None:
                    list_operators.append(cursor)
            cursor += 1
    return cursor, specs, unknown_operator, post_heredoc_list_operator, owner_start


def _find_heredoc_close(
        command: str, body_start: int, delimiter: str, strip_tabs: bool) -> int | None:
    """Return the position after an exact shell heredoc terminator line."""
    cursor = body_start
    while True:
        newline = command.find("\n", cursor)
        after = len(command) if newline == -1 else newline + 1
        line = command[cursor:after].removesuffix("\n").removesuffix("\r")
        candidate = line.lstrip("\t") if strip_tabs else line
        if candidate == delimiter:
            return after
        if newline == -1:
            return None
        cursor = after


def strip_inert_heredoc_bodies(command: str) -> str:
    """Mask heredoc bodies that are provably inert data (see module docstring)."""
    # Runs on every terminal call: skip the state machine when no '<<' exists; stop past the last.
    if "<<" not in command:
        return command
    last_opener_index = command.rfind("<<")
    ranges: list[tuple[int, int]] = []
    command_start = 0
    while command_start <= last_opener_index:
        (
            command_end,
            specs,
            unknown_operator,
            post_heredoc_list_operator,
            owner_start,
        ) = _scan_heredoc_command_unit(command, command_start)
        if unknown_operator:
            return command
        if not specs:
            if command_end >= len(command):
                break
            command_start = command_end + 1
            continue
        if command_end >= len(command):
            return command  # opener with no body line: unterminated — leave visible
        body_cursor = command_end + 1
        body_ranges: list[tuple[int, int]] = []
        for delimiter, strip_tabs, _quoted in specs:
            close_end = _find_heredoc_close(command, body_cursor, delimiter, strip_tabs)
            if close_end is None:
                return command  # unterminated
            body_ranges.append((body_cursor, close_end))
            body_cursor = close_end
        if (
            all(quoted for _delimiter, _strip_tabs, quoted in specs)
            and not post_heredoc_list_operator
        ):
            masked_opener = _mask_simple_quotes(command[command_start:command_end])
            masked_owner = _mask_simple_quotes(command[owner_start:command_end])
            if not any(
                marker in masked_opener
                for marker in ("$(", "`", "<(", ">(", "(", ")", "{", "}")
            ) and _INERT_HEREDOC_CONSUMER_RE.search(masked_owner):
                ranges.extend(body_ranges)
        command_start = body_cursor
    # Single-pass rebuild (ranges are sorted and non-overlapping), bodies -> their newlines only.
    parts: list[str] = []
    previous = 0
    for start, end in ranges:
        parts += [command[previous:start], "\n" * command.count("\n", start, end)]
        previous = end
    return "".join(parts) + command[previous:]


# --- PR17: Python heredoc bodies for the cron lifecycle guard ----------------------------------
#
# Every quoted heredoc whose owning simple command is a Python interpreter is classified, so no
# Python body the inert masker above hides can escape language-aware inspection:
# * PROGRAM: the owner reads its program from this heredoc on stdin (``python3 - <<'EOF'``, with
#   optional VAR=/env prefixes, flags, argv after ``-`` and output redirections, and with any list
#   operator after it, e.g. ``2>&1 | tail``). The body is split out of the shell source and
#   inspected strictly.
# * OTHER: any other Python-owned body (script/``-c``/``-m`` operand, unquoted, input redirection,
#   unusual quoting). It stays in the shell source untouched (upstream masking applies) and is
#   inspected additively: a literal lifecycle process call in it still blocks.

_PY_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=\S*")
_PY_INTERPRETER_RE = re.compile(r"(?:[A-Za-z0-9_./-]+/)?python(?:3(?:\.\d+)*)?", re.IGNORECASE)
_PY_OWNER_TOKEN_RE = re.compile(r"(?:^|[\s/])python[0-9.]*(?:\.exe)?(?=\s|$)", re.IGNORECASE)
_PY_FLAG_RE = re.compile(r"-[BEIOPRSbdqsuvx]+|-[WX]\S+")
_PY_HEREDOC_TOKEN_RE = re.compile(r"<<-?(?:''|\"\")")
# Output redirections / fd duplication only: an input redirection would replace the heredoc as stdin.
_PY_REDIRECT_RE = re.compile(r"(?:\d*>>?|&>>?|\d*>\|)(?:&(?:\d+|-)|[^\s<>&|;]+)|\d*<&(?:\d+|-)")
_PY_REDIRECT_OPERATOR_RE = re.compile(r"\d*>>?|&>>?|\d*>\|")


def _is_python_stdin_program_owner(masked_owner: str) -> bool:
    """Whether the quote-masked simple command runs Python with its program read from one heredoc."""
    tokens = masked_owner.split()
    index = 0
    while index < len(tokens) and _PY_ASSIGNMENT_RE.fullmatch(tokens[index]):
        index += 1
    if index < len(tokens) and tokens[index] == "env":
        index += 1
        while index < len(tokens):
            token = tokens[index]
            if _PY_ASSIGNMENT_RE.fullmatch(token) or token in ("-i", "--ignore-environment"):
                index += 1
            elif token == "-u" and index + 1 < len(tokens):
                index += 2
            elif token.startswith("--unset="):
                index += 1
            else:
                break
    if index >= len(tokens) or not _PY_INTERPRETER_RE.fullmatch(tokens[index]):
        return False
    index += 1
    heredocs = 0
    program_from_stdin = False  # after `-` every word is sys.argv for the stdin program
    while index < len(tokens):
        token = tokens[index]
        if _PY_HEREDOC_TOKEN_RE.fullmatch(token):
            heredocs += 1
            index += 1
        elif token in ("<<", "<<-") and index + 1 < len(tokens) and tokens[index + 1] in ("''", '""'):
            heredocs += 1
            index += 2
        elif _PY_REDIRECT_RE.fullmatch(token):
            index += 1
        elif (_PY_REDIRECT_OPERATOR_RE.fullmatch(token) and index + 1 < len(tokens)
              and not tokens[index + 1].startswith(("<", ">", "&"))):
            index += 2
        elif "<" in token or ">" in token:
            return False
        elif program_from_stdin:
            index += 1
        elif token == "-":
            program_from_stdin = True
            index += 1
        elif _PY_FLAG_RE.fullmatch(token):
            index += 1
        elif token in ("-W", "-X") and index + 1 < len(tokens):
            index += 2
        else:
            return False  # a script, -c, -m or unknown option: the heredoc is that program's data
    return heredocs == 1


def split_python_heredoc_bodies(command: str) -> tuple[str, list[str], list[str]]:
    """``(shell_source, program_bodies, other_bodies)``: PROGRAM bodies are removed from the shell
    source (newlines kept); OTHER Python-owned bodies stay in it. Neither kind is declared safe."""
    if "<<" not in command:
        return command, [], []
    last_opener_index = command.rfind("<<")
    program_ranges: list[tuple[int, int]] = []
    program_bodies: list[str] = []
    other_bodies: list[str] = []
    command_start = 0
    while command_start <= last_opener_index:
        spec_owners: list[int] = []
        list_operators: list[int] = []
        command_end, specs, unknown_operator, _post_list, _owner = _scan_heredoc_command_unit(
            command, command_start, spec_owners=spec_owners, list_operators=list_operators)
        if unknown_operator:
            break  # the masker leaves such a command fully visible; classify nothing further
        if not specs:
            if command_end >= len(command):
                break
            command_start = command_end + 1
            continue
        if command_end >= len(command):
            break  # unterminated: visible to every scanner
        masked_unit = _mask_simple_quotes(command[command_start:command_end])
        grouped = any(marker in masked_unit for marker in ("$(", "`", "<(", ">(", "(", ")", "{", "}"))
        body_cursor = command_end + 1
        unit_ranges: list[tuple[int, int, int, bool]] = []
        for (delimiter, strip_tabs, quoted), owner_start in zip(specs, spec_owners):
            close_end = _find_heredoc_close(command, body_cursor, delimiter, strip_tabs)
            if close_end is None:
                unit_ranges = []
                body_cursor = None
                break
            unit_ranges.append((body_cursor, close_end, owner_start, quoted))
            body_cursor = close_end
        if body_cursor is None:
            break
        for start, end, owner_start, quoted in unit_ranges:
            owner_end = next((p for p in list_operators if p >= owner_start), command_end)
            masked_owner = _mask_simple_quotes(command[owner_start:owner_end])
            if not _PY_OWNER_TOKEN_RE.search(masked_owner):
                continue
            body = "".join(command[start:end].splitlines(keepends=True)[:-1])
            if quoted and not grouped and _is_python_stdin_program_owner(masked_owner):
                program_ranges.append((start, end))
                program_bodies.append(body)
            else:
                other_bodies.append(body)
        command_start = body_cursor
    parts: list[str] = []
    previous = 0
    for start, end in program_ranges:
        parts += [command[previous:start], "\n" * command.count("\n", start, end)]
        previous = end
    return "".join(parts) + command[previous:], program_bodies, other_bodies
