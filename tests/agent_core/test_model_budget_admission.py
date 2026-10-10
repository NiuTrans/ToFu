"""Finite authorization covers requests, concurrency, retries and cost truth."""
pytest_plugins = ('tests._chat_sidecar',)
import asyncio
from concurrent.futures import ThreadPoolExecutor
from collections import deque
import json
import threading
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler
from tests.support.loopback import LoopbackThreadingHTTPServer

import pytest

from lib.agent_core.execution_budget import (
    ExecutionBudget, ModelBudgetExceeded, inherit_execution_budget,
    close_execution_budget, model_budget_admission,
)
from lib.evolution.bridge import derive_actuals
from lib.llm.request import ModelRequestBudgetScope
from lib.llm_errors import AbortedError, LocalRequestPreparationError
from tests.evolution.test_evolution_runs import runtime as runtime, finish, review, OWNER



def task(limit=100, *, name='root'):
    return {'id': name, '_userId': 7, 'usage': {}, 'abort_event': threading.Event(),
            'config': {'executionLimits': {'max_tokens': limit,
                'max_tool_calls': None, 'deadline': None}}}


def reserve(account, owner, input_tokens=20, output_tokens=30):
    return account.reserve_model(owner, input_tokens=input_tokens,
                                 max_output_tokens=output_tokens)


@pytest.mark.unit
def test_prompt_and_output_reserve_before_dispatch_and_actuals_refund_once():
    owner = task(50); account = ExecutionBudget(owner)
    lease = reserve(account, owner, output_tokens=100)
    assert lease.max_output_tokens == 30
    lease.settle({'prompt_tokens': 20, 'completion_tokens': 5})
    lease.settle({'prompt_tokens': 20, 'completion_tokens': 5})
    assert account.consumption() == (25, 0)
    owner['usage'] = {'prompt_tokens': 20, 'completion_tokens': 5}
    next_lease = reserve(account, owner)
    assert next_lease.max_output_tokens == 5
    next_lease.settle({'prompt_tokens': 20, 'completion_tokens': 5})
    assert account.consumption() == (50, 0)
    with pytest.raises(ModelBudgetExceeded): reserve(account, owner)
    assert not account._model_leases


@pytest.mark.unit
def test_impossible_input_is_refused_and_local_preparation_can_void():
    owner = task(30); account = ExecutionBudget(owner)
    with pytest.raises(ModelBudgetExceeded): reserve(account, owner, input_tokens=30)
    lease = reserve(account, owner, input_tokens=10)
    lease.settle(unsent=True)
    replacement = reserve(account, owner, input_tokens=10)
    assert replacement.max_output_tokens == 20
    replacement.settle(unsent=True)
    assert account.consumption() == (0, 0)


@pytest.mark.unit
def test_child_slice_and_tree_reservations_share_one_account():
    owner = task(100)
    child = task(35, name='child')
    inherit_execution_budget(owner, child)
    child['config']['executionLimits']['max_tokens'] = 35
    lease = reserve(child['_executionBudget'], child, output_tokens=80)
    assert lease.max_output_tokens == 15
    lease.settle({'prompt_tokens': 20, 'completion_tokens': 10})
    child['usage'] = {'prompt_tokens': 20, 'completion_tokens': 10}
    with pytest.raises(ModelBudgetExceeded): reserve(child['_executionBudget'], child)
    close_execution_budget(child)
    assert derive_actuals(owner)['tokens'] == 30
    assert owner['_executionBudget']._model_tokens == {'root': 0}


@pytest.mark.unit
def test_waiting_reservation_wakes_after_refund_and_cancellation_stops_wait():
    owner = task(100); account = ExecutionBudget(owner)
    admitted = reserve(account, owner, input_tokens=40, output_tokens=50)
    entered = threading.Event()
    def waiting():
        entered.set()
        return reserve(account, owner, input_tokens=20, output_tokens=30)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(waiting); assert entered.wait(2)
        assert not future.done()
        admitted.settle({'prompt_tokens': 40, 'completion_tokens': 5})
        following = future.result(timeout=2)
        assert following.max_output_tokens == 30
        following.settle(unsent=True)
    lease = reserve(account, owner, input_tokens=40, output_tokens=10)
    cancelled = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(account.reserve_model, owner, input_tokens=40,
            max_output_tokens=10, cancelled=cancelled.is_set)
        cancelled.set()
        with pytest.raises(ModelBudgetExceeded, match='execution_owner_cancelled'):
            future.result(timeout=2)
    lease.settle(unsent=True)


