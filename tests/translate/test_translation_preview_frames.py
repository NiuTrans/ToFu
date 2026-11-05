"""Per-segment transport does not reserialize completed history.

Each running preview is the cumulative translation of one block, published as
that block's latest value: a socket that is behind skips to the newest value,
and the final translation is always the block's last delivered value.
"""
import asyncio
import json
import threading

import pytest

from lib.translate import incremental

pytestmark = pytest.mark.unit

_TASK = {'id': 'fixture', 'convId': 'conversation', '_turnId': 'turn', '_userId': 9,
         'config': {'autoTranslate': True, 'uiLang': 'zh'}}


def test_large_completed_cache_does_not_expand_each_progress_frame(monkeypatch):
    accumulator = incremental._Accumulator(dict(_TASK))
    for index in range(2000):
        accumulator.translations[f'text:llm-{index}'] = 'cached-translation' * 100
    frames = []
    monkeypatch.setattr(accumulator, '_push',
                        lambda payload, **options: frames.append((payload, options)))
    accumulator._push_segment_progress('text:llm-2000', 'current-partial')
    assert frames == [({'type': 'running', 'status': 'running', 'statusKind': 'in_progress',
                        'partialByRound': {'text:llm-2000': 'current-partial'}},
                       {'latest_value_key': 'text:llm-2000'})]
    assert len(json.dumps(frames[0][0]).encode()) < 256


def test_preview_chunks_merge_to_every_completed_block(monkeypatch):
    accumulator = incremental._Accumulator(dict(_TASK))
    frames = []
    monkeypatch.setattr(accumulator, '_push',
                        lambda payload, **options: frames.append((payload, options)))
    def translate(original, progress_cb=None, **kwargs):
        progress_cb('partial:' + original)
        return 'translated:' + original, 'fixture-model'
    monkeypatch.setattr(accumulator, '_translate', translate)
    for index in range(1000):
        accumulator._translate_segment(f'text:llm-{index}', f'source-{index}', max_429_attempts=1, defer_on_shared_contention=False)
    merged = {}
    for frame, options in frames:
        assert len(frame['partialByRound']) == 1
        assert options == {'latest_value_key': next(iter(frame['partialByRound']))}
        merged.update(frame['partialByRound'])
    assert len(merged) == 1000
    assert merged['text:llm-999'] == 'translated:source-999'
    assert sum(len(json.dumps(frame).encode()) for frame, _ in frames) < 1000 * 400


@pytest.fixture
def push_hub(monkeypatch):
    from lib.agent_core import push

    hub = push.PushHub()
    monkeypatch.setattr(hub, '_register_subscription', lambda *args: None)
    monkeypatch.setattr(hub, '_deregister_subscription', lambda *args: None)
    monkeypatch.setattr(push, 'hub', hub)
    try:
        yield hub
    finally:
        hub.stop()


def _streaming_accumulator(monkeypatch, final):
    accumulator = incremental._Accumulator(dict(_TASK))

    def translate(original, progress_cb=None, **kwargs):
        for size in range(1, len(final) + 1):
            progress_cb(final[:size])
        return final, 'fixture-model'

    monkeypatch.setattr(accumulator, '_translate', translate)
    return accumulator


def test_unsent_cumulative_partials_are_superseded_by_the_final_translation(
        monkeypatch, push_hub):
    from lib.agent_core.push import PushClient

    final = '最终译文。' * 80
    client = PushClient(user_id=9)
    assert push_hub.register(client)
    push_hub.subscribe(client, 'translate', '*')
    accumulator = _streaming_accumulator(monkeypatch, final)
    accumulator._translate_segment('text:llm-1', 'source', max_429_attempts=1,
                                   defer_on_shared_contention=False)

    async def drain_ready():
        frames = []
        while not client._queue.empty():
            frames.append(json.loads(await client.drain(encoded=True)))
        return frames

    frames = asyncio.run(drain_ready())
    assert [frame['partialByRound'] for frame in frames] == [{'text:llm-1': final}]
    assert frames[0]['convId'] == 'conversation' and frames[0]['turnId'] == 'turn'
    assert accumulator.translations['text:llm-1'] == final


def test_a_live_socket_receives_ordered_prefixes_ending_with_the_final_translation(
        monkeypatch, push_hub):
    from lib.agent_core.push import PushClient

    final = 'cumulative translation ' * 120
    loop = asyncio.new_event_loop()
    serving = threading.Thread(target=loop.run_forever, daemon=True)
    serving.start()
    try:
        push_hub.set_loop(loop)
        client = PushClient(user_id=9)
        assert push_hub.register(client)
        push_hub.subscribe(client, 'translate', '*')
        received = []

        async def consume():
            while True:
                frame = json.loads(await client.drain(encoded=True))
                if frame.get('type') == 'done':
                    return
                received.append(frame['partialByRound']['text:llm-1'])

        consumer = asyncio.run_coroutine_threadsafe(consume(), loop)
        accumulator = _streaming_accumulator(monkeypatch, final)
        accumulator._translate_segment('text:llm-1', 'source', max_429_attempts=1,
                                       defer_on_shared_contention=False)
        accumulator._push({'type': 'done', 'status': 'done'})
        consumer.result(30)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        serving.join(10)
        loop.close()
    assert received[-1] == final
    assert all(final.startswith(value) for value in received)
    lengths = [len(value) for value in received]
    assert lengths == sorted(lengths)
