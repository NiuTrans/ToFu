"""A completed provider attempt never re-enters dispatch's transport retries.

Public sync/async entrypoints use real slots. Faults at receipt, cache and
notification boundaries must preserve another request's reservation and the
provider's already-known outcome.
"""
import asyncio

import pytest

from lib.llm.llm_errors import AbortedError
from lib.llm.stream_result import ProviderStreamResult, ProviderStreamState
from tests.llm_dispatch.test_async_dispatch_stream import _FakeDispatcher, _make_slot

pytestmark = pytest.mark.unit
MESSAGES = [{'role': 'user', 'content': 'hello'}]
CONTROL_ERRORS = [RuntimeError, asyncio.CancelledError, KeyboardInterrupt, AbortedError]


@pytest.fixture(params=['sync', 'async'])
def dispatch_case(request, monkeypatch):
    from lib.llm_dispatch import api

    slot, unused = _make_slot(), _make_slot(key='unused')
    dispatcher = _FakeDispatcher([slot, unused])
    monkeypatch.setattr(api, 'get_dispatcher', lambda: dispatcher)
    slot.record_request()  # Independently owned work remains active throughout.

    def run(result, **kwargs):
        if request.param == 'sync':
            monkeypatch.setattr('lib.llm.stream_chat', lambda *a, **kw: result)
            return api.dispatch_stream(MESSAGES, max_retries=2, **kwargs)

        async def transport(*args, **kwargs):
            return result

        monkeypatch.setattr('lib.llm.astream.async_stream_chat', transport)
        return asyncio.run(api.async_dispatch_stream(MESSAGES, max_retries=2, **kwargs))

    yield slot, unused, dispatcher, run
    slot.release()


def _result(state=ProviderStreamState.PROVIDER_FINISHED):
    return ProviderStreamResult(
        message={'role': 'assistant', 'content': 'observed content'},
        compatibility_finish_reason='stop', usage={'completion_tokens': 3}, state=state,
        saw_finish_reason=state is ProviderStreamState.PROVIDER_FINISHED,
        provider_finish_reason=('stop' if state is ProviderStreamState.PROVIDER_FINISHED else None),
    )


@pytest.mark.parametrize('state', [
    ProviderStreamState.PROVIDER_FINISHED,
    ProviderStreamState.CLIENT_ABORTED,
    ProviderStreamState.PREMATURE_CLOSE,
])
@pytest.mark.parametrize('phase', ['receipt', 'route_snapshot'])
@pytest.mark.parametrize('exception_type', CONTROL_ERRORS)
def test_result_diagnostic_failure_keeps_the_settled_outcome(
        dispatch_case, monkeypatch, state, phase, exception_type):
    slot, unused, dispatcher, run = dispatch_case
    error = exception_type('controlled result diagnostic failure')

    def fail(*args, **kwargs):
        raise error

    target = ('lib.llm_dispatch._api_stream_state.secret_hint' if phase == 'receipt'
              else 'lib.llm_dispatch._route_trace.RouteAttemptTrace.snapshot')
    monkeypatch.setattr(target, fail)
    with pytest.raises(exception_type) as caught:
        run(_result(state))
    assert caught.value is error
    assert slot.inflight == 1, 'only this attempt returns its reservation'
    assert slot.total_errors == int(state is ProviderStreamState.PREMATURE_CLOSE)
    assert bool(slot.last_success_time) == (state is ProviderStreamState.PROVIDER_FINISHED)
    assert dispatcher.picks == 1 and unused.total_requests == 0


