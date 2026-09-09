"""Bounded local antecedents for the containment-reference downgrade.

This is a recognizer, not English coreference: every token must belong to the
supported grammar. Nouns are open-class; grammatical operators are closed.
Unknown productions, extra referents and unmatched tails fail closed.
"""
import ast
import re


# Closed-class words cannot masquerade as part of an open-class nominal.
_RESERVED = set('''a an the another some any all each every no not never must
can could should would may might will shall do does did is are was were be been
being have has had it its itself this that these those their them they he she
his her hers himself herself ourselves yourselves themselves we us our ours you your yours
one ones former latter other others such something anything everything nothing
which who whom whose where when how what why
above below earlier later previous following same said aforementioned
and or but if then than as after before while because so to from of in on at
for with without using containing including having reads checks shows parses
returns crashes fails ends up left happen use uses'''.split())
_WORD = re.compile(r'[A-Za-z][A-Za-z0-9_-]*\Z')


def _nominal(text: str, *, indefinite: bool = False) -> bool:
    words = text.split()
    if words and words[0].lower() in {'a', 'an'}:
        words = words[1:]
    elif indefinite:
        return False
    return bool(words) and all(_WORD.fullmatch(w) and w.lower() not in _RESERVED for w in words)


def _product(text: str, depth: int = 0) -> bool:
    """NP [in proper-name] [with NP] [that VP], bounded recursive composition."""
    if depth > 4:
        return False
    nominal, relative, predicate = text.partition(' that ')
    nominal, nested, component = nominal.partition(' with ')
    nominal, language, name = nominal.partition(' in ')
    # A conversion compound has two explicit nominals, not a deictic target.
    first, conversion, second = nominal.partition(' to ')
    if not _nominal(first, indefinite=True):
        return False
    if conversion and not _nominal(second):
        return False
    if language and not (re.fullmatch(r'[A-Z][A-Za-z0-9_+-]*', name)):
        return False
    if nested and not _product(component, depth + 1):
        return False
    return not relative or _product_predicate(predicate)


def _product_predicate(text: str) -> bool:
    # Relative subject is the newly constructed product. Its input object is
    # introduced by the first transitive predicate, not by the nearest noun.
    first, conjunction, second = text.partition(' and ')
    read = re.fullmatch(r'(?:reads|parses) (.+)', first)
    if read:
        if not _nominal(read[1], indefinite=True):
            return False
        if not conjunction:
            return True
        # "it" is bound only in this coordinated predicate's input slot.
        output = re.fullmatch(r'(?:shows|reports) (.+) for it', second)
        return bool(output and _nominal(output[1]))
    check = re.fullmatch(r'checks how ([A-Za-z-]+) (.+) is', text)
    return bool(check and _nominal(check[1]) and _nominal(check[2], indefinite=True))


def constructed_product(sentence: str, continuation: str) -> bool:
    """Resolve only writing the sole newly constructed product to a named output."""
    construction = re.fullmatch(r'(?:Build|Make|Write|Create)(?: me)? (.+)', sentence, re.IGNORECASE)
    if not construction or not re.fullmatch(
            r'\s+to [A-Za-z_][\w./-]*(?:\.(?=\s|$)|$)', continuation):
        return False
    return _product(construction[1])


# Verb valencies of the bounded report grammar. These are grammatical terminals,
# not approved products, filenames or bug descriptions.
_INTRANSITIVE = {'crash', 'crashes', 'fail', 'fails', 'happen', 'happens'}
_TRANSITIVE = {'use', 'uses', 'return', 'returns'}
_REPORT_OPERATORS = _RESERVED | _INTRANSITIVE | _TRANSITIVE | {
    'end', 'ends', 'move', 'moves', 'go', 'goes', 'like', 'into'}
_TOKEN = re.compile(r"\s*(?:('[\$+\-\d,]+(?:\.\d+)?')|([A-Za-z][\w-]*)|([^\s]))")


