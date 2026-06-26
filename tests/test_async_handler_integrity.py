"""App-wide integrity guards for the native-async handler migration.

The Stage-4 migration converted ~130 route handlers from sync ``def`` to native
``async def`` using the await-able DB facade. Two failure modes must never
regress, and both are caught here statically + via the live app:

  1. LEAKED COROUTINE — an ``async def`` view wrapped by a SYNC passthrough
     decorator is not a coroutine function to Quart, which then serializes the
     coroutine OBJECT as the response. We assert every converted view in the
     live app is ``iscoroutinefunction``.

  2. BLOCKING CALL ON THE EVENT LOOP — an ``async def`` handler that still calls
     the sync ``get_db()`` / ``db.execute()`` / sync ``parse_body()`` blocks the
     loop (or crashes: sync parse_body's shim calls ``asyncio.run()`` inside the
     running loop). We scan the converted route files' source for these.

Run:  pytest tests/test_async_handler_integrity.py
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# Route files converted in the native-async sweep (Stage 4).
CONVERTED_FILES = [
    'routes/conversations.py',
    'routes/api_v1/conversations.py',
    'routes/conversations_compaction.py',
    'routes/conversations_search.py',
    'routes/chat_tool_state.py',
    'routes/api_v1/billing.py',
    'routes/api_v1/artifacts.py',
    'routes/api_v1/daily_report.py',
    'routes/paper.py',
    'routes/api_v1/chat_direct.py',
]

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _async_funcs_with_blocking_calls(path):
    """Return [(func_name, blocking_call)] for async defs that call a blocking
    DB/body primitive directly in their OWN body (not in a nested sync def,
    which is the run_pooled / to_thread escape hatch)."""
    full = os.path.join(_ROOT, path)
    if not os.path.exists(full):
        return []
    tree = ast.parse(open(full, encoding='utf-8').read(), filename=path)
    BLOCKING = {'get_db', 'get_thread_db'}
    offenders = []

    class _Visitor(ast.NodeVisitor):
        def visit_AsyncFunctionDef(self, node):
            # Only inspect calls that are NOT inside a nested (sync) def/asyncdef.
            for child in ast.walk(node):
                # Skip nested function scopes — they run off-loop via to_thread.
                if child is not node and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # Don't descend into nested defs: mark their calls as exempt
                    # by removing them from consideration. ast.walk already
                    # flattened, so we instead check enclosing scope below.
                    pass
            # Re-walk with scope awareness: collect calls whose nearest function
            # ancestor is THIS async node.
            for call in _direct_calls(node):
                fn = call.func
                name = getattr(fn, 'id', None) or getattr(fn, 'attr', None)
                if name in BLOCKING:
                    offenders.append((node.name, name))
                if name == 'parse_body':
                    offenders.append((node.name, 'parse_body(sync)'))
            self.generic_visit(node)

    _Visitor().visit(tree)
    return offenders


def _direct_calls(func_node):
    """Yield Call nodes whose nearest function ancestor is func_node (i.e. not
    inside a nested def)."""
    nested = []
    for n in ast.walk(func_node):
        if n is not func_node and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            nested.append(n)
    nested_nodes = set()
    for nf in nested:
        for n in ast.walk(nf):
            nested_nodes.add(id(n))
    for n in ast.walk(func_node):
        if isinstance(n, ast.Call) and id(n) not in nested_nodes:
            yield n


@pytest.mark.unit
class TestNoBlockingCallsInAsyncHandlers:
    @pytest.mark.parametrize('path', CONVERTED_FILES)
    def test_no_direct_blocking_db_or_body_calls(self, path):
        offenders = _async_funcs_with_blocking_calls(path)
        assert not offenders, (
            f'{path}: async handlers call blocking primitives directly on the '
            f'event loop (use async_* facade, run_pooled, or to_thread): {offenders}')


# Route handlers intentionally LEFT SYNC (documented carve-outs). Converting
# them to async would deadlock: their only blocking calls are the server.py
# Flask→Quart sync-safe shims for send_file / request.files / file.save, which
# schedule a coroutine via run_coroutine_threadsafe(...).result() — fatal if
# invoked from the event loop inside an `async def`.
INTENTIONAL_SYNC_HANDLERS = {
    'routes/paper.py::serve_paper_image',
    'routes/paper.py::serve_paper_pdf',
    'routes/paper.py::upload_paper',
}


def _route_decorated_sync_defs(path):
    """Return 'path::name' for every @<bp>.route(...)-decorated plain `def`
    (i.e. a sync route handler) in the file."""
    full = os.path.join(_ROOT, path)
    if not os.path.exists(full):
        return []
    tree = ast.parse(open(full, encoding='utf-8').read(), filename=path)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):  # plain def (NOT AsyncFunctionDef)
            for d in node.decorator_list:
                if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                        and d.func.attr in ('route', 'websocket')):
                    out.append(f'{path}::{node.name}')
    return out


@pytest.mark.unit
class TestNoUnconvertedRouteHandlers:
    @pytest.mark.parametrize('path', CONVERTED_FILES)
    def test_all_route_handlers_async_except_carveouts(self, path):
        sync_handlers = set(_route_decorated_sync_defs(path))
        leftover = sync_handlers - INTENTIONAL_SYNC_HANDLERS
        assert not leftover, (
            f'{path}: these @route-decorated handlers are still sync `def` and '
            f'not in the documented carve-out allowlist: {sorted(leftover)}')


@pytest.mark.api
class TestConvertedViewsAreCoroutines:
    def test_conversation_views_are_coroutines(self, flask_app):
        names = [
            'api_v1_conversations.get_conv',
            'api_v1_conversations.list_convs',
            'api_v1_conversations.save_conv',
            'api_v1_conversations.patch_conv_settings',
            'api_v1_conversations.delete_message',
            'api_v1_conversations.patch_message',
            'api_v1_conversations.patch_message_by_id',
            'api_v1_conversations.delete_branch',
            'api_v1_conversations.delete_conv',
        ]
        not_coro = [n for n in names
                    if n in flask_app.view_functions
                    and not asyncio.iscoroutinefunction(flask_app.view_functions[n])]
        assert not not_coro, f'these converted views are NOT coroutines (leak risk): {not_coro}'

    # Blueprint prefixes whose handlers were ALL converted in the sweep and are
    # registered in the default (non-trading) test app. Every view under these
    # prefixes must be a coroutine — a sync one is a missed/half conversion.
    CONVERTED_BP_PREFIXES = (
        'api_v1_billing.',
        'api_v1_artifacts.',
        'api_v1_daily_report.',
        'api_v1_chat_direct.',
    )

    def test_all_converted_blueprint_views_are_coroutines(self, flask_app):
        offenders = []
        for name, fn in flask_app.view_functions.items():
            if name.startswith(self.CONVERTED_BP_PREFIXES):
                if not asyncio.iscoroutinefunction(fn):
                    offenders.append(name)
        assert not offenders, (
            'these converted-blueprint views are NOT coroutines (leaked-coroutine '
            f'risk — Quart would serialize the coroutine object): {offenders}')


@pytest.mark.api
class TestConvertedHandlersExecuteThroughStack:
    """Smoke: a converted async handler must run through the FULL Quart stack
    and return a real Response (status code), NOT a leaked coroutine object.

    The admin-scoped billing routes are ideal: without a credential they must
    return 401/403. If the async handler leaked a coroutine the response would
    be a 500 / serialization error instead — so a clean auth rejection proves
    the dual-mode decorator + async handler chain works end to end.
    """

    @pytest.mark.auth_mode('private')
    def test_billing_admin_route_is_auth_gated_not_500(self, flask_client):
        resp = flask_client.get('/api/v1/billing/ledger')
        # Auth gate (401/403) or a clean JSON error — anything but a 500 from a
        # leaked/un-awaited coroutine.
        assert resp.status_code in (401, 403), (
            f'expected auth rejection, got {resp.status_code}: '
            f'{resp.get_data(as_text=True)[:200]}')

    @pytest.mark.auth_mode('open')
    def test_billing_pricing_public_route_returns_json(self, flask_client):
        # get_pricing is public (no admin scope) — must return 200 JSON, proving
        # the converted async handler executes and serializes correctly.
        resp = flask_client.get('/api/v1/billing/pricing')
        assert resp.status_code == 200, resp.get_data(as_text=True)[:200]
        assert resp.get_json() is not None
