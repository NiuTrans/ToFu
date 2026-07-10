"""Guard: the blocking ``conv_message_builder`` DB read never runs on the loop.

Context
-------
``lib.tasks_pkg.conv_message_builder.build_api_messages_from_db`` (and its
branch sibling ``build_branch_api_messages``) do a SYNCHRONOUS
``db.execute(SELECT messages …)`` of the full conversation blob, using a
thread-local DB connection.  On FUSE / cross-DC PostgreSQL this read can take
hundreds of ms.  That is fine when the call runs in a worker thread (every
sync ``def`` route handler is dispatched to Hypercorn's thread pool by the
Flask→Quart shim, and the autopilot / message-queue callers run in background
threads) — but it MUST NOT be called directly from inside an ``async def``
coroutine, where it would block the single event loop and stall every other
in-flight request.

Today there is exactly ONE ``async def`` caller —
``routes/conversations.py::debug_messages`` — and it correctly off-loads the
blocking DB reconstruction (``_load_messages_from_db`` → ``build_wire_messages``)
by wrapping it in a local ``_build`` closure that is run via
``await asyncio.to_thread(_build)``, so the event loop never blocks.

This suite makes that an enforced, CI-checked invariant so a future ``async
def`` route can't silently reintroduce the event-loop block.  It is a
static-AST check (no app build, no DB) in the same spirit as the source-guard
tests in ``tests/test_compaction_invariants.py``.

Run:  pytest tests/test_conv_message_builder_async_safety.py -v
"""
from __future__ import annotations

import ast
import os
import sys

import pytest


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROUTES_DIR = os.path.join(_ROOT, 'routes')

# The blocking builder entry points that must never be awaited-on-the-loop.
_BLOCKING_BUILDERS = {
    'build_api_messages_from_db',
    'build_branch_api_messages',
}

# The blocking DB-read primitives whose synchronous ``db.execute(SELECT …)`` is
# what actually stalls the loop. ``debug_messages`` off-loads these by wrapping
# them in a local closure run via ``asyncio.to_thread`` — so they must never be
# invoked directly on the coroutine's own frame.
_BLOCKING_DB_READS = _BLOCKING_BUILDERS | {'_load_messages_from_db'}


def _iter_route_py_files():
    """Yield (relpath, abspath) for every .py under routes/ (recursively)."""
    for dirpath, _dirs, files in os.walk(_ROUTES_DIR):
        for fn in files:
            if fn.endswith('.py'):
                ap = os.path.join(dirpath, fn)
                yield os.path.relpath(ap, _ROOT), ap


def _parse(abspath):
    with open(abspath, 'r', encoding='utf-8') as fh:
        return ast.parse(fh.read(), filename=abspath)


def _async_func_nodes(tree):
    """All AsyncFunctionDef nodes anywhere in the module (incl. nested)."""
    return [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]


def _called_name(call: ast.Call) -> str | None:
    """Return the bare callee name for ``foo(...)`` / ``mod.foo(...)``."""
    fn = call.func
    if isinstance(fn, ast.Name):
        return fn.id
    if isinstance(fn, ast.Attribute):
        return fn.attr
    return None


def _is_to_thread_call(call: ast.Call) -> bool:
    """True for ``asyncio.to_thread(...)`` / ``to_thread(...)`` /
    ``run_in_executor(...)`` — the approved off-loop wrappers."""
    name = _called_name(call)
    return name in ('to_thread', 'run_in_executor')


def _raw_blocking_calls_in_async(func: ast.AsyncFunctionDef) -> list[tuple[str, int]]:
    """Find direct (non-off-loaded) calls to a blocking builder inside an
    async function body.

    A call is considered SAFE when the builder name appears as a *reference*
    (``ast.Name``) passed to ``to_thread`` / ``run_in_executor`` rather than
    being *invoked* directly.  We therefore flag only ``ast.Call`` sites whose
    callee resolves to a blocking-builder name.
    """
    offenders: list[tuple[str, int]] = []
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            name = _called_name(node)
            if name in _BLOCKING_BUILDERS:
                offenders.append((name, node.lineno))
    return offenders


def _nested_call_node_ids(func: ast.AST) -> set[int]:
    """ids of every ``ast.Call`` that lives inside a nested (async) function
    def within *func* — i.e. calls that run on the NESTED frame, not *func*'s
    own frame. Used to distinguish a blocking read that is off-loaded (wrapped
    in a local closure passed to ``to_thread``) from one invoked on the loop."""
    nested: set[int] = set()
    for node in ast.walk(func):
        if node is not func and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    nested.add(id(sub))
    return nested


