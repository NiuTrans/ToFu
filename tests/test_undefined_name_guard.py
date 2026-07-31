"""Ratchet: no function may reference a name its signature never defines.

**The incident this exists to prevent (2026-07-27).**
``lib/database/messages_rows.py::mirror_write_and_commit`` was landed with a
body that branched on ``full``::

    def mirror_write_and_commit(db, conv_id, messages, *, now_ms=0,
                                changed_seqs=None):     # <- no `full`
        ...
        if full:                                        # <- NameError
            backfill_conv(...)

The docstring documented ``full=True`` and 8 call sites passed it, but the
parameter was dropped from the signature. Nothing caught it: the module
imports fine, the function only explodes when *called*, and its two callers
(``dual_write_conv``'s wrapper and the blob writers) swallow exceptions
because mirroring is best-effort. The result was 112 silent failures across
4 subsystems (translate commit, conversation sync, swarm snapshot, autopilot
baton) over ~90 minutes before anyone noticed autopilot had stopped looping.

**Why a targeted test was not enough.** The first guard written for this bug
only checked ``mirror_write_and_commit``. But the *class* of defect is
"function body references an undefined local name", which any refactor can
reintroduce anywhere. This module is the general ratchet: it AST-walks every
tracked ``lib/`` and ``routes/`` module and fails on any name a function
reads that is not bound by its parameters, its own assignments, an enclosing
scope, module globals, or builtins.

**Design notes**
* ``git ls-files`` enumerates sources — ``os.walk`` times out on this FUSE
  mount (the same lesson recorded for ``test_error_transparency_guard.py``).
* Purely static: no imports of the scanned modules, so scanning is safe and
  fast and cannot be defeated by import side effects.
* Conservative by construction. A name is only reported when it resolves in
  NO scope at all. Anything dynamic (``global``/``nonlocal``, comprehension
  and exception-handler bindings, walrus, star-imports, decorators that
  inject names) is tracked or bails out, so a false positive requires a
  genuinely unresolvable name.
"""

import ast
import builtins
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit

_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

_BUILTINS = frozenset(dir(builtins)) | {'__file__', '__name__', '__doc__',
                                        '__spec__', '__package__', '__loader__',
                                        '__debug__', '__builtins__'}


def _tracked_sources():
    """Every tracked .py under lib/ and routes/ (git ls-files, not os.walk)."""
    out = subprocess.run(
        ['git', 'ls-files', 'lib/*.py', 'lib/**/*.py', 'routes/*.py', 'routes/**/*.py'],
        cwd=_ROOT, capture_output=True, text=True, check=True)
    return sorted({p for p in out.stdout.split('\n') if p.endswith('.py')})


def _module_level_names(tree):
    """Names bound at module scope: imports, assignments, defs, classes."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == '*':
                    # Star-import can inject anything — treat the module as
                    # unanalyzable rather than emit false positives.
                    return None
                names.add(alias.asname or alias.name.split('.')[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
    return names


def _bound_in_function(fn):
    """Every name bound anywhere inside ``fn`` (params + local bindings).

    Deliberately over-approximates: a name assigned on any branch, in a
    comprehension, as an except-handler target, an import inside the body, a
    nested def/class, or declared global/nonlocal all count as bound. That
    keeps the check conservative — we only flag names bound NOWHERE.
    """
    bound = set()

    args = fn.args
    for a in (list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)):
        bound.add(a.arg)
    if args.vararg:
        bound.add(args.vararg.arg)
    if args.kwarg:
        bound.add(args.kwarg.arg)

    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == '*':
                    return None
                bound.add(alias.asname or alias.name.split('.')[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)          # nested lambdas / inner defs
        elif isinstance(node, ast.alias):
            bound.add(node.asname or node.name.split('.')[0])
    return bound


def _undefined_names(path, source):
    """Return [(function_name, lineno, undefined_name), ...] for one module."""
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError:
        return []                                  # not our concern here

    module_names = _module_level_names(tree)
    if module_names is None:
        return []                                  # star-import: unanalyzable

    findings = []

    # Walk functions with their enclosing-function chain so closures resolve.
    def walk(node, enclosing):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                bound = _bound_in_function(child)
                if bound is None:
                    continue
                scope = bound | enclosing
                loaded = {
                    (n.id, n.lineno)
                    for n in ast.walk(child)
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                }
                for name, lineno in sorted(loaded, key=lambda t: t[1]):
                    if (name not in scope
                            and name not in module_names
                            and name not in _BUILTINS):
                        findings.append((child.name, lineno, name))
                walk(child, scope)
            elif isinstance(child, ast.ClassDef):
                # Class body names are not visible to nested methods, so the
                # enclosing scope does not grow.
                walk(child, enclosing)
            else:
                walk(child, enclosing)

    walk(tree, set())
    return findings


def test_no_function_references_an_undefined_name():
    """The ratchet: zero unresolvable names across lib/ and routes/.

    A failure here is almost always a signature/body drift — a parameter
    renamed or dropped while the body still reads it. That is a guaranteed
    runtime NameError on the first call, and in swallow-exceptions code paths
    it will be SILENT.
    """
    violations = []
    for rel in _tracked_sources():
        abs_path = os.path.join(_ROOT, rel)
        try:
            with open(abs_path, encoding='utf-8') as f:
                source = f.read()
        except OSError:
            continue
        for fn_name, lineno, name in _undefined_names(rel, source):
            violations.append(f'{rel}:{lineno} {fn_name}() references undefined name {name!r}')

    assert not violations, (
        'Function bodies reference names bound in no scope — each is a runtime '
        'NameError waiting to fire (and silent wherever the caller swallows '
        'exceptions):\n  ' + '\n  '.join(violations)
    )


def test_ratchet_detects_the_original_defect():
    """NEUTER: the exact pre-fix ``mirror_write_and_commit`` must be caught."""
    broken = '''
def mirror_write_and_commit(db, conv_id, messages, *, now_ms=0, changed_seqs=None):
    if not rows_write_enabled():
        return
    if full:
        backfill_conv(db, conv_id, messages, now_ms=now_ms, commit=False)
'''
    findings = _undefined_names('synthetic.py', broken)
    names = {n for _fn, _ln, n in findings}
    assert 'full' in names, (
        'the ratchet failed to detect the original defect — it would not have '
        'prevented the 2026-07-27 incident'
    )


def test_ratchet_does_not_flag_legitimate_scopes():
    """Closures, globals, comprehensions and except-targets are NOT violations."""
    ok = '''
import os

CONST = 1

def outer(a, *args, **kwargs):
    captured = a + CONST

    def inner():
        return captured + a + CONST + os.sep

    total = sum(x for x in range(3))
    try:
        pass
    except ValueError as e:
        total = len(str(e))
    if (walrus := total) > 0:
        total = walrus
    return inner() + total + len(args) + len(kwargs)

def uses_global():
    global _lazy
    _lazy = 5
    return _lazy
'''
    assert _undefined_names('ok.py', ok) == []
