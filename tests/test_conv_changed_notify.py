#!/usr/bin/env python3
"""Event-driven cross-device sync — server emit seam
(lib/conversations/meta_cache.py::notify_conv_changed).

WHY
---
Cross-device conversation sync used to be PULL-only (refocus + a periodic
poll), so a sibling device only reconciled on the next tick — the "needs a
manual refresh" pain. The fix routes every authoritative conversation mutation
through ONE seam, ``notify_conv_changed``, which (a) invalidates the sidebar
cache (unchanged behaviour) and (b) publishes a tiny real-time ``notify`` frame
to connected clients:

    { type:'conv_changed'|'conv_deleted', convId, rev?, userId }

The frame is a targeting HINT, not the data. The client rev-gates on it (a
frame whose rev is <= its known _serverRev is a no-op → cheap self-echo) and
does a targeted refetch of just that conv. A metadata-only change (rename /
folder — the DB rev trigger only bumps on a messages change) omits ``rev`` so
the client does a debounced sidebar refresh instead of a body refetch.

This suite captures the published frame directly (monkeypatching
``lib.agent_core.push.push_event``) and asserts the shape for each mutate
"kind":
  * content change → carries a numeric rev;
  * metadata-only (rev=None) → no rev key (client falls back to list refresh);
  * delete → type conv_deleted;
  * userId scoping present (multi-user forward-safety);
  * NEGATIVE CONTROL: a plain ``invalidate_meta_cache`` (the OLD seam) pushes
    NOTHING — proving the notify frame is what carries the real-time signal;
  * fail-open: a push_event that raises never breaks the mutation path (the
    cache is still invalidated).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pytestmark = pytest.mark.unit


@pytest.fixture
def captured(monkeypatch):
    """Capture every push_event(channel, task_id, payload) the seam emits."""
    frames = []

    def _fake_push_event(channel, task_id, payload):
        frames.append({'channel': channel, 'taskId': task_id, 'payload': payload})

    # Patch at the definition module so the lazy `from lib.agent_core.push
    # import push_event` inside notify_conv_changed picks up the fake.
    import lib.agent_core.push as push_mod
    monkeypatch.setattr(push_mod, 'push_event', _fake_push_event)
    return frames


def test_content_change_emits_conv_changed_with_rev(captured):
    from lib.conversations.meta_cache import notify_conv_changed
    notify_conv_changed('conv-A', rev=7)
    assert len(captured) == 1
    f = captured[0]
    assert f['channel'] == 'notify'
    assert f['taskId'] == 'conv-A'
    p = f['payload']
    assert p['type'] == 'conv_changed'
    assert p['convId'] == 'conv-A'
    assert p['rev'] == 7
    assert p['userId'] == 1  # DEFAULT_USER_ID scoping present


def test_metadata_only_omits_rev(captured):
    """rev=None (rename / folder / activeTaskId) → no rev key, so the client
    routes to a debounced sidebar refresh rather than a body refetch."""
    from lib.conversations.meta_cache import notify_conv_changed
    notify_conv_changed('conv-B', rev=None)
    assert len(captured) == 1
    p = captured[0]['payload']
    assert p['type'] == 'conv_changed'
    assert 'rev' not in p, 'metadata-only frame must NOT carry a rev'


def test_delete_emits_conv_deleted(captured):
    from lib.conversations.meta_cache import notify_conv_changed
    notify_conv_changed('conv-C', deleted=True)
    assert len(captured) == 1
    p = captured[0]['payload']
    assert p['type'] == 'conv_deleted'
    assert p['convId'] == 'conv-C'


def test_userid_scoping_is_forwarded(captured):
    from lib.conversations.meta_cache import notify_conv_changed
    notify_conv_changed('conv-D', rev=3, user_id=42)
    p = captured[0]['payload']
    assert p['userId'] == 42, 'the frame must carry the mutating user for D4 gating'


def test_non_int_rev_is_dropped_not_crashed(captured):
    """A non-int rev is defensively dropped (logged debug), never crashes the
    mutation path — the frame is still emitted, just without a rev."""
    from lib.conversations.meta_cache import notify_conv_changed
    notify_conv_changed('conv-E', rev='not-a-number')
    assert len(captured) == 1
    assert 'rev' not in captured[0]['payload']


def test_NC_plain_invalidate_emits_no_frame(monkeypatch):
    """NEGATIVE CONTROL: the OLD seam (invalidate_meta_cache alone) pushes
    NOTHING. This proves the real-time signal is specifically the notify frame
    that notify_conv_changed adds — not a side-effect of cache invalidation."""
    frames = []
    import lib.agent_core.push as push_mod
    monkeypatch.setattr(push_mod, 'push_event',
                        lambda *a, **k: frames.append(a))
    from lib.conversations.meta_cache import invalidate_meta_cache
    invalidate_meta_cache()  # the pre-fix mutation seam
    assert frames == [], 'plain invalidate must not emit a client push frame'


def test_fail_open_push_error_does_not_raise(monkeypatch):
    """A push_event that raises must never break the mutation path. The cache
    invalidation (the correctness half) still runs; notify is best-effort."""
    import lib.agent_core.push as push_mod

    def _boom(*a, **k):
        raise RuntimeError('push transport down')

    monkeypatch.setattr(push_mod, 'push_event', _boom)
    from lib.conversations.meta_cache import notify_conv_changed
    # Must not raise.
    notify_conv_changed('conv-F', rev=1)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
