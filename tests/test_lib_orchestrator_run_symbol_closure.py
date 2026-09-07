"""Static symbol-closure guards for the orchestrator extraction boundary.

``run_task`` delegates to many focused helpers. These AST checks ensure every
name read by the main loop and every keyword forwarded to the post-loop sink is
bound in the caller's scope, catching incomplete extractions before runtime.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

RUN_PY = (
    Path(__file__).resolve().parent.parent
    / 'lib' / 'tasks_pkg' / 'orchestrator' / '_run.py'
)


def _find_run_task(tree: ast.Module) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == 'run_task':
            return node
    raise AssertionError('run_task() function not found in _run.py')


def _module_toplevel_names(tree: ast.Module) -> set[str]:
    """Names bound at module top level (imports, defs, top-level assigns).

    Used so the intra-function scan doesn't false-flag references to imported
    symbols like ``EventType`` / ``build_event`` / ``append_event`` / logger.
    """
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for a in node.names:
                out.add((a.asname or a.name).split('.')[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                out.add(a.asname or a.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                for n in ast.walk(tgt):
                    if isinstance(n, ast.Name):
                        out.add(n.id)
    return out


def _collect_bindings(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Every name that gets bound somewhere inside ``fn``.

    Covers ast.Store contexts (assignments, augmented assignments,
    tuple/starred unpacks), ``for`` targets, ``with ... as x``, ``except ... as x``,
    nested def/class names, and parameters. This is the "what's visible if
    reached at runtime" set — deliberately permissive: the guard only fires
    when a name is read that has NO potential binding anywhere in the scope.
    """
    bound: set[str] = set()
    # Parameters
    args = fn.args
    for arg_list in (args.args, args.posonlyargs, args.kwonlyargs):
        for a in arg_list:
            bound.add(a.arg)
    if args.vararg:
        bound.add(args.vararg.arg)
    if args.kwarg:
        bound.add(args.kwarg.arg)

    def _add_arg_names(a: ast.arguments) -> None:
        for arg_list in (a.args, a.posonlyargs, a.kwonlyargs):
            for arg in arg_list:
                bound.add(arg.arg)
        if a.vararg:
            bound.add(a.vararg.arg)
        if a.kwarg:
            bound.add(a.kwarg.arg)

    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                _add_arg_names(node.args)
        elif isinstance(node, ast.Lambda):
            _add_arg_names(node.args)
        elif isinstance(node, ast.Import):
            for a in node.names:
                bound.add((a.asname or a.name).split('.')[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                bound.add(a.asname or a.name)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            for n in ast.walk(node.target):
                if isinstance(n, ast.Name):
                    bound.add(n.id)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    for n in ast.walk(item.optional_vars):
                        if isinstance(n, ast.Name):
                            bound.add(n.id)
        elif isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name):
            bound.add(node.target.id)
    return bound


def _load_names(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    return {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}


@pytest.mark.unit
def test_run_task_has_no_undefined_local_reads():
    """Every Name read in run_task must be resolvable in-scope or from module globals.

    This catches a helper extraction that removes a local while leaving a
    downstream consumer behind.
    """
    src = RUN_PY.read_text()
    tree = ast.parse(src)
    fn = _find_run_task(tree)

    module_names = _module_toplevel_names(tree)
    bound = _collect_bindings(fn)
    loads = _load_names(fn)

    # Python builtins we know are always resolvable.
    import builtins as _b
    builtin_names = set(dir(_b))

    unresolved = sorted(n for n in loads if n not in bound and n not in module_names and n not in builtin_names)
    assert not unresolved, (
        f'run_task() references {len(unresolved)} name(s) that are not defined '
        f'anywhere in its scope or at module top-level:\n  ' + '\n  '.join(unresolved) +
        '\nA helper extraction likely removed a binding without migrating '
        'every consumer inside run_task.'
    )


@pytest.mark.unit
def test_finalize_after_loop_kwargs_all_bound():
    """Every ``kwarg=<Name>`` in the finalize_after_loop() call must resolve in-scope.

    Narrower reproducer: even if a future refactor moves the whole main-stream
    loop into a helper, this specific call site — the one that failed in
    logs/app.log line 123649 with conv=mryio038zf2qsd — must never regress
    to reading an undefined local again.
    """
    src = RUN_PY.read_text()
    tree = ast.parse(src)
    fn = _find_run_task(tree)

    call = None
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == 'finalize_after_loop'):
            call = node
            break
    assert call is not None, 'finalize_after_loop() call not found in run_task'

    module_names = _module_toplevel_names(tree)
    bound = _collect_bindings(fn)
    import builtins as _b
    builtin_names = set(dir(_b))
    scope = bound | module_names | builtin_names

    unresolved_kwargs: list[str] = []
    for kw in call.keywords:
        if kw.arg is None:  # **kwargs splat
            continue
        # Only check the simple `kwarg=<Name>` case. Complex expressions
        # (attribute chains, calls, literals) are out-of-scope for a static
        # closure check.
        if isinstance(kw.value, ast.Name) and kw.value.id not in scope:
            unresolved_kwargs.append(f'{kw.arg}={kw.value.id}')

    assert not unresolved_kwargs, (
        'finalize_after_loop() is called with kwargs bound to undefined locals:\n  '
        + '\n  '.join(unresolved_kwargs)
        + '\nEvery forwarded local must remain bound after helper extraction.'
    )
