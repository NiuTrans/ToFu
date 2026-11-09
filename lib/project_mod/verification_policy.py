"""Recognize unchanged extraction of the repository's pytest execution owner.

Entry point: same_extracted_execution. This is a conservative, source-only
comparison for the legacy-selector to standalone-owner transition. Unknown
globals, dynamic binding or extra standalone statements retain full invalidation.
Dependencies: Python ast only. Contract: docs/modules/project_integration.md.
"""
from __future__ import annotations

import ast

_FUNCTIONS = frozenset({'_parallel_workers', 'build_pytest_command'})
_BUILTINS = frozenset({'len', 'ValueError', 'list', 'str'})
_PROTECTED = _FUNCTIONS | _BUILTINS | {'sys'}
_DYNAMIC = frozenset({'eval', 'exec', 'globals', 'locals', 'vars', 'getattr', 'setattr', '__import__'})
_LEGACY_IMPORTS = {'argparse', 'ast', 'json', 'os', 're', 'subprocess', 'sys', 'tempfile'}
_LEGACY_CALLS = {'os.path.normpath', 'os.path.join', 'os.path.dirname', 'os.path.abspath', 're.compile'}


def _legacy_declaration(node):
    """Only the old selector's inert import-time declarations are recognized."""
    if isinstance(node, ast.Import):
        return all(alias.name in _LEGACY_IMPORTS and alias.asname is None for alias in node.names)
    if isinstance(node, ast.FunctionDef):
        defaults = [*node.args.defaults, *node.args.kw_defaults]
        return not node.decorator_list and all(
            value is None or isinstance(value, (ast.Constant, ast.Name)) for value in defaults)
    if isinstance(node, ast.If):
        return ast.dump(node.test) == ast.dump(ast.parse("__name__ == '__main__'", mode='eval').body) and not node.orelse
    if not isinstance(node, ast.Assign) or not all(isinstance(target, ast.Name) for target in node.targets):
        return False
    for expression in ast.walk(node.value):
        if isinstance(expression, ast.Call) and ast.unparse(expression.func) not in _LEGACY_CALLS:
            return False
        if isinstance(expression, (ast.Lambda, ast.NamedExpr, ast.ListComp, ast.SetComp,
                                   ast.DictComp, ast.GeneratorExp, ast.Await, ast.Yield)):
            return False
    return True


def _definitions(tree, *, standalone):
    functions = {}
    imports_sys = False
    for node in tree.body:
        if isinstance(node, ast.Import) and all(alias.name == 'sys' and alias.asname in (None, 'sys')
                                               for alias in node.names):
            imports_sys = True
        elif isinstance(node, ast.ImportFrom) and node.module == '__future__' and all(
                alias.name == 'annotations' for alias in node.names):
            continue
        elif isinstance(node, ast.FunctionDef) and node.name in _FUNCTIONS:
            if node.name in functions:
                return None
            functions[node.name] = node
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        elif standalone or not _legacy_declaration(node):
            return None
    if not imports_sys or functions.keys() != _FUNCTIONS:
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            return None
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)) and node.id in _PROTECTED:
            return None
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _DYNAMIC:
            return None
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
            return None
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)):
            base = node.value
            while isinstance(base, (ast.Attribute, ast.Subscript)):
                base = base.value
            if isinstance(base, ast.Name) and base.id in _PROTECTED:
                return None
        if isinstance(node, ast.alias) and (node.asname or node.name.split('.')[0]) in _PROTECTED:
            if node.name != 'sys' or node.asname not in (None, 'sys'):
                return None
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in _PROTECTED - _FUNCTIONS:
            return None
    for function in functions.values():
        local = {node.id for node in ast.walk(function) if isinstance(node, ast.Name)
                 and isinstance(node.ctx, ast.Store)}
        local.update(node.arg for node in ast.walk(function) if isinstance(node, ast.arg))
        reads = {node.id for node in ast.walk(function) if isinstance(node, ast.Name)
                 and isinstance(node.ctx, ast.Load)}
        if reads - local - _PROTECTED or function.decorator_list:
            return None
    return functions


def same_extracted_execution(legacy_source, standalone_source) -> bool:
    """Recognize the inert legacy shape and unchanged execution definitions."""
    try:
        legacy = _definitions(ast.parse(legacy_source), standalone=False)
        standalone = _definitions(ast.parse(standalone_source), standalone=True)
    except (SyntaxError, ValueError, TypeError):
        return False
    return bool(legacy and standalone and all(
        ast.dump(legacy[name]) == ast.dump(standalone[name]) for name in _FUNCTIONS))
