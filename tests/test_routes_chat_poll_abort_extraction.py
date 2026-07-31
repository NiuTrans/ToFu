#!/usr/bin/env python3
"""Wire-parity for pt_04686ac6 slice 10 — poll/abort cluster extraction.

Board epic ``pt_04686ac6054a451e``: split routes/chat.py into a sub-package.
Prior slices delivered the business-logic-sink half via ``lib/chat_dispatch.py``
(slices 5-9, chat_send/chat_stream/chat_continue → 34% reduction of chat.py).
This slice extends the extraction to the poll_abort cluster.

**Extracted this slice**: 4 handlers moved to ``routes/chat_poll_abort.py``:

  * ``chat_abort_conv(conv_id)`` — POST /api/v1/chat/abort-conv/<conv_id>
  * ``chat_abort(task_id)``       — POST /api/v1/chat/abort/<task_id>
  * ``chat_poll(task_id)``        — GET  /api/v1/chat/poll/<task_id>
  * ``chat_flow_trace(task_id)``  — GET  /api/v1/chat/flow-trace/<task_id>

Follows the same "sibling module + side-effect import" pattern the epic has
used since slice 1 (routes/chat_helpers.py, routes/chat_state.py,
routes/chat_side_effects.py, routes/chat_task_start.py, routes/chat_queue.py,
routes/chat_human_io.py, routes/chat_tool_state.py — all sibling files that
attach to the same chat_bp / api_v1_chat_bp). NOT a package conversion — the
sibling-module pattern is byte-equivalent for the Blueprint URL registration
and preserves every ``from routes.chat import X`` call site through the
existing re-export machinery.

**What the test enforces**:

  1. Module presence: ``routes.chat_poll_abort`` exists and exposes all 4
     handler functions at module level.
  2. Blueprint wiring: routes/__init__.py's ``chat_poll_abort`` side-effect
     import is registered — otherwise the ``@api_v1_chat_bp.route(...)``
     decorators never fire and the URLs 404.
  3. Delegation from routes/chat.py: the 4 inline handler definitions are
     GONE (guards against a silent revert that puts them back).
  4. Wire-parity for the URL rules: the pre-split rule set is preserved
     (uses the existing test_api_v1_chat_bp_rules_snapshot machinery in
     test_routes_chat_wire_parity.py — this file only adds symbol/module
     guards, the rules snapshot is already covered).

**Byte-parity**: every handler body is moved verbatim — same imports, same
audit_log call, same log lines, same URL patterns, same endpoint names.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart
sys.modules.setdefault('flask', _quart)

try:
    import pytest
except ImportError:  # pragma: no cover
    pytest = None  # type: ignore[assignment]


def _unit(fn):
    return fn if pytest is None else pytest.mark.unit(fn)


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


_POLL_ABORT_HANDLERS = (
    'chat_abort_conv',
    'chat_abort',
    'chat_poll',
    'chat_flow_trace',
)


@_unit
def test_chat_poll_abort_module_exists():
    """Slice 10 (pt_04686ac6): routes.chat_poll_abort exists as an
    importable module."""
    import importlib
    mod = importlib.import_module('routes.chat_poll_abort')
    assert mod is not None


@_unit
def test_chat_poll_abort_exposes_all_four_handlers():
    """Slice 10: every extracted handler is a module-level callable in
    routes.chat_poll_abort."""
    import importlib
    mod = importlib.import_module('routes.chat_poll_abort')
    missing = [n for n in _POLL_ABORT_HANDLERS if not hasattr(mod, n)]
    assert not missing, (
        f'routes.chat_poll_abort missing handlers: {missing}. Slice 10 must '
        f'move all 4 handler functions to the new module.'
    )
    for name in _POLL_ABORT_HANDLERS:
        obj = getattr(mod, name)
        assert callable(obj), (
            f'routes.chat_poll_abort.{name} is not callable ({type(obj).__name__})'
        )


@_unit
def test_routes_init_side_effect_imports_chat_poll_abort():
    """Slice 10: routes/__init__.py must side-effect import
    ``chat_poll_abort`` so its @api_v1_chat_bp.route decorators fire.
    Same pattern as chat_queue / chat_human_io / chat_tool_state."""
    init_path = os.path.join(_ROOT, 'routes/__init__.py')
    with open(init_path, encoding='utf-8') as f:
        src = f.read()
    assert 'chat_poll_abort' in src, (
        'routes/__init__.py MUST side-effect import chat_poll_abort — '
        'without it the poll/abort URL decorators never register on '
        'api_v1_chat_bp and the endpoints 404.'
    )


@_unit
def test_routes_chat_no_longer_defines_poll_abort_handlers():
    """Slice 10: the four handler DEFINITIONS must be gone from
    routes/chat.py — moving to routes.chat_poll_abort. A silent revert
    would re-add them.
    """
    with open(os.path.join(_ROOT, 'routes/chat.py'), encoding='utf-8') as f:
        src = f.read()
    for handler in _POLL_ABORT_HANDLERS:
        # Look for the ``def <handler>(`` pattern at line start (module-level
        # definition). We tolerate an equivalent re-export ``from
        # routes.chat_poll_abort import chat_poll`` — that's the facade
        # pattern the extraction preserves for external callers.
        import re as _re
        assert not _re.search(rf'^def {handler}\(', src, _re.M), (
            f'routes/chat.py still defines {handler}() at module level. '
            f'Slice 10 must move the definition to routes/chat_poll_abort.py '
            f'(a re-export line ``from routes.chat_poll_abort import {handler}`` '
            f'is fine, but a re-declared ``def {handler}(...)`` is not)'
        )


@_unit
def test_poll_abort_handlers_still_importable_from_routes_chat():
    """Slice 10: existing tests / code that does
    ``from routes.chat import chat_poll`` MUST still work — the extraction
    preserves the facade re-export pattern established by slices 1-9.
    """
    import importlib
    mod = importlib.import_module('routes.chat')
    missing = [n for n in _POLL_ABORT_HANDLERS if not hasattr(mod, n)]
    assert not missing, (
        f'routes.chat lost handler re-exports after slice 10: {missing}. '
        f'The facade MUST re-export every moved handler so existing '
        f'callers keep working (matches the chat_helpers / chat_state / '
        f'chat_task_start pattern).'
    )


if __name__ == '__main__':
    for fn in [
        test_chat_poll_abort_module_exists,
        test_chat_poll_abort_exposes_all_four_handlers,
        test_routes_init_side_effect_imports_chat_poll_abort,
        test_routes_chat_no_longer_defines_poll_abort_handlers,
        test_poll_abort_handlers_still_importable_from_routes_chat,
    ]:
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