class _Report:
    """Consume every report token; references have explicit grammatical slots."""
    def __init__(self, text: str, subject: bool):
        self.tokens = [next(p for p in m.groups() if p).lower() for m in _TOKEN.finditer(text)]
        self.pos = 0
        self.subject = subject
        self.heads: list[str] = []

    def take(self, *words: str) -> bool:
        if self.tokens[self.pos:self.pos + len(words)] != list(words):
            return False
        self.pos += len(words)
        return True

    def noun(self, *, subject: bool = False, references: bool = False) -> bool:
        if subject and self.subject and (self.take('it') or self.take('this')):
            return True
        referential = references and self.take('those')
        if not referential:
            for determiner in ('a', 'an', 'some', 'no'):
                if self.take(determiner):
                    break
        start = self.pos
        while self.pos < len(self.tokens):
            word = self.tokens[self.pos]
            if not _WORD.fullmatch(word) or word in _REPORT_OPERATORS:
                break
            self.pos += 1
        if self.pos == start:
            return False
        head = self.tokens[self.pos - 1]
        if referential:
            return self.heads.count(head) == 1
        self.heads.append(head)
        return True

    def resultative(self) -> bool:
        return self.take('up') and self.take('with') and self.noun()

    def copular(self) -> bool:
        if self.take('left'):
            return self.take('with') and self.noun()
        return self.noun()

    def predicate(self, depth: int) -> bool:
        if depth > 4 or self.pos == len(self.tokens):
            return False
        modal = self.take('must') or self.take('can')
        if modal:
            self.take('never') or self.take('not')
        verb = self.tokens[self.pos] if self.pos < len(self.tokens) else ''
        self.pos += 1
        if modal and verb not in {'crash', 'fail', 'happen', 'use', 'return', 'end', 'move', 'go'}:
            return False
        actions = {
            **dict.fromkeys(_TRANSITIVE | {'move', 'moves', 'go', 'goes'}, self.noun),
            **dict.fromkeys(_INTRANSITIVE, lambda: True),
            **dict.fromkeys({'end', 'ends'}, self.resultative),
            **dict.fromkeys({'is', 'are'}, self.copular),
        }
        action = actions.get(verb)
        if action is None or not action():
            return False
        if self.take('on') or self.take('with'):
            if not self.noun():
                return False
            if self.take('where') and not self.clause(depth + 1):
                return False
        if self.take(',', 'like'):
            if self.pos == len(self.tokens) or not re.fullmatch(
                    r"'[\$+\-\d,]+(?:\.\d+)?'", self.tokens[self.pos]):
                return False
            self.pos += 1
        # A sentential relative binds the completed preceding proposition.
        if self.take(',', 'which') and not self.predicate(depth + 1):
            return False
        return True

    def clause(self, depth: int = 0, *, references: bool = False) -> bool:
        if depth > 4:
            return False
        if self.take('after') and not self.noun():
            return False
        if not self.noun(subject=depth == 0, references=references) or not self.predicate(depth):
            return False
        if self.take('and'):
            return self.clause(depth + 1, references=references)
        return True

    def complete(self) -> bool:
        return self.clause() and self.pos == len(self.tokens)


def _docstring_at(source: str, offset: int) -> tuple[str, int] | None:
    """Bind a function docstring to its declared function, not another source unit.

    Return exact docstring content before/after the instruction using the source
    span. No import/eval or assumption about program dataflow is involved.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return None
    lines = source.splitlines(keepends=True)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not node.body:
            continue
        first = node.body[0]
        if not isinstance(first, ast.Expr) or not isinstance(first.value, ast.Constant):
            continue
        value = first.value
        if not isinstance(value.value, str):
            continue
        # ast.parse supplies end positions for every parsed literal.
        assert value.end_lineno is not None and value.end_col_offset is not None
        # AST columns are UTF-8 bytes; the scanner offset counts Unicode chars.
        start = sum(map(len, lines[:value.lineno - 1])) + len(
            lines[value.lineno - 1].encode()[:value.col_offset].decode())
        end = sum(map(len, lines[:value.end_lineno - 1])) + len(
            lines[value.end_lineno - 1].encode()[:value.end_col_offset].decode())
        if start <= offset < end:
            segment = source[start:end]
            quoted = re.fullmatch(r'(?:[ruRU]*)("""|\'\'\')(.*)\1', segment, re.DOTALL)
            if quoted:
                body_start = start + quoted.start(2)
                return quoted[2], offset - body_start
    return None


def complete_report(context: str, offset: int, end: int, source: str,
                    source_offset: int | None = None) -> bool:
    """Fix the complete reported defect, only after all internal bindings pass."""
    docstring = _docstring_at(source, offset if source_offset is None else source_offset)
    subject = docstring is not None
    if docstring:
        context, offset = docstring
        # The matched instruction is punctuation + optional whitespace + Fix it.
        instruction = re.match(r'[.!?]\s*Fix it\b', context[offset:], re.IGNORECASE)
        if not instruction:
            return False
        end = offset + instruction.end()
    before = re.split(r'\n\s*\n', context[:offset])[-1]
    reports = list(re.finditer(r'\bbug report:\s*', before, re.IGNORECASE))
    if len(reports) != 1:
        return False
    report = reports[0]
    prefix = ' '.join(before[:report.start()].split())
    if not subject and prefix:
        subject = re.fullmatch(
            r'[A-Za-z_]\w*\([\w, ]*\) in [\w./-]+ has a', prefix) is not None
        if not subject:
            return False
    parser = _Report(before[report.end():], subject)
    if not parser.complete():
        return False
    # A repair purpose clause is part of the request, not ignored suffix text.
    tail = context[end:]
    tail = re.split(r'(?<=[.!?])\s', tail, maxsplit=1)[0].strip()
    if tail in {'', '.'}:
        return True
    if not tail.lower().startswith('so '):
        return False
    continuation = _Report(tail[3:].removesuffix('.'), False)
    continuation.heads = parser.heads.copy()
    return continuation.clause(references=True) and continuation.pos == len(continuation.tokens)
