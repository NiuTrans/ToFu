"""Conversation delivery shares one lifecycle watch and scoped replay hints."""
import asyncio
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


def test_scoped_hint_and_epoch_gap_wake_the_correct_subscriptions():
    from lib.conversation_sync.broker import ConversationWakeBroker
    from lib.conversation_sync.delivery_runtime import ConversationWakeSource

    async def run():
        broker = ConversationWakeBroker()
        one, two = broker.subscribe(41, 'same'), broker.subscribe(42, 'same')
        source = ConversationWakeSource(None)
        before = {'epoch': 'one', 'change': 0}
        after = {'epoch': 'one', 'change': 1}
        source._observe(before, {'cursor': after, 'conversations': [
            {'userId': 41, 'conversationId': 'same'}]}, broker.invalidate)
        assert await one.wait(1)
        assert not await two.wait(0)
        source._observe(after, {'cursor': after, 'conversations': None}, broker.invalidate)
        assert not await one.wait(0)
        source._observe(after, {'cursor': {'epoch': 'two', 'change': 0},
                               'conversations': []}, broker.invalidate)
        assert await one.wait(1)
        assert await two.wait(1)
        one.close()
        two.close()
        assert broker.snapshot()['active'] == 0

    asyncio.run(run())


def test_delivery_lifecycle_starts_once_and_joins_before_removal(monkeypatch):
    from lib.conversation_sync import delivery_runtime as runtime
    calls = []
    class Source:
        def __init__(self, factory):
            calls.append('construct')
        def start(self, hint):
            calls.append('start')
        def close(self):
            assert app.extensions
            calls.append('join')
    app = SimpleNamespace(extensions={})
    monkeypatch.setattr(runtime, 'ConversationWakeSource', Source)
    runtime.ensure_started(app)
    runtime.ensure_started(app)
    runtime.stop(app)
    runtime.stop(app)
    assert calls == ['construct', 'start', 'join']
    assert app.extensions == {}


@pytest.mark.parametrize('response', [
    {},
    {'format': 'tofu.conversation-control/v1', 'cursor': {'epoch': 'a', 'change': 0},
     'conversations': [{'userId': True, 'conversationId': 'foreign'}]},
    {'format': 'tofu.conversation-control/v1', 'cursor': {'epoch': 'a', 'change': -1},
     'conversations': None},
])
def test_malformed_hints_are_refused(response):
    from lib.storage.errors import StorageError
    from lib.conversation_sync.delivery_runtime import ConversationWakeSource
    with pytest.raises(StorageError, match='Invalid conversation'):
        ConversationWakeSource(None)._validate(response)