@pytest.mark.unit
def test_parent_stop_propagates_through_the_required_budget_wait_port():
    from lib.tasks_pkg.manager._registry import make_provider_abort_check
    owner = task(50);child = task(name='child');inherit_execution_budget(owner, child)
    active = reserve(owner['_executionBudget'], owner, input_tokens=20, output_tokens=30)
    child['_transientRuntime'] = True
    child['_invocationAbortCheck'] = lambda: owner.get('aborted', False)
    admit = model_budget_admission(child, cancelled=make_provider_abort_check(child))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(admit, input_tokens=20, max_output_tokens=30)
        owner['aborted'] = True
        with pytest.raises(AbortedError):future.result(timeout=2)
    assert child['aborted'] and child['abort_event'].is_set()
    active.settle(unsent=True)


@pytest.mark.unit
def test_budget_refusal_is_not_relabelled_as_owner_cancellation(monkeypatch):
    from lib.tasks_pkg.orchestrator import _llm_round_call as call
    owner = task(10)
    admit = model_budget_admission(owner)
    def attempt(*args, **kwargs):admit(input_tokens=20, max_output_tokens=30)
    monkeypatch.setattr(call, '_llm_call_with_fallback', attempt)
    state = SimpleNamespace(model='gpt-4o', tool_call_happened=False,
        preset='default', thinking_enabled=False, accumulated_usage={}, api_rounds=[])
    action = call.run_llm_call_with_fallback(owner, state, {}, [], [],
        SimpleNamespace(on_tool_call_ready=None), round_num=0, tid='root', max_tokens=30)
    assert action == 'break' and state.last_finish_reason == 'error'
    assert state.exit_reason == 'execution_token_budget_exhausted'
    assert not owner.get('aborted')


@pytest.mark.unit
def test_unknown_usage_blocks_future_requests_without_inventing_actuals():
    owner = task(100)
    admission = model_budget_admission(owner)
    lease = admission(input_tokens=20, max_output_tokens=30)
    lease.settle(None)
    assert owner['_executionActualsComplete'] is False
    assert derive_actuals(owner)['tokens'] == 0
    with pytest.raises(AbortedError, match='execution_token_usage_unknown'):
        admission(input_tokens=20, max_output_tokens=30)
    assert owner['_executionLimitReason'] == 'execution_token_usage_unknown'


@pytest.mark.unit
def test_provider_overestimate_is_charged_honestly_and_stops_next_admission():
    owner = task(50); account = ExecutionBudget(owner)
    lease = reserve(account, owner)
    lease.settle({'prompt_tokens': 60, 'completion_tokens': 5})
    assert account.consumption() == (65, 0)
    with pytest.raises(ModelBudgetExceeded): reserve(account, owner)


@pytest.mark.unit
def test_late_model_settlement_after_child_close_retains_consumption_once():
    owner = task(100);child = task(name='child')
    inherit_execution_budget(owner, child)
    lease = reserve(child['_executionBudget'], child)
    close_execution_budget(child)
    lease.settle({'prompt_tokens': 20, 'completion_tokens': 5})
    lease.settle({'prompt_tokens': 20, 'completion_tokens': 5})
    assert derive_actuals(owner)['tokens'] == 25
    assert derive_actuals(child)['tokens'] == 25
    assert not owner['_executionBudget']._model_leases


@pytest.mark.unit
def test_required_scope_voids_local_failure_and_accounts_nested_scope_once():
    owner = task(100)
    body = {'_model_budget_admit': model_budget_admission(owner)}
    with pytest.raises(LocalRequestPreparationError):
        with ModelRequestBudgetScope(body) as scope:
            scope.body['_model_budget_admit'](input_tokens=20, max_output_tokens=30)
            raise LocalRequestPreparationError('local schema rejected', stage='wire_projection')
    assert owner['_executionBudget'].consumption() == (0, 0)
    with ModelRequestBudgetScope(body) as outer:
        with ModelRequestBudgetScope(outer.body) as inner:
            inner.body['_model_budget_admit'](input_tokens=20, max_output_tokens=30)
            inner.usage = {'prompt_tokens': 20, 'completion_tokens': 5}
        outer.usage = inner.usage
    assert owner['_executionBudget'].consumption() == (25, 0)




