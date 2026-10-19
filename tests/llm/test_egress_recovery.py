"""Pre-send recovery preserves request/credential health and cancellation."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lib.desktop import egress
from lib.llm import egress_recovery as recovery
from lib.llm.llm_errors import AbortedError, RequestTransportError

pytestmark = pytest.mark.unit
URL = 'https://chatgpt.com/backend-api/codex/responses'


@pytest.fixture
def clock(monkeypatch):
    import lib.llm._transport as transport
    now = [0.0]
    monkeypatch.setattr(recovery, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    def sleep(seconds, abort_check):
        if abort_check and abort_check():
            raise AbortedError('cancelled')
        now[0] += seconds
    async def asleep(seconds, abort_check):
        sleep(seconds, abort_check)
        await asyncio.sleep(0)
    monkeypatch.setattr(transport, 'abortable_sleep', sleep)
    monkeypatch.setattr(transport, 'async_abortable_sleep', asleep)
    return now


@pytest.mark.parametrize('asynchronous', [False, True])
def test_recovery_is_waiting_not_a_provider_failure(monkeypatch, clock, asynchronous):
    from lib.llm_dispatch import api
    from tests.llm_dispatch.test_dispatch_stream import _FakeDispatcher, _make_slot
    import lib.llm as llm

    slot = _make_slot(model='gpt-6-astra')
    dispatcher = _FakeDispatcher([slot])
    monkeypatch.setattr(api, 'get_dispatcher', lambda: dispatcher)
    route = Mock(side_effect=[egress.EgressRecoveryPending(5.7),
                              egress.EgressRecoveryPending(4.7), 'direct'])
    monkeypatch.setattr(egress, 'route_request', route)
    retries, attempts = [], []

    def stream(body, **kwargs):
        assert recovery.resolve_egress_route(
            egress, URL, user_id='7', on_wait=kwargs['on_stream_wait']) == 'direct'
        return {'content': 'recovered'}, 'stop', {}

    async def astream(body, **kwargs):
        assert await recovery.async_resolve_egress_route(
            egress, URL, user_id='7', on_wait=kwargs['on_stream_wait']) == 'direct'
        return {'content': 'recovered'}, 'stop', {}

    monkeypatch.setattr(llm, 'stream_chat', stream)
    from lib.llm import astream as async_transport
    monkeypatch.setattr(async_transport, 'async_stream_chat', astream)
    args = dict(prefer_model=slot.model, strict_model=True, max_retries=1,
                on_retry=lambda **event: retries.append(event),
                on_provider_attempt=lambda **event: attempts.append(event))
    if asynchronous:
        result = asyncio.run(api.async_dispatch_stream([{'role': 'user', 'content': 'hi'}], **args))
    else:
        result = api.dispatch_stream([{'role': 'user', 'content': 'hi'}], **args)
    assert result[0]['content'] == 'recovered'
    assert [e['reason'] for e in retries] == [
        'Waiting for network recovery', 'Waiting for network recovery', 'Network recovered']
    assert [e.get('status') for e in attempts] == [None, 'succeeded']
    assert slot.inflight == slot.total_errors == slot.consecutive_errors == 0
    assert slot.cooldown_until == 0
    assert dispatcher.picks == 1
    assert clock[0] == 2.0  # Shared recovery wins before the original 5.7s deadline.
    assert all(c.kwargs['user_id'] == '7' for c in route.call_args_list)


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('outcome', ['cancel', 'timeout', 'policy'])
def test_unsent_wait_stops_and_preserves_failure_kind(monkeypatch, clock, asynchronous, outcome):
    def route(*args, **kwargs):
        if outcome == 'policy':
            raise egress.EgressUnavailable('No authorized desktop route')
        raise egress.EgressRecoveryPending(10)
    monkeypatch.setattr(egress, 'route_request', route)
    abort = lambda: outcome == 'cancel' and clock[0] >= 1
    error = {'cancel': AbortedError, 'timeout': RequestTransportError,
             'policy': egress.EgressUnavailable}[outcome]
    kwargs = dict(user_id='7', abort_check=abort, timeout_s=2.5)
    with pytest.raises(error):
        if asynchronous:
            asyncio.run(recovery.async_resolve_egress_route(egress, URL, **kwargs))
        else:
            recovery.resolve_egress_route(egress, URL, **kwargs)
    assert clock[0] == {'cancel': 1, 'timeout': 2.5, 'policy': 0}[outcome]


def test_observer_failure_cannot_break_recovery(monkeypatch, clock):
    monkeypatch.setattr(egress, 'route_request', Mock(side_effect=[
        egress.EgressRecoveryPending(0.2), 'agent-owned-by-7']))
    assert recovery.resolve_egress_route(egress, URL, user_id='7',
        on_wait=Mock(side_effect=RuntimeError('observer'))) == 'agent-owned-by-7'


def test_egress_prefers_authorized_desktop_before_waiting(monkeypatch):
    monkeypatch.setattr(egress, '_probe_host', lambda *a, **k: 'network_fail')
    monkeypatch.setattr(egress, '_online_egress_agents', lambda owner: [
        {'agent_id': 'desktop-7'}] if owner == '7' else [])
    monkeypatch.setattr(egress, '_order_agents', lambda agents, owner: agents)
    monkeypatch.setattr('lib.proxy.subscription_route_recovery_delay', lambda url: 5.7)
    assert egress.route_request(URL, user_id='7') == 'desktop-7'
    with pytest.raises(egress.EgressRecoveryPending) as caught:
        egress.route_request(URL, user_id='8')
    assert caught.value.retry_after_s == 5.7


def test_network_wait_is_localized_and_has_no_attempt_counter():
    from lib.llm_dispatch.retry_i18n import retry_phase_fields, RetryPhaseEventBudget
    from lib.agent_core.events import validate_event
    fields = retry_phase_fields(model='gpt-6-astra', attempt=0,
                                reason='Waiting for network recovery')
    assert fields['detailKey'] == 'stream.phase.retryCooldownWait'
    assert fields['detailArgs']['reasonKey'] == 'stream.retryReason.networkRecovery'
    assert 'attempt' not in fields['detailArgs']
    event = dict(type='swarm_agent_phase', agentId='worker', phase='retrying',
                 content=fields.pop('detail'), **fields)
    assert not validate_event(event)
    budget = RetryPhaseEventBudget()
    assert sum(budget.should_emit(('network_recovery',)) for _ in range(100000)) <= 16


def test_async_cancel_joins_route_wait_without_cancelling_shared_probe(monkeypatch):
    import threading
    entered, exited = threading.Event(), threading.Event()
    def route(*args, cancelled=None, **kwargs):
        entered.set()
        while not cancelled():
            exited.wait(0.005)
        exited.set()
        raise egress.EgressRecoveryPending(5)
    monkeypatch.setattr(egress, 'route_request', route)
    async def run():
        task = asyncio.create_task(recovery.async_resolve_egress_route(egress, URL, user_id='7'))
        while not entered.is_set():
            await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert exited.is_set()
    asyncio.run(run())


def test_async_prepared_request_closes_on_cancelled_route_wait(monkeypatch, clock):
    from lib.llm import astream
    dumper = SimpleNamespace(enabled=True, _fh=object(), finish=Mock())
    lease = Mock()
    plan = SimpleNamespace(url=URL, raw_dumper=dumper,
                           model_budget_lease=lease, raw_archive_capture=None)
    monkeypatch.setattr(astream, 'prepare_request', lambda *a, **kw: plan)
    monkeypatch.setattr(egress, 'route_request', Mock(side_effect=egress.EgressRecoveryPending(5)))
    with pytest.raises(AbortedError):
        asyncio.run(astream._async_stream_chat_once(
            {'model': 'gpt-test', 'messages': []}, abort_check=lambda: clock[0] >= 1))
    dumper.finish.assert_called_once_with(error=True)
    lease.settle.assert_called_once_with(unsent=True)


def test_sampled_network_wait_always_has_a_matching_recovery_within_budget():
    from lib.llm_dispatch.retry_i18n import RetryPhaseEventBudget
    budget = RetryPhaseEventBudget()
    signature = ('dispatch_retry', 'Waiting for network recovery', 0)
    emitted = 0
    for cycle in range(200):
        visible = False
        for _ in range(cycle % 7 + 1):
            shown = budget.should_emit_wait(signature)
            emitted += shown
            visible |= shown
        completed = budget.complete_wait(signature)
        assert completed is visible
        emitted += completed
    assert emitted <= budget.MAX_EVENTS_PER_SIGNATURE
