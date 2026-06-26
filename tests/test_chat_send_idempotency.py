#!/usr/bin/env python3
"""Test #5 of the sync-robustness pass (2026-06-25): the UI send path
(/api/v1/chat/start and /api/v1/chat/send) is idempotent.

A retried POST (client never received the first response, network retry) used
to spawn a SECOND task — wasting the first's work and double-charging tokens.
The `abort_running_tasks_for_conv` guard prevented two CONCURRENT tasks but is
not idempotency. We now apply the existing, tested `@idempotent_post()`
decorator (lib/idempotency.py) so a duplicate `Idempotency-Key` replays the
cached `{taskId}` instead of creating a new task. No-op when the header is
absent (fully backward-compatible).

NOTE: the headless `/api/v1/chat/completions` endpoint was ALREADY idempotent
(tests/test_api_v1_chat_route.py::test_idempotency_key_replays). This covers
the DISTINCT UI send path (chat_start / chat_send in routes/chat.py), which
was not.

This test installs the quart→flask shim (server.py does this at boot) so
routes/chat.py imports, then introspects that both handlers carry the
idempotency wrapper. The replay MECHANISM itself is covered end-to-end by
test_api_v1_chat_route.py against the same decorator + cache.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit


def _install_shim():
    """Map flask→quart so routes/* import (mirrors server.py + the
    test_api_v1_chat_route harness). Quart provides .websocket on blueprints."""
    import quart
    sys.modules['flask'] = quart
    for attr in ('json', 'globals', 'helpers', 'wrappers', 'ctx'):
        qs = f'quart.{attr}'
        if qs in sys.modules:
            sys.modules[f'flask.{attr}'] = sys.modules[qs]


def test_chat_send_path_is_idempotent_decorated():
    """Assert the UI send handlers are decorated with @idempotent_post().

    Introspecting the wrapper chain is ambiguous (functools.wraps makes both
    require_scope's and idempotent_post's layers report co_name='wrapper' +
    module='routes.chat'), so we assert against the decorator SOURCE — the
    unambiguous, stable signal that the decorator is wired on each handler.
    """
    pytest.importorskip('quart')
    _install_shim()
    import inspect

    import routes.chat as c

    assert hasattr(c, 'idempotent_post'), 'idempotent_post not imported into routes.chat'
    src = inspect.getsource(c)

    def _decorated(handler_def):
        # Find `def <handler>(` and confirm @idempotent_post() appears in the
        # decorator block immediately above it (within the preceding ~5 lines).
        idx = src.index('\ndef ' + handler_def)
        block = src[:idx]
        # last 6 lines before the def = its decorator stack
        preceding = '\n'.join(block.splitlines()[-6:])
        return '@idempotent_post()' in preceding

    assert _decorated('chat_start('), 'chat_start missing @idempotent_post()'
    assert _decorated('chat_send('), 'chat_send missing @idempotent_post()'


def test_idempotency_noop_without_header():
    """Decorator must be a pass-through when no Idempotency-Key is present —
    existing clients (which don't send the header) are unaffected."""
    pytest.importorskip('quart')
    from lib.idempotency import idempotent_post

    calls = []

    @idempotent_post()
    def _handler():
        calls.append(1)
        return {'taskId': 't1'}, 200

    # No request context / no header → must just call through (not raise,
    # not cache). The decorator guards header access with try/except.
    rv = _handler()
    assert rv == ({'taskId': 't1'}, 200)
    assert len(calls) == 1


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
