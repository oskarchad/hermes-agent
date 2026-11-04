"""Positive AST verification for the approval guard's read-only Python carveout.

This classifies source only: it never executes code, resolves files, or changes policy.
"""

import ast

_SAFE_PYTHON_DATA_MODULES = frozenset({"sys", "json", "pathlib", "ast", "re"})
_SAFE_MODULE_ATTR_LOOKUPS = {
    "sys": frozenset({"stdin", "stdout", "stderr", "argv"}),
    "json": frozenset({"load", "loads"}),
    "pathlib": frozenset({"Path"}),
    "ast": frozenset({"literal_eval"}),
    "re": frozenset({"search", "match", "fullmatch", "split", "findall", "finditer", "sub", "subn"}),
}
_SAFE_RECEIVER_METHODS = {
    "sys.stdin": frozenset({"read", "readline", "readlines"}),
    "pathlib.Path": frozenset({"read_text", "read_bytes"}),
    "json": frozenset({"load", "loads"}),
    "ast": frozenset({"literal_eval"}),
    "re": frozenset({"search", "match", "fullmatch", "split", "findall", "finditer", "sub", "subn"}),
}
_SAFE_BUILTIN_CALLS = frozenset({
    "print", "len", "str", "int", "float", "bool", "bytes", "list", "dict", "set", "tuple",
    "min", "max", "sum", "sorted", "reversed", "enumerate", "zip", "range",
})
_DANGEROUS_CALL_NAMES = frozenset({
    "exec", "eval", "compile", "__import__",
    "system", "popen", "spawn", "fork", "call", "check_call", "check_output",
    "getattr", "setattr", "delattr", "open",
})
_DANGEROUS_MODULE_ROOTS = frozenset({
    "os", "subprocess", "posix", "nt", "pty", "socket", "http", "urllib",
    "requests", "shutil", "builtins", "__builtin__", "importlib", "ctypes",
    "signal", "multiprocessing", "threading",
})
_MUTATION_OR_WRITE_CALLS = frozenset({
    "write", "writelines", "write_text", "write_bytes", "unlink", "remove",
    "rmdir", "mkdir", "rename", "replace", "chmod", "chown", "touch",
})


_SAFE_FORMAT_METHODS = frozenset({
    "strip", "split", "replace", "lower", "upper", "startswith", "endswith", "format", "join",
})
_SAFE_DICT_METHODS = frozenset({"get", "keys", "values", "items"})
_READ_METHODS = frozenset({"read", "readline", "readlines", "read_text", "read_bytes"})
_FORBIDDEN_NODES = (
    ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda,
    ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.Match,
    ast.NamedExpr, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp,
)


def _valid_assignment(stmt: ast.Assign, bindings: dict[str, str]) -> bool:
    if len(stmt.targets) != 1 or not isinstance(stmt.targets[0], ast.Name):
        return False
    if stmt.targets[0].id in bindings or stmt.targets[0].id in _SAFE_BUILTIN_CALLS:
        return False
    # Direct aliases to modules/callables do not prove a receiver type.
    return not isinstance(stmt.value, (ast.Name, ast.Attribute))


def _import_bindings(tree: ast.Module) -> dict[str, str] | None:
    bindings: dict[str, str] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                root = alias.name.split(".")[0]
                if root not in _SAFE_PYTHON_DATA_MODULES or root in _DANGEROUS_MODULE_ROOTS:
                    return None
                bindings[alias.asname or alias.name] = alias.name
        elif isinstance(stmt, ast.ImportFrom):
            if not stmt.module:
                return None
            root = stmt.module.split(".")[0]
            if root not in _SAFE_PYTHON_DATA_MODULES or root in _DANGEROUS_MODULE_ROOTS:
                return None
            allowed = _SAFE_MODULE_ATTR_LOOKUPS.get(stmt.module, frozenset())
            for alias in stmt.names:
                if alias.name == "*" or alias.name not in allowed:
                    return None
                bindings[alias.asname or alias.name] = f"{stmt.module}.{alias.name}"
        elif isinstance(stmt, ast.Assign):
            if not _valid_assignment(stmt, bindings):
                return None
        elif not isinstance(stmt, ast.Expr):
            return None
    return bindings


def _named_call(name: str, bindings: dict[str, str]) -> bool | None:
    if name in _SAFE_BUILTIN_CALLS:
        return False
    bound = bindings.get(name)
    if bound and (bound in {"json.loads", "json.load", "ast.literal_eval", "pathlib.Path"}
                  or bound.startswith("re.")):
        return True
    return None


def _module_method(module: str, attr: str) -> bool | None:
    if module == "pathlib" and attr == "Path":
        return False
    if ((module == "json" and attr in {"loads", "load"})
            or (module == "ast" and attr == "literal_eval")
            or (module == "re" and attr in _SAFE_RECEIVER_METHODS["re"])):
        return True
    return None


def _receiver_method(receiver: ast.expr, attr: str, bindings: dict[str, str]) -> bool | None:
    if isinstance(receiver, ast.Name):
        if receiver.id in bindings:
            return _module_method(bindings[receiver.id], attr)
        if attr in _SAFE_FORMAT_METHODS or attr in _SAFE_DICT_METHODS:
            return False
        return True if attr in _READ_METHODS else None
    if isinstance(receiver, ast.Attribute):
        if (isinstance(receiver.value, ast.Name)
                and bindings.get(receiver.value.id) == "sys"
                and receiver.attr == "stdin" and attr in {"read", "readline", "readlines"}):
            return True
        return False if attr in _SAFE_FORMAT_METHODS or attr in _SAFE_DICT_METHODS else None
    if isinstance(receiver, ast.Call):
        if attr in _SAFE_FORMAT_METHODS or attr == "splitlines" or attr in _SAFE_DICT_METHODS:
            return False
        return True if attr in {"read_text", "read_bytes"} else None
    return None


def _call_verdict(node: ast.Call, bindings: dict[str, str]) -> bool | None:
    if isinstance(node.func, ast.Name):
        return _named_call(node.func.id, bindings)
    if isinstance(node.func, ast.Attribute):
        attr = node.func.attr
        if attr in _DANGEROUS_CALL_NAMES or attr in _MUTATION_OR_WRITE_CALLS:
            return None
        return _receiver_method(node.func.value, attr, bindings)
    return None


def is_safe_python_data_code(code_str: str) -> bool:
    """Require allowed imports, simple statements, known calls and a recognized read/parse."""
    try:
        tree = ast.parse(code_str)
    except (SyntaxError, ValueError, TypeError, MemoryError, RecursionError):
        return False
    if not tree.body:
        return False
    bindings = _import_bindings(tree)
    if bindings is None:
        return False
    has_read_or_parse = False
    for node in ast.walk(tree):
        if isinstance(node, _FORBIDDEN_NODES):
            return False
        if isinstance(node, ast.Attribute) and node.attr.startswith("__") and node.attr.endswith("__"):
            return False
        if isinstance(node, ast.Call):
            verdict = _call_verdict(node, bindings)
            if verdict is None:
                return False
            has_read_or_parse |= verdict
    return has_read_or_parse