@pytest.mark.unit
def test_proven_unsent_preflight_failure_refunds_without_unknown_cost():
    from lib.llm_errors import EndpointUnreachableError
    owner = task(50)
    with pytest.raises(EndpointUnreachableError):
        with ModelRequestBudgetScope(request(owner)) as scope:
            scope.body['_model_budget_admit'](input_tokens=20, max_output_tokens=30)
            raise EndpointUnreachableError('No route admitted')
    assert owner['_executionBudget'].consumption() == (0, 0)
    assert owner.get('_executionActualsComplete') is not False
    reserve(owner['_executionBudget'], owner).settle(unsent=True)


@pytest.mark.unit
def test_sent_request_cannot_be_voided_by_an_unsent_claim():
    owner = task(50);account = ExecutionBudget(owner)
    lease = reserve(account, owner)
    lease.mark_dispatched();lease.settle(unsent=True)
    assert owner['_executionActualsComplete'] is False
    with pytest.raises(ModelBudgetExceeded, match='execution_token_usage_unknown'):
        reserve(account, owner)


@pytest.mark.unit
def test_voided_lease_cannot_authorize_a_late_physical_send():
    from lib.llm.request import dispatch_model_request
    owner = task(50);lease = reserve(ExecutionBudget(owner), owner)
    lease.settle(unsent=True)
    sent = []
    with pytest.raises(AbortedError):
        dispatch_model_request(SimpleNamespace(model_budget_lease=lease), sent.append, 'send')
    assert sent == []




@pytest.mark.unit
def test_stop_between_reservation_and_send_refunds_without_cost_uncertainty():
    owner = task(50);admit = model_budget_admission(owner)
    lease = admit(input_tokens=20, max_output_tokens=30)
    owner['aborted'] = True
    with pytest.raises(AbortedError, match='execution_owner_cancelled'):
        lease.mark_dispatched()
    lease.settle(unsent=True)
    assert owner.get('_executionActualsComplete') is not False
    assert not owner.get('_executionLimitReason')


@pytest.mark.unit
def test_provider_overrun_rechecks_already_reserved_sibling_before_send():
    owner = task(80);account = ExecutionBudget(owner)
    first = reserve(account, owner, input_tokens=20, output_tokens=20)
    second = reserve(account, owner, input_tokens=20, output_tokens=20)
    first.mark_dispatched();first.settle({'prompt_tokens': 50, 'completion_tokens': 5})
    assert account.consumption() == (55, 0)
    with pytest.raises(AbortedError, match='execution_token_budget_exhausted'):
        second.mark_dispatched()
    second.settle(unsent=True)
    assert account.consumption() == (55, 0)
    assert owner.get('_executionActualsComplete') is not False

@pytest.mark.unit
def test_scope_closed_during_async_preparation_refuses_late_admission():
    owner = task(50)
    with ModelRequestBudgetScope(request(owner)) as scope:
        admit = scope.body['_model_budget_admit']
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(admit, input_tokens=20, max_output_tokens=30)
        with pytest.raises(AbortedError, match='scope closed'):
            future.result(timeout=2)
    assert not owner['_executionBudget']._model_leases
    assert owner.get('_executionActualsComplete') is not False


@pytest.mark.unit
def test_delegated_attempt_settles_after_its_async_caller_exits():
    owner = task(50);entered = threading.Event();release = threading.Event()
    def delegated(body):
        with ModelRequestBudgetScope(body) as inner:
            lease = inner.body['_model_budget_admit'](input_tokens=20, max_output_tokens=30)
            lease.mark_dispatched();entered.set();assert release.wait(2)
            inner.usage = {'prompt_tokens': 20, 'completion_tokens': 5}
    with ThreadPoolExecutor(max_workers=1) as pool:
        with ModelRequestBudgetScope(request(owner)) as outer:
            future = pool.submit(delegated, outer.body)
            assert entered.wait(2)
        assert owner.get('_executionActualsComplete') is not False
        assert len(owner['_executionBudget']._model_leases) == 1
        release.set();future.result(timeout=2)
    assert derive_actuals(owner)['tokens'] == 25
    assert not owner['_executionBudget']._model_leases

@pytest.mark.unit
def test_unbounded_tasks_have_no_request_reservation_port():
    assert model_budget_admission(task(None)) is None
    assert model_budget_admission({'config': {}}) is None


