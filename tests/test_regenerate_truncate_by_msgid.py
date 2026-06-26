#!/usr/bin/env python3
"""Phase 3 — msgId-authoritative truncate point for /api/v1/chat/regenerate.

The client now sends ``truncateToMsgId`` (the stable id of the user message to
keep-and-resend-from) alongside the legacy ``truncateToIndex``. The server
resolves the id to its CURRENT index via ``find_message_by_id``; that index
wins (drift-proof). When the id is absent (older client) or doesn't resolve
(message since deleted), it falls back to the supplied index — strictly
additive.

The resolution core is ``lib.tasks_pkg.manager.find_message_by_id``; these
tests pin its drift/fallback semantics and assert the route wires it.
"""
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit


def _msgs():
    # A realistic conv tail; each message carries a stable _msgId.
    return [
        {'role': 'user', 'content': 'q1', '_msgId': 'm-a'},
        {'role': 'assistant', 'content': 'a1', '_msgId': 'm-b'},
        {'role': 'user', 'content': 'q2', '_msgId': 'm-c'},
        {'role': 'assistant', 'content': 'a2', '_msgId': 'm-d'},
    ]


def test_msgid_resolves_to_current_index():
    from lib.tasks_pkg.manager import find_message_by_id
    idx, msg = find_message_by_id(_msgs(), 'm-c')
    assert idx == 2
    assert msg['content'] == 'q2'


def test_msgid_resolution_is_drift_proof():
    """If a writer PREPENDED a message (index drift), the stale client index
    points at the wrong message but the msgId still resolves correctly."""
    from lib.tasks_pkg.manager import find_message_by_id
    msgs = _msgs()
    # Simulate drift: a new turn was inserted at the front since the client read.
    msgs.insert(0, {'role': 'user', 'content': 'q0', '_msgId': 'm-z'})
    # Client thought 'q2' was at index 2; it's now at index 3.
    idx, msg = find_message_by_id(msgs, 'm-c')
    assert idx == 3, 'msgId must resolve to the CURRENT index, not the stale one'
    assert msg['content'] == 'q2'


def test_unresolved_msgid_returns_none_for_fallback():
    """A deleted/unknown msgId returns (None, None) so the route falls back to
    the supplied truncateToIndex."""
    from lib.tasks_pkg.manager import find_message_by_id
    idx2, msg2 = find_message_by_id(_msgs(), 'm-deleted')
    assert idx2 is None and msg2 is None


def test_empty_or_missing_msgid_is_falsy():
    from lib.tasks_pkg.manager import find_message_by_id
    assert find_message_by_id(_msgs(), '') == (None, None)
    assert find_message_by_id(_msgs(), None) == (None, None)


def test_regenerate_route_wires_truncate_to_msgid():
    """The route must consult truncateToMsgId with an index fallback — assert
    against source (the route handler is hard to unit-drive through the full
    HTTP stack; the resolution semantics are covered above)."""
    pytest.importorskip('quart')
    import quart
    sys.modules['flask'] = quart  # routes/* import flask → quart shim (websocket etc.)
    import routes.chat as c
    src = inspect.getsource(c.chat_regenerate)
    assert "data.get('truncateToMsgId')" in src, \
        'chat_regenerate no longer reads truncateToMsgId'
    assert 'find_message_by_id' in src, \
        'chat_regenerate no longer resolves the msgId'
    # Fallback must be preserved: truncateToIndex stays required + used.
    assert "data.get('truncateToIndex')" in src


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
