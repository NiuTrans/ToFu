"""Independent API processes observe committed conversation invalidations."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import uuid

import pytest

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api


def test_conversation_watch_tracks_commit_without_worker_queue_noise(semantic_authority):
    factory, owner, _engine = semantic_authority
    client = factory()
    before = client.conversation_watch(cursor=None, deadline_unix_ms=int((time.time() + 5) * 1000))
    conversation = 'watch-' + uuid.uuid4().hex
    from lib import turn_lifecycle as turns
    from unittest.mock import patch
    with patch.object(turns, '_turn_client', lambda **kw: factory(**kw)):
        turns.create_turn_pair(conversation, command_id=conversation,
            input_projection={'content': 'watch'}, config={}, user_id=owner,
            conversation_defaults={'allowCreate': True, 'title': 'Watch', 'settings': {}})
    after = client.conversation_watch(cursor=before['cursor'], deadline_unix_ms=int((time.time() + 5) * 1000))
    assert after['cursor'] != before['cursor']
    assert after['conversations'] is None  # Commit in the gap still invalidates.
    from lib.conversation_sync.delivery_runtime import ConversationWakeSource
    observed = threading.Event()
    source = ConversationWakeSource(factory)
    source.start(lambda scopes: observed.set())
    try:
        with patch.object(turns, '_turn_client', lambda **kw: factory(**kw)):
            turns.create_turn_pair(conversation + '-live', command_id=conversation + '-live',
                input_projection={'content': 'live'}, config={}, user_id=owner,
                conversation_defaults={'allowCreate': True, 'title': 'Watch', 'settings': {}})
        assert observed.wait(5)
    finally:
        source.close()
    assert not source._thread.is_alive()



@pytest.mark.unit
def test_conversation_wait_coalesces_scopes_and_ignores_worker_changes():
    from lib.storage_authority_sql.worker_control import WorkerControl
    control = WorkerControl()
    conversation = control.conversations
    before = conversation.watch({'cursor': None}, time.monotonic())
    control.publish(True, {'worker'})
    assert conversation.watch({'cursor': before['cursor']}, time.monotonic()) == {
        **before, 'conversations': [],
    }
    waiting = threading.Event()
    # Synchronize enrollment rather than relying on the scheduler.
    condition = conversation._condition
    original_wait = condition.wait
    def enrolled(timeout=None):
        waiting.set()
        return original_wait(timeout)
    condition.wait = enrolled
    with ThreadPoolExecutor() as pool:
        pending = pool.submit(conversation.watch, {'cursor': before['cursor']}, time.monotonic() + 5)
        assert waiting.wait(5)
        control.publish(False, set(), {(41, 'one'), (42, 'one'), (41, 'one')})
        result = pending.result(5)
    assert result['conversations'] == [
        {'userId': 41, 'conversationId': 'one'},
        {'userId': 42, 'conversationId': 'one'},
    ]
    assert conversation._waiters == {}
    control.close()


def test_conversation_watch_close_leaves_other_requests_live(semantic_authority):
    from tofu_db._request_cancellation import RequestCancellation
    factory, _owner, _engine = semantic_authority
    client = factory()
    cursor = client.conversation_watch(cursor=None, deadline_unix_ms=int((time.time() + 5) * 1000))['cursor']
    cancellation = RequestCancellation()
    with ThreadPoolExecutor() as pool:
        pending = pool.submit(client.conversation_watch, cursor=cursor,
            deadline_unix_ms=int((time.time() + 60) * 1000), cancellation=cancellation)
        cancellation.cancel()
        with pytest.raises(Exception):
            pending.result(5)
    assert factory().worker_watch(cursor=None, deadline_unix_ms=int((time.time() + 5) * 1000))['cursor']