@pytest.mark.parametrize('phase', ['contention_recovery', 'cache_write'])
@pytest.mark.parametrize('exception_type', CONTROL_ERRORS)
def test_success_bookkeeping_failure_does_not_retry_or_release_again(
        dispatch_case, monkeypatch, phase, exception_type):
    slot, unused, dispatcher, run = dispatch_case
    error = exception_type('controlled success bookkeeping failure')

    def fail(*args, **kwargs):
        raise error

    if phase == 'contention_recovery':
        monkeypatch.setattr(
            'lib.llm_dispatch._api_stream_state._note_shared_contention_recovered', fail)
    else:
        monkeypatch.setattr('lib.llm_dispatch.conv_affinity.get_conv_affinity', lambda: 'owned-conversation')
        monkeypatch.setattr('lib.llm_dispatch.cache_settle.record_stream_end', fail)
    with pytest.raises(exception_type) as caught:
        run(_result())
    assert caught.value is error
    assert slot.inflight == 1
    assert slot.total_errors == 0 and slot.last_success_time > 0
    assert dispatcher.picks == 1 and unused.total_requests == 0


@pytest.mark.parametrize('exception_type', CONTROL_ERRORS)
def test_network_error_retry_notification_failure_settles_only_once(
        dispatch_case, exception_type):
    slot, unused, dispatcher, run = dispatch_case
    error = exception_type('controlled retry notification failure')
    notices = []

    def fail(**kwargs):
        notices.append(kwargs)
        raise error

    result = ({'role': 'assistant', 'content': ''}, 'network_error', {})
    with pytest.raises(exception_type) as caught:
        run(result, on_retry=fail)
    assert caught.value is error
    assert slot.inflight == 1
    assert slot.total_errors == 1 and slot.last_success_time == 0
    assert len(notices) == 1
    assert notices[0]['attempt'] == 1
    assert notices[0]['reason'] == 'Provider network error'
    assert dispatcher.picks == 1 and unused.total_requests == 0


@pytest.mark.parametrize('phase', ['cache_wait', 'admission'])
@pytest.mark.parametrize('exception_type', CONTROL_ERRORS)
def test_local_presend_failure_has_no_provider_health_effect(
        dispatch_case, monkeypatch, phase, exception_type):
    slot, unused, dispatcher, run = dispatch_case
    error = exception_type('controlled local failure')
    attempts = []

    def fail(*args, **kwargs):
        raise error

    async def async_fail(*args, **kwargs):
        raise error

    if phase == 'cache_wait':
        monkeypatch.setattr('lib.llm_dispatch.cache_settle.settle_before_send', fail)
        monkeypatch.setattr('lib.llm_dispatch.cache_settle.async_settle_before_send', async_fail)
    else:
        monkeypatch.setattr('lib.llm_dispatch._api_stream._admit_or_defer_shared_contention', fail)
        monkeypatch.setattr('lib.llm_dispatch._api_stream._admit_or_defer_shared_contention_async', async_fail)
    with pytest.raises(exception_type) as caught:
        run(_result(), on_provider_attempt=lambda **kwargs: attempts.append(kwargs))
    assert caught.value is error
    assert slot.inflight == 1
    assert slot.total_errors == 0 and slot.last_success_time == 0
    assert attempts == []
    assert dispatcher.picks == 1 and unused.total_requests == 0


@pytest.mark.parametrize('exception_type', [asyncio.CancelledError, KeyboardInterrupt])
def test_control_signal_inside_health_observation_cannot_release_again(
        dispatch_case, monkeypatch, exception_type):
    slot, unused, dispatcher, run = dispatch_case
    error = exception_type('controlled key outcome observation failure')

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr('lib.key_stats.record_outcome', fail)
    with pytest.raises(exception_type) as caught:
        run(_result())
    assert caught.value is error
    assert slot.inflight == 1
    assert slot.total_errors == 0 and slot.last_success_time > 0
    assert dispatcher.picks == 1 and unused.total_requests == 0


@pytest.mark.parametrize('output_tokens', [float('inf'), float('-inf'), 'not a count'])
def test_unusable_token_estimate_does_not_lose_completed_response(dispatch_case, output_tokens):
    slot, unused, dispatcher, run = dispatch_case
    result = _result()
    result.usage['completion_tokens'] = output_tokens
    assert run(result) is result
    assert slot.inflight == 1
    assert slot.total_errors == 0 and slot.last_success_time > 0
    assert slot.throughput_ema == 0
    assert dispatcher.picks == 1 and unused.total_requests == 0