@pytest.mark.unit
def test_unbounded_evolution_retains_unknown_cost_without_stopping_work():
    owner = task(None);owner['config']['evolution'] = {'run_id': 'run-one'}
    admit = model_budget_admission(owner)
    first = admit(input_tokens=20, max_output_tokens=30);first.settle(None)
    second = admit(input_tokens=20, max_output_tokens=30)
    second.settle({'prompt_tokens': 20, 'completion_tokens': 5})
    assert owner['_executionActualsComplete'] is False
    assert derive_actuals(owner)['tokens'] == 25


@pytest.mark.unit
def test_legacy_evolution_without_limits_can_track_a_server_default_output():
    owner = task(None);owner['config'] = {'evolution': {'episode_id': 'episode-one'}}
    admission = model_budget_admission(owner)
    lease = admission(input_tokens=20, max_output_tokens=0)
    lease.settle({'prompt_tokens': 20, 'completion_tokens': 5})
    assert derive_actuals(owner)['tokens'] == 25


@pytest.mark.parametrize(('usage', 'total', 'complete'), [
    ({'input_tokens': 100, 'output_tokens': 5, 'cache_read_input_tokens': 60}, 165, True),
    ({'prompt_tokens': 100, 'completion_tokens': 5, 'cached_tokens': 60}, 105, True),
    ({'completion_tokens': 5, 'cached_tokens': 60}, 65, False),
    ({'prompt_tokens': -10, 'completion_tokens': 5}, 5, False),
    ({'prompt_tokens': float('inf'), 'completion_tokens': 5}, 5, False),
    ({'input_tokens': 10, 'output_tokens': 5, 'cache_read_input_tokens': 'bad'}, 15, False),
    ({'prompt_tokens': 0, 'completion_tokens': 0}, 0, True),
    ({'prompt_tokens': 0, 'completion_tokens': 5, 'cached_tokens': 60}, 65, False),
    ({'prompt_tokens': 100, 'completion_tokens': 5, 'cached_tokens': None}, 105, True),
])
@pytest.mark.unit
def test_usage_observation_preserves_cache_conventions_and_unknown_counters(usage, total, complete):
    from lib.cost import observed_usage_totals
    assert observed_usage_totals(usage) == (total, complete)


@pytest.fixture
def provider(monkeypatch):
    """Real loopback SSE transport; only token counting/upstream are synthetic."""
    import lib.token_counter
    monkeypatch.setattr(lib.token_counter, 'count_tokens', lambda *a, **k: {'tokens': 20})
    monkeypatch.setattr('lib.tasks_pkg.compaction._tokens.resolve_model_context_limit',
                        lambda *a, **k: 100000)
    bodies = []
    options = {'usage': True, 'failure': False, 'hold': False,
               'release': threading.Event(), 'three_started': threading.Event()}
    lock = threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *args): pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            with lock:
                bodies.append(body)
                if len(bodies) == 3: options['three_started'].set()
            if options['hold']: assert options['release'].wait(5)
            if options['failure']:
                # Headers prove a request attempt, then the peer closes before
                # delivering its declared body or a usage receipt.
                self.send_response(200);self.send_header('Content-Length', '100')
                self.end_headers();self.close_connection = True;return
            output = min(5, body.get('max_tokens', body.get('max_output_tokens', 5)))
            final = {'choices': [{'delta': {}, 'finish_reason': 'stop'}]}
            if options['usage']: final['usage'] = {'prompt_tokens': 20, 'completion_tokens': output}
            payload = (b'data: {"choices":[{"delta":{"content":"observed"}}]}\n\n'
                + json.dumps(final).encode().join(
                        (b'data: ', b'\n\n')) + b'data: [DONE]\n\n')
            if 'input' in body:
                result = {'type': 'response.completed', 'response': {
                    'id': 'observed-response', 'status': 'completed', 'output': [
                        {'type': 'message', 'role': 'assistant', 'content': [
                            {'type': 'output_text', 'text': 'observed'}]}]}}
                if options['usage']:
                    result['response']['usage'] = {'input_tokens': 20, 'output_tokens': output}
                payload = (b'data: ' + json.dumps(result).encode() + b'\n\n')
            self.send_response(200);self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(payload)));self.end_headers()
            self.wfile.write(payload)
    server = LoopbackThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True);worker.start()
    yield f'http://127.0.0.1:{server.server_port}/v1', bodies, options
    options['release'].set()
    from lib.llm._transport import reset_pools_for_test
    reset_pools_for_test();server.shutdown();server.server_close();worker.join(timeout=2)


