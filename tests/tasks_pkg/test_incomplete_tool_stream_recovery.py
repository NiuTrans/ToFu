"""Recovery must agree with provider completion before any tool can execute."""
from copy import deepcopy
import threading

import pytest

from lib.llm.stream_result import ProviderStreamResult, ProviderStreamState
from lib.tasks_pkg.stream_handler.api import RecoveryAction, analyse_stream_result
from lib.tasks_pkg.stream_handler import _budget

pytestmark = pytest.mark.unit


def _task():
    return {'id': 'incomplete-tools', 'aborted': False,
            'content': 'earlier response', 'thinking': '',
            '_round_base_content': 'earlier response', '_round_base_thinking': '',
            'content_lock': threading.Lock()}


def _stream(state, *, saw_done=False, usage=None):
    finished = state is ProviderStreamState.PROVIDER_FINISHED
    return ProviderStreamResult(
        message={'role': 'assistant', 'content': 'I will inspect the file.',
                 'tool_calls': [{'id': 'read-one', 'type': 'function',
                                 'function': {'name': 'read_file',
                                              'arguments': '{"path":"file.txt"}'}}]},
        compatibility_finish_reason='stop', usage=usage,
        state=state, saw_done=saw_done, saw_finish_reason=finished,
        provider_finish_reason='tool_calls' if finished else None,
        malformed_frame_count=1 if state is ProviderStreamState.MALFORMED_STREAM else 0)


@pytest.fixture
def recovery_events(monkeypatch):
    events = []
    monkeypatch.setattr('lib.tasks_pkg.stream_handler._analyse.append_event',
                        lambda task, event: events.append(event))
    monkeypatch.setattr('lib.tasks_pkg.stream_handler._analyse.emit_phase',
                        lambda task, phase, **fields: events.append(fields))
    monkeypatch.setattr(_budget, '_interruptible_sleep', lambda *args: None)
    return events


@pytest.mark.parametrize('saw_done', [False, True])
@pytest.mark.parametrize('usage', [None, {'_flood_salvage': True}])
def test_parseable_tool_prefix_without_provider_finish_retries(
        recovery_events, saw_done, usage):
    task = _task()
    stream = _stream(ProviderStreamState.PREMATURE_CLOSE, saw_done=saw_done, usage=usage)
    history = [{'role': 'tool', 'tool_call_id': 'earlier', 'content': 'already executed'}]
    before = deepcopy(history)
    decision = analyse_stream_result({}, 'stop', task, 'test', 'test-model',
                                     4, 0, history, stream_result=stream)
    assert decision.action is RecoveryAction.CONTINUE
    assert decision.premature_retry_count == 1
    assert history == before
    assert task['content'] == 'earlier response'
    assert any(event.get('bucket') == 'incomplete_tool_stream' for event in recovery_events)


def test_incomplete_tool_stream_exhaustion_is_actionable(recovery_events):
    task = _task()
    stream = _stream(ProviderStreamState.PREMATURE_CLOSE)
    decision = analyse_stream_result({}, 'stop', task, 'test', 'test-model', 4,
        _budget._PREMATURE_RETRY_MAX_CLASSIC, [], stream_result=stream)
    assert decision.action is RecoveryAction.BREAK
    assert decision.last_finish_reason == 'premature_close'
    assert task['error']['kind'] == 'premature_close'
    assert task['content'] == 'earlier response'


def test_provider_finish_without_done_still_allows_tools(recovery_events):
    task = _task()
    stream = _stream(ProviderStreamState.PROVIDER_FINISHED,
                     usage={'_missing_done': True, '_stream_anomaly': True})
    decision = analyse_stream_result({}, 'stop', task, 'test', 'test-model',
                                     4, 0, [], stream_result=stream)
    assert decision.action is RecoveryAction.PROCEED
    assert not recovery_events


def test_flood_hint_cannot_override_malformed_typed_stream(recovery_events):
    task = _task()
    stream = _stream(ProviderStreamState.MALFORMED_STREAM,
                     usage={'_flood_salvage': True})
    decision = analyse_stream_result({}, 'stop', task, 'test', 'test-model',
                                     4, 0, [], stream_result=stream)
    assert decision.action is RecoveryAction.CONTINUE
