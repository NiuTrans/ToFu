"""tests/test_async_delegation.py — no coroutine may be handed to asyncio.to_thread.

``asyncio.to_thread(fn)`` calls ``fn()`` in a worker thread. If ``fn`` is an
``async def``, calling it merely builds a coroutine object and never awaits it,
so the endpoint returns an un-awaited coroutine instead of a response. The
handler looks correct and fails only at runtime.

Six endpoints in trading_autopilot.py shipped this way (state / toggle / run /
stream / cycles / cycles/<id>), all delegating to ``async def`` handlers in
trading_brain.py. This test is the static guard against a regression.

It is deliberately AST-based rather than a grep: ``to_thread(_run)`` where
``_run`` is a *sync* closure is legitimate and must keep passing.
"""

import ast
import os

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_HANDLERS = os.path.join(_HERE, '..', 'tofu_trading', 'web', 'handlers')


def _async_defs(tree):
    """Names of every ``async def`` at any nesting depth."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            out.add(node.name)
    return out


def _module_trees():
    for fname in sorted(os.listdir(_HANDLERS)):
        if fname.endswith('.py'):
            path = os.path.join(_HANDLERS, fname)
            with open(path, encoding='utf-8') as fh:
                yield fname, ast.parse(fh.read()), path


def _local_import_sources(tree):
    """Map imported name -> module it came from, for ``from .x import y``."""
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                out[alias.asname or alias.name] = node.module
    return out


@pytest.mark.unit
def test_no_async_function_passed_to_to_thread():
    """The core guard: every to_thread target must be a sync callable."""
    # Build a project-wide index of async function names per handler module.
    async_by_module = {}
    trees = {}
    for fname, tree, _ in _module_trees():
        mod = fname[:-3]
        async_by_module[mod] = _async_defs(tree)
        trees[mod] = tree

    violations = []
    for mod, tree in trees.items():
        imports = _local_import_sources(tree)
        local_async = async_by_module[mod]

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_to_thread = (
                isinstance(func, ast.Attribute) and func.attr == 'to_thread'
            )
            if not is_to_thread or not node.args:
                continue

            target = node.args[0]
            if not isinstance(target, ast.Name):
                continue
            name = target.id

            # Same-module async def?
            if name in local_async:
                violations.append(
                    f'{mod}.py:{node.lineno} to_thread({name}) — {name} is '
                    f'async def in this module')
                continue

            # Imported from a sibling handler that defines it as async def?
            src = imports.get(name, '')
            sibling = src.lstrip('.').split('.')[-1]
            if sibling in async_by_module and name in async_by_module[sibling]:
                violations.append(
                    f'{mod}.py:{node.lineno} to_thread({name}) — {name} is '
                    f'async def in {sibling}.py')

    assert not violations, (
        'asyncio.to_thread() called with async function(s); the coroutine is '
        'never awaited and the endpoint returns a coroutine object:\n  '
        + '\n  '.join(violations))


@pytest.mark.unit
def test_guard_detects_a_planted_violation(tmp_path):
    """NEUTER: prove the guard bites rather than passing vacuously."""
    src = (
        'import asyncio\n'
        'async def handler():\n'
        '    return 1\n'
        'async def route():\n'
        '    return await asyncio.to_thread(handler)\n'
    )
    tree = ast.parse(src)
    async_names = _async_defs(tree)
    assert 'handler' in async_names

    found = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == 'to_thread'
        and n.args
        and isinstance(n.args[0], ast.Name)
        and n.args[0].id in async_names
    ]
    assert found, 'guard logic failed to flag a planted async to_thread target'


@pytest.mark.unit
def test_sync_closure_to_thread_is_allowed():
    """A sync ``_run`` closure in to_thread is correct and must NOT be flagged."""
    src = (
        'import asyncio\n'
        'async def route():\n'
        '    def _run():\n'
        '        return 1\n'
        '    return await asyncio.to_thread(_run)\n'
    )
    tree = ast.parse(src)
    async_names = _async_defs(tree)
    flagged = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == 'to_thread'
        and n.args
        and isinstance(n.args[0], ast.Name)
        and n.args[0].id in async_names
    ]
    assert not flagged, 'sync closure wrongly flagged as async'