def _direct_frame_calls(func: ast.AST, names: set[str]) -> list[tuple[str, int]]:
    """Calls to any callee in *names* that run on *func*'s OWN frame (excluding
    calls inside nested function defs — those run off-frame, e.g. when the
    nested def is off-loaded via ``asyncio.to_thread``)."""
    nested = _nested_call_node_ids(func)
    hits: list[tuple[str, int]] = []
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and id(node) not in nested:
            nm = _called_name(node)
            if nm in names:
                hits.append((nm, node.lineno))
    return hits


@pytest.mark.unit
class TestDebugMessagesStaysOffLoop:
    """The single known async caller must keep its ``asyncio.to_thread``
    off-loading of the blocking builder."""

    def test_debug_messages_uses_to_thread_for_builder(self):
        path = os.path.join(_ROUTES_DIR, 'conversations.py')
        tree = _parse(path)

        debug_fn = next(
            (n for n in _async_func_nodes(tree) if n.name == 'debug_messages'),
            None,
        )
        assert debug_fn is not None, (
            'routes/conversations.py::debug_messages not found as an async '
            'def — if it was renamed/removed, update this guard'
        )

        # It must NOT invoke a named blocking builder directly on the loop …
        raw = _raw_blocking_calls_in_async(debug_fn)
        assert not raw, (
            f'debug_messages invokes a blocking builder directly on the event '
            f'loop at line(s) {[ln for _n, ln in raw]} — off-load it via '
            f'`await asyncio.to_thread(...)`'
        )

        # … it MUST actively off-load work via asyncio.to_thread / executor …
        to_thread_calls = [
            c for c in ast.walk(debug_fn)
            if isinstance(c, ast.Call) and _is_to_thread_call(c)
        ]
        assert to_thread_calls, (
            'debug_messages no longer off-loads via asyncio.to_thread — the '
            'FUSE/cross-DC DB read would block the event loop for every '
            'concurrent request'
        )

        # … and the blocking DB-read primitive must run ONLY inside the
        # off-loaded nested closure, never on the coroutine's own frame. (The
        # route wraps `_load_messages_from_db` + `build_wire_messages` in a
        # local `_build` closure that is passed to asyncio.to_thread.)
        on_loop = _direct_frame_calls(debug_fn, _BLOCKING_DB_READS)
        assert not on_loop, (
            f'debug_messages runs a blocking DB read on the event loop directly '
            f'at line(s) {[ln for _n, ln in on_loop]} — it must live inside the '
            f'closure that is off-loaded via asyncio.to_thread'
        )


@pytest.mark.unit
class TestNoAsyncRouteCallsBuilderRaw:
    """Sweep every ``async def`` in ``routes/`` and assert none invokes a
    blocking builder directly.  Off-loading via ``asyncio.to_thread`` (passing
    the builder by reference) is the only allowed pattern from a coroutine."""

    def test_no_async_route_invokes_blocking_builder_directly(self):
        offenders: list[str] = []
        for relpath, abspath in _iter_route_py_files():
            tree = _parse(abspath)
            for fn in _async_func_nodes(tree):
                for name, lineno in _raw_blocking_calls_in_async(fn):
                    offenders.append(f'{relpath}:{lineno} async def '
                                     f'{fn.name}() calls {name}(...) directly')

        assert not offenders, (
            'async route(s) call a blocking conv_message_builder entry point '
            'directly on the event loop — wrap each in '
            '`await asyncio.to_thread(<builder>, …)`:\n  '
            + '\n  '.join(offenders)
        )


@pytest.mark.unit
class TestGuardSelfCheck:
    """Belt-and-suspenders: the AST helpers actually detect what they claim,
    so the two guards above can't pass vacuously after a refactor of this
    file."""

    def test_detects_a_raw_call_inside_async(self):
        tree = ast.parse(
            'import asyncio\n'
            'async def f():\n'
            '    return build_api_messages_from_db(cid, cfg)\n'
        )
        fn = _async_func_nodes(tree)[0]
        raw = _raw_blocking_calls_in_async(fn)
        assert raw == [('build_api_messages_from_db', 3)]

    def test_allows_to_thread_wrapped_call(self):
        tree = ast.parse(
            'import asyncio\n'
            'async def f():\n'
            '    return await asyncio.to_thread(build_api_messages_from_db, cid, cfg)\n'
        )
        fn = _async_func_nodes(tree)[0]
        # The builder appears only as a reference arg to to_thread, not as a
        # direct Call callee → no offenders.
        assert _raw_blocking_calls_in_async(fn) == []
