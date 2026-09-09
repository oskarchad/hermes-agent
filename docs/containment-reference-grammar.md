# Local references in containment requirements

The shared [scanner](../tools/skills_guard.py) can retain a system-password-path
containment requirement as a HIGH review finding rather than CRITICAL access.
The comment/table-cell grammar and full-document explicit-reference veto still
apply. This does not make the document safe or waive other findings.

[The local recognizer](../tools/skills_guard_references.py) supplies positive
evidence for two additional instruction forms. It is a bounded lexical grammar,
not a general English parser, a Python dataflow analysis, or a sandbox. Unknown
productions fail closed. Nominals are open-class; there is no list of permitted
product names, report subjects, repository paths, or source hashes.

## Construct then write

A single imperative construction introduces a product. The entire product must
parse, including any component, language qualifier, and relative predicate.
The immediately following `Write it to <named-output>` refers to that product.
Blank paragraphs, competing products and other operations do not use this rule.

A supported relative predicate can read/parse an explicitly introduced singular
input and then show/report a nominal result **for that input**. Only `for it` in
that coordinated predicate binds to the explicit input. The outer `Write it`
binds to the construction result, not the nearest noun. A relative `reads it`
without an explicit input does not acquire a binding from the outer product.
All relative-clause and instruction suffix tokens must be consumed.

## Report then fix

`Fix it` can refer to a completely parsed local bug report, not an arbitrary
prefix followed by uninspected text. The report grammar composes subject,
verb-valency, prepositional, temporal, relative and coordinated clauses; the
implementation defines the supported terminals. A purpose clause after `Fix it`
is also consumed. Unsupported predicates or trailing material retain CRITICAL.

An initial report-subject `it`/`this` needs an explicitly named function in the
report introduction or the owning function of an actual Python docstring.
The latter is a documentation-convention binding: the function's own docstring
describes that function. It is not inferred from a neighboring function or
from the existence of separate AST sections. A returned instruction string is
not a docstring. Python is parsed only as data, never imported or executed.
Original source positions are retained through markup normalization; UTF-8 AST
columns are converted to character offsets.

Nested clauses do not inherit that subject-pronoun permission. A sentential
`which` is bound to the completed preceding proposition. In a repair purpose,
`those <nominal>` requires exactly one matching head introduced by the report.
Objects do not receive an automatic nearest-noun binding. Unresolved `it`,
`containing it`, `that reads it`, spatial file references and remote linked
consumers therefore keep the critical finding and project quarantine.

## Boundary and verification

These are syntactic bindings within the supported forms, not proof of semantic
independence for arbitrary prose or programs. Review must judge the suitability
of the function-docstring convention and the finite grammar; a passing scan or
mechanical architecture receipt is not that approval.

No trust/force/quarantine policy changes accompany this recognizer. A CAUTION
plugin still requires native confirmation; DANGEROUS is still non-overridable.
Both scanner cache identities change with shared verdict behavior.

Executable contracts: [complete-reference tests](../tests/tools/test_containment_references.py)
and [existing scanner/policy controls](../tests/tools/test_skills_guard.py).
