"""Failed model dispatch closes its announced round before outer recovery."""
import pytest

from lib.error_envelope import from_exception
from lib.tasks_pkg.event_delivery import EventDeliveryError
from lib.tasks_pkg.orchestrator import _root_agent_loop as root_loop
from lib.tasks_pkg.turn_retry import should_auto_retry_turn
from tests.tasks_pkg.test_root_orchestrator_agent_loop_adapter import (
    _install_common, _request, _state,
)

pytestmark = pytest.mark.unit


class FatalDispatch(BaseException):
    pass


@pytest.fixture
def dispatch_boundary(monkeypatch):
    from lib.tasks_pkg.orchestrator import _round_open, _finalize

    trace = []
    _install_common(monkeypatch, trace)
    monkeypatch.setattr(root_loop, 'emit_round_open', _round_open.emit_round_open)
    monkeypatch.setattr(root_loop, 'prepare_round_context', lambda *a, **k: None)
    monkeypatch.setattr(root_loop, 'admit_round_context',
                        lambda *a, **k: {'totalTokens': 1, 'toolSchemaTokens': 0})
    events = []

    def publish(task, event):
        events.append(event)
        trace.append(('event', event['type']))

    monkeypatch.setattr(_round_open, 'append_event', publish)
    monkeypatch.setattr(_finalize, 'append_event', publish)
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event', publish)
    state = _state()
    task = {'id': 'dispatch-boundary', 'config': {}, 'content': '', 'thinking': ''}
    return root_loop._RootLoopHooks(_request(task, state)), events, trace


@pytest.mark.parametrize('stage', ['prepare_round_context', 'run_llm_call_with_fallback'])
@pytest.mark.parametrize('error_type', [RuntimeError, FatalDispatch])
def test_failed_dispatch_pairs_round_and_preserves_original_exception(
        monkeypatch, dispatch_boundary, stage, error_type):
    hooks, events, trace = dispatch_boundary
    failure = error_type('original dispatch failure')

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(root_loop, stage, fail)
    with pytest.raises(error_type) as raised:
        hooks.dispatch(7, [])
    assert raised.value is failure
    boundaries = [event for event in events if event['type'] in {'round_start', 'round_end'}]
    assert [(event['type'], event['roundNum']) for event in boundaries] == [
        ('round_start', 7), ('round_end', 7)]
    assert boundaries[-1]['reason'] == 'error'
    if stage == 'run_llm_call_with_fallback':
        assert trace.index(('stream_close', True, False)) < trace.index(('event', 'round_end'))


def test_unannounced_round_does_not_gain_an_invented_close(monkeypatch, dispatch_boundary):
    from lib.tasks_pkg.orchestrator import _round_open

    hooks, events, _ = dispatch_boundary
    failure = EventDeliveryError('start refused before acceptance')
    monkeypatch.setattr(_round_open, 'append_event',
                        lambda *a, **k: (_ for _ in ()).throw(failure))
    with pytest.raises(EventDeliveryError) as raised:
        hooks.dispatch(7, [])
    assert raised.value is failure and events == []


def test_failed_close_remains_a_delivery_failure_and_blocks_provider_replay(
        monkeypatch, dispatch_boundary):
    hooks, _, _ = dispatch_boundary
    provider_failure = RuntimeError('original provider failure')
    close_failure = EventDeliveryError('round close was not accepted')

    def fail_provider(*args, **kwargs):
        raise provider_failure

    def fail_close(*args, **kwargs):
        raise close_failure

    monkeypatch.setattr(root_loop, 'run_llm_call_with_fallback', fail_provider)
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event', fail_close)
    with pytest.raises(EventDeliveryError) as raised:
        hooks.dispatch(7, [])
    assert raised.value is close_failure
    assert close_failure.__context__ is provider_failure
    envelope = from_exception(close_failure)
    assert envelope['source'] == 'task-event-delivery'
    assert should_auto_retry_turn(envelope, 0) == (False, 0.0)


def test_returned_model_round_remains_owned_by_normal_completion(monkeypatch, dispatch_boundary):
    hooks, events, _ = dispatch_boundary

    def return_reply(task, state, *args, **kwargs):
        state.assistant_msg = {'role': 'assistant', 'content': 'reply'}
        state.last_finish_reason = 'stop'
        state.last_usage = {'prompt_tokens': 1, 'completion_tokens': 1}
        return 'proceed'

    monkeypatch.setattr(root_loop, 'run_llm_call_with_fallback', return_reply)
    result = hooks.dispatch(7, [])
    assert result.message['content'] == 'reply'
    assert [event['type'] for event in events if event['type'].startswith('round_')] == ['round_start']