def request(owner):
    return {'model': 'gpt-4o', 'messages': [{'role': 'user', 'content': 'observe'}],
            'max_tokens': 100, 'stream': True,
            '_model_budget_admit': model_budget_admission(owner)}


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.api
def test_real_transport_caps_output_and_blocks_an_impossible_following_prompt(provider, asynchronous):
    from lib.llm.stream import stream_chat
    from lib.llm.astream import async_stream_chat
    url, bodies, _ = provider;owner = task(50)
    def call():
        if asynchronous:
            return asyncio.run(async_stream_chat(request(owner), base_url=url, owner_user_id=7))
        return stream_chat(request(owner), base_url=url, owner_user_id=7)
    assert call().is_verified_complete
    assert call().is_verified_complete
    with pytest.raises(AbortedError): call()
    assert len(bodies) == 2 and [row['max_tokens'] for row in bodies] == [30, 5]
    assert all(not any(k.startswith('_') for k in body) for body in bodies)
    assert derive_actuals(owner)['tokens'] == 50



@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.api
def test_real_transport_routing_failure_before_send_refunds_and_can_retry(provider, monkeypatch, asynchronous):
    from lib.desktop import egress
    from lib.llm import stream, astream
    from lib.llm_errors import EndpointUnreachableError
    url, bodies, _ = provider;owner = task(50)
    original = egress.route_request
    def refuse(*args, **kwargs):raise egress.EgressUnavailable('Route not available')
    monkeypatch.setattr(egress, 'route_request', refuse)
    def call():
        if asynchronous:
            return asyncio.run(astream._async_stream_chat_once(request(owner), base_url=url, owner_user_id=7))
        return stream._stream_chat_once(request(owner), base_url=url, owner_user_id=7)
    with pytest.raises(EndpointUnreachableError):call()
    assert bodies == [] and owner.get('_executionActualsComplete') is not False
    assert not owner['_executionBudget']._model_leases
    monkeypatch.setattr(egress, 'route_request', original)
    assert call().is_verified_complete
    assert len(bodies) == 1 and derive_actuals(owner)['tokens'] == 25


@pytest.mark.api
def test_websocket_pre_send_failure_reuses_one_lease_for_real_sse(provider, monkeypatch):
    from lib.llm import stream, responses_websocket as ws
    url, bodies, _ = provider;owner = task(50)
    def unavailable(*args):raise ws.ResponsesWebSocketUnavailable('Socket unavailable before create')
    monkeypatch.setattr(ws, '_open_session', unavailable);ws._sessions().clear()
    body = request(owner)
    body.update({'model': 'gpt-5.6-sol', '_task_id': 'budget-websocket', '_responses_feature_profile': 'openai',
                 '_responses_transport': 'websocket'})
    result = stream._stream_chat_once(body, base_url=url, api_protocol='responses', owner_user_id=7)
    assert result.is_verified_complete
    assert len(bodies) == 1 and bodies[0]['max_output_tokens'] == 30
    assert derive_actuals(owner)['tokens'] == 25
    assert not owner['_executionBudget']._model_leases


