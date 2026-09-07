#!/usr/bin/env python3
"""Event-driven cross-device sync — server wake-hint seam.

WHY
---
Cross-device conversation sync used to be PULL-only (refocus + a periodic
poll), so a sibling device only reconciled on the next tick — the "needs a
manual refresh" pain. The fix routes every authoritative conversation mutation
through ONE seam, ``notify_conv_changed``, which publishes a tiny real-time ``notify`` frame
to connected clients:

    { type:'conv_changed'|'conv_deleted', convId, rev?, userId }

The frame is a targeting HINT, not the data. The client rev-gates on it (a
frame whose rev is <= its known TurnState/catalog revision is a no-op), wakes
Conversation Sync for a newer revision, then rechecks after the debounce. A
metadata-only change (rename / folder — the DB rev trigger only bumps on a
messages change) omits ``rev`` and therefore retains the authoritative sidebar
refresh.

This suite captures the published frame directly (monkeypatching
``lib.agent_core.push.push_event``) and asserts the shape for each mutate
"kind":
  * content change → carries a numeric rev;
  * metadata-only (rev=None) → no rev key (client falls back to list refresh);
  * delete → type conv_deleted;
  * userId scoping present (multi-user forward-safety);
  * fail-open: a push_event that raises never breaks the mutation path.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pytestmark = pytest.mark.unit

TEST_OWNER_USER_ID = 1


@pytest.fixture
def captured(monkeypatch):
    """Capture every push_event(channel, task_id, payload) the seam emits."""
    frames = []

    def _fake_push_event(channel, task_id, payload, *, user_id):
        assert payload['userId'] == user_id
        frames.append({
            'channel': channel,
            'taskId': task_id,
            'payload': payload,
            'ownerUserId': user_id,
        })

    # Patch at the definition module so the lazy `from lib.agent_core.push
    # import push_event` inside notify_conv_changed picks up the fake.
    import lib.agent_core.push as push_mod
    monkeypatch.setattr(push_mod, 'push_event', _fake_push_event)
    return frames


def test_content_change_emits_conv_changed_with_rev(captured):
    from lib.conversations.change_notifications import notify_conv_changed
    notify_conv_changed('conv-A', rev=7, user_id=TEST_OWNER_USER_ID)
    assert len(captured) == 1
    f = captured[0]
    assert f['channel'] == 'notify'
    assert f['taskId'] == 'conv-A'
    p = f['payload']
    assert p['type'] == 'conv_changed'
    assert p['convId'] == 'conv-A'
    assert p['rev'] == 7
    assert p['userId'] == TEST_OWNER_USER_ID
    assert f['ownerUserId'] == TEST_OWNER_USER_ID


def test_metadata_only_omits_rev(captured):
    """rev=None (rename / folder / activeTaskId) → no rev key, so the client
    routes to a debounced sidebar refresh rather than a body refetch."""
    from lib.conversations.change_notifications import notify_conv_changed
    notify_conv_changed('conv-B', rev=None, user_id=TEST_OWNER_USER_ID)
    assert len(captured) == 1
    p = captured[0]['payload']
    assert p['type'] == 'conv_changed'
    assert 'rev' not in p, 'metadata-only frame must NOT carry a rev'


def test_delete_emits_conv_deleted(captured):
    from lib.conversations.change_notifications import notify_conv_changed
    notify_conv_changed('conv-C', deleted=True, user_id=TEST_OWNER_USER_ID)
    assert len(captured) == 1
    p = captured[0]['payload']
    assert p['type'] == 'conv_deleted'
    assert p['convId'] == 'conv-C'


def test_userid_scoping_is_forwarded(captured):
    from lib.conversations.change_notifications import notify_conv_changed
    notify_conv_changed('conv-D', rev=3, user_id=42)
    frame = captured[0]
    p = frame['payload']
    assert p['userId'] == 42, 'the frame must carry the mutating user for D4 gating'
    assert frame['ownerUserId'] == 42


def test_non_int_rev_is_dropped_not_crashed(captured):
    """A non-int rev is defensively dropped (logged debug), never crashes the
    mutation path — the frame is still emitted, just without a rev."""
    from lib.conversations.change_notifications import notify_conv_changed
    notify_conv_changed('conv-E', rev='not-a-number', user_id=TEST_OWNER_USER_ID)
    assert len(captured) == 1
    assert 'rev' not in captured[0]['payload']


def test_fail_open_push_error_does_not_raise(monkeypatch):
    """A push transport failure never breaks an authoritative mutation."""
    import lib.agent_core.push as push_mod

    def _boom(*a, **k):
        raise RuntimeError('push transport down')

    monkeypatch.setattr(push_mod, 'push_event', _boom)
    from lib.conversations.change_notifications import notify_conv_changed
    # Must not raise.
    notify_conv_changed('conv-F', rev=1, user_id=TEST_OWNER_USER_ID)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
