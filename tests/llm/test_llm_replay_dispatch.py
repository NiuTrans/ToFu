"""Replay contract failures stop real dispatch without provider retry or debt."""
import asyncio

import pytest

from tests.llm.test_inference_local_dispatch_errors import dispatch_fixture
from tests.support.llm_replay import ReplaySession, ScenarioDriftError

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('entry', ['sync_stream', 'async_stream', 'chat'])
def test_replay_failure_never_retries_or_penalizes_provider(
        tmp_path, monkeypatch, dispatch_fixture, entry):
    api, dispatcher, sleep = dispatch_fixture
    script = tmp_path / 'script.jsonl'
    script.write_text('', encoding='utf-8')
    session = ReplaySession(script, cwd=str(tmp_path))
    session.install(monkeypatch)
    monkeypatch.setattr(api, '_adapt_stream_body_for_slot',
                        lambda *args, **kwargs: {'messages': []})
    snapshots = [slot.health() for slot in dispatcher.slots]
    messages = [{'role': 'user', 'content': 'An unrecorded request'}]
    kwargs = {'max_retries': 3, 'owner_user_id': 987654}

    with pytest.raises(ScenarioDriftError, match='script exhausted'):
        if entry == 'chat':
            api.dispatch_chat(messages, **kwargs)
        elif entry == 'async_stream':
            asyncio.run(api.async_dispatch_stream(messages, **kwargs))
        else:
            api.dispatch_stream(messages, **kwargs)

    assert len(dispatcher.picks) == 1
    assert [slot.release_calls for slot in dispatcher.slots] == [1, 0]
    assert [slot.inflight for slot in dispatcher.slots] == [0, 0]
    assert [slot.health() for slot in dispatcher.slots] == snapshots
    for slot in dispatcher.slots:
        slot.record_error.assert_not_called()
        slot.record_success.assert_not_called()
        slot.record_truncation.assert_not_called()
    sleep.assert_not_called()