@pytest.mark.api
def test_websocket_failed_send_is_ambiguous_and_not_replayed_on_sse(provider, monkeypatch):
    from lib.llm import stream, responses_websocket as ws
    url, bodies, _ = provider;owner = task(50);sent = []
    class Connection:
        def send(self, payload):sent.append(payload);raise OSError('Socket dropped during send')
        def close(self):pass
    def open_session(plan, key):
        session = ws._Session(connection=Connection());ws._sessions()[key] = session;return session
    monkeypatch.setattr(ws, '_open_session', open_session);ws._sessions().clear()
    body = request(owner)
    body.update({'model': 'gpt-5.6-sol', '_task_id': 'budget-websocket', '_responses_feature_profile': 'openai',
                 '_responses_transport': 'websocket'})
    from lib.llm_errors import RetryableAPIError
    with pytest.raises(RetryableAPIError, match='WebSocket failed'):
        stream._stream_chat_once(body, base_url=url, api_protocol='responses', owner_user_id=7)
    assert len(sent) == 1 and bodies == []
    assert owner['_executionActualsComplete'] is False
    with pytest.raises(AbortedError, match='execution_token_usage_unknown'):
        model_budget_admission(owner)(input_tokens=20, max_output_tokens=30)


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('adapter_route', [False, True])
@pytest.mark.api
def test_desktop_handoffs_send_one_capped_request_and_settle_once(provider, monkeypatch, asynchronous, adapter_route):
    import requests
    from lib.desktop import adapter, egress
    from lib.llm import stream, astream
    url, bodies, _ = provider;owner = task(50)
    def relay(*args, headers, body, **kwargs):
        return requests.post(url + '/chat/completions', headers=headers,
                             data=body, stream=True, timeout=3)
    if adapter_route:
        monkeypatch.setattr(adapter, 'relay_stream', relay)
        options = {'adapter': {'agent_id': 'budget-agent', 'port': 8317}}
    else:
        monkeypatch.setattr(egress, 'route_request', lambda *a, **k: 'budget-agent')
        monkeypatch.setattr(egress, 'open_stream', relay)
        options = {}
    def call():
        kwargs = {'base_url': url, 'owner_user_id': 7, **options}
        if asynchronous:
            return asyncio.run(astream._async_stream_chat_once(request(owner), **kwargs))
        return stream._stream_chat_once(request(owner), **kwargs)
    assert call().is_verified_complete and call().is_verified_complete
    assert len(bodies) == 2 and [body['max_tokens'] for body in bodies] == [30, 5]
    assert derive_actuals(owner)['tokens'] == 50
    assert not owner['_executionBudget']._model_leases

@pytest.mark.api
def test_parallel_children_wait_for_live_allowances_before_a_fourth_http_request(provider):
    from lib.llm.stream import stream_chat
    url, bodies, options = provider;owner = task(100);options['hold'] = True
    children = [task(name=f'child-{index}') for index in range(4)]
    for child in children: inherit_execution_budget(owner, child)
    def run(child):
        body = request(child);body['max_tokens'] = 10
        try:return stream_chat(body, base_url=url, owner_user_id=7)
        finally:close_execution_budget(child)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(run, child) for child in children]
        try:
            assert options['three_started'].wait(3)
            assert len(bodies) == 3
        finally:options['release'].set()
        assert all(future.result(timeout=3).is_verified_complete for future in futures)
    assert len(bodies) == 4 and derive_actuals(owner)['tokens'] == 100
    assert sorted(body['max_tokens'] for body in bodies) == [5, 10, 10, 10]
    assert not owner['_executionBudget']._model_leases


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.api
def test_real_transport_missing_usage_fences_subsequent_dispatch(provider, asynchronous):
    from lib.llm.stream import stream_chat
    from lib.llm.astream import async_stream_chat
    url, bodies, options = provider;options['usage'] = False;owner = task(50)
    def call():
        if asynchronous:return asyncio.run(async_stream_chat(request(owner), base_url=url, owner_user_id=7))
        return stream_chat(request(owner), base_url=url, owner_user_id=7)
    assert call().is_verified_complete
    with pytest.raises(AbortedError, match='execution_token_usage_unknown'):call()
    assert len(bodies) == 1 and owner['_executionActualsComplete'] is False
    assert derive_actuals(owner)['tokens'] == 0


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.api
def test_unobserved_failed_request_cannot_spend_again_during_transport_retry(provider, monkeypatch, asynchronous):
    from lib.llm import stream, astream
    url, bodies, options = provider;options['failure'] = True;owner = task(50)
    monkeypatch.setattr(stream, 'prepare_retryable_wait', lambda *a, **k: 0)
    monkeypatch.setattr(astream, 'prepare_retryable_wait', lambda *a, **k: 0)
    with pytest.raises(AbortedError, match='execution_token_usage_unknown'):
        if asynchronous:asyncio.run(astream.async_stream_chat(request(owner), base_url=url, owner_user_id=7))
        else:stream.stream_chat(request(owner), base_url=url, owner_user_id=7)
    assert len(bodies) == 1 and owner['_executionActualsComplete'] is False


@pytest.mark.parametrize('protocol', ['openai', 'responses', 'anthropic'])
@pytest.mark.unit
def test_sealed_wire_has_protocol_output_cap_and_no_policy_callbacks(monkeypatch, protocol):
    from lib.llm._sse_core import prepare_request
    import lib.token_counter
    monkeypatch.setattr(lib.token_counter, 'count_tokens', lambda *a, **k: {'tokens': 20})
    monkeypatch.setattr('lib.tasks_pkg.compaction._tokens.resolve_model_context_limit',
                        lambda *a, **k: 100000)
    owner = task(50)
    with ModelRequestBudgetScope(request(owner)) as scope:
        plan = prepare_request(scope.body, base_url='https://example.invalid/v1',
                               api_protocol=protocol)
        wire = plan.body.wire_body()
        key = 'max_output_tokens' if protocol == 'responses' else 'max_tokens'
        assert wire[key] == 50 - plan.body.input_tokens
        assert not any(k.startswith('_') for k in wire)
        plan.model_budget_lease.settle(unsent=True)


@pytest.mark.unit
def test_incomplete_stage_usage_survives_run_episode_settlement(runtime):
    from lib.evolution import get_episode
    runs, _, tasks = runtime
    executing = runs.submit_run({'objective': 'Audit model spending receipts.'}, user_id=OWNER)
    tasks[0]['_executionActualsComplete'] = False
    reviewing = finish(runtime, tasks[0], 'Observed evidence with a missing provider cost receipt.')
    completed = finish(runtime, tasks[-1], review())
    assert completed['status'] == 'completed' and completed['actuals_complete'] is False
    assert completed['receipts'][0]['actuals_complete'] is False
    assert get_episode(executing['episode_id'], user_id=OWNER)['actuals_complete'] is False


@pytest.mark.unit
def test_generic_episode_automation_keeps_incomplete_cost_in_its_compact_queue(runtime, monkeypatch):
    from lib.evolution import automation, get_episode
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'Measure model usage.'}, user_id=OWNER)
    task = tasks[0];task['status'] = 'aborted';task['_executionActualsComplete'] = False
    task['config']['evolution'] = {'goal_id': run['goal_id'], 'plan_id': run['plan_id'],
                                  'episode_id': run['episode_id']}
    # The expensive worker launch is paused; real queue compaction/settlement run.
    monkeypatch.setattr(automation, '_pending', deque())
    monkeypatch.setattr(automation, '_worker', SimpleNamespace(is_alive=lambda: True))
    monkeypatch.setattr(automation, '_worker_stop', threading.Event())
    monkeypatch.setattr(automation, '_queue_capacity', 10)
    assert automation.enqueue_task_terminal(task)
    receipt, actuals = automation._pending.popleft()
    assert receipt['_executionActualsComplete'] is False
    automation.on_task_terminal(receipt, actuals=actuals)
    assert get_episode(run['episode_id'], user_id=OWNER)['actuals_complete'] is False


@pytest.mark.unit
def test_later_known_repair_episode_does_not_inherit_prior_incomplete_receipt(runtime):
    from lib.evolution import get_episode
    runs, _, tasks = runtime
    runs.submit_run({'objective': 'Audit model spending receipts.', 'max_attempts': 2}, user_id=OWNER)
    tasks[0]['_executionActualsComplete'] = False
    finish(runtime, tasks[0], 'Observed evidence with incomplete cost.')
    repaired = finish(runtime, tasks[-1], review('fail'))
    finish(runtime, tasks[-1], 'Repair with complete cost receipts.')
    completed = finish(runtime, tasks[-1], review())
    assert completed['actuals_complete'] is False
    assert get_episode(repaired['episode_id'], user_id=OWNER).get('actuals_complete', True)


@pytest.mark.unit
def test_finite_run_refuses_review_with_the_precise_unknown_usage_cause(runtime):
    from lib.evolution import get_episode
    runs, _, tasks = runtime
    opened = runs.submit_run({'objective': 'Validate spending before a review.',
        'budget': {'max_tokens': 200}}, user_id=OWNER)
    tasks[0]['_executionActualsComplete'] = False
    stopped = finish(runtime, tasks[0], 'Inspected evidence with an incomplete usage receipt.')
    assert stopped['status'] == 'needs_attention'
    assert stopped['error'] == 'execution_token_usage_unknown'
    assert stopped['actuals']['tokens'] < 200 and stopped['actuals_complete'] is False
    assert len(tasks) == 1 and stopped['review'] is None
    assert get_episode(opened['episode_id'], user_id=OWNER)['actuals_complete'] is False
    assert runs.advance_run(opened['run_id'], user_id=OWNER) == stopped
