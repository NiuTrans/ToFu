"""Client-owned tools traverse real auth/routing/dispatch and never the agent loop.

Only the expensive provider transport is replaced. Synthetic model facts are
installed through the same registration and owner repository boundary as other
routing tests; no real credential or external request is used.
"""
from copy import deepcopy
import json

import pytest
from quart import Quart

from lib.llm.stream_result import ProviderStreamResult
from tests.support.model_routing import (
    allow_native_test_endpoint, clear_test_model_routing,
    install_native_test_model_route,
)
from tests.storage import test_storage_application_contract as storage_contract

pytestmark = [pytest.mark.api, pytest.mark.anyio]
OWNER = 79001
application_storage = storage_contract.application_storage


@pytest.fixture
def application_storage_owner_id(request):
    # Each paid case creates the first account in its own authority (owner 2).
    return 2 if {'failure', 'billing_case'} & request.node.callspec.params.keys() else OWNER


@pytest.fixture
def client_surface(application_storage, application_storage_owner_id):
    from lib.api_keys import create_key
    from routes.api_v1.auth import bearer_auth_before_request
    from routes.compat_openai import compat_openai_bp

    app = Quart(__name__, static_folder=None)
    app.config.update(TESTING=True, PROVIDE_AUTOMATIC_OPTIONS=True)
    app.before_request(bearer_auth_before_request)
    app.register_blueprint(compat_openai_bp)
    owner = application_storage_owner_id
    _, token = create_key(owner_user_id=owner, name='external-tools', scopes=['chat'])
    with allow_native_test_endpoint():
        install_native_test_model_route(owner_user_id=owner)
        try:
            yield app.test_client(), {'Authorization': f'Bearer {token}'}
        finally:
            clear_test_model_routing(owner_user_id=owner)


def request_body(*, stream=False, tools=None):
    return {'model': 'stub-model', 'tofu_creator_id': 'tofu-test',
            'messages': [{'role': 'user', 'content': 'Call the declared client tool.'}],
            'tools': tools if tools is not None else [], 'stream': stream,
            'max_tokens': 128, 'timeout_s': 10}


def frames(text):
    return [json.loads(line[6:]) for line in text.splitlines()
            if line.startswith('data: ') and line != 'data: [DONE]']


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('name', ['write_file', 'external_probe'])
async def test_client_call_and_result_survive_without_server_execution(
        client_surface, monkeypatch, tmp_path, stream, name):
    from lib.llm import astream

    path = tmp_path / 'must-be-created-only-by-client.txt'
    arguments = json.dumps({'path': str(path), 'content': '引号 " and newline\n'}, ensure_ascii=False)
    call = {'id': 'call-client-1', 'type': 'function',
            'function': {'name': name, 'arguments': arguments}}
    tool = {'type': 'function', 'function': {'name': name,
            'parameters': {'type': 'object', 'properties': {
                'path': {'type': 'string'}, 'content': {'type': 'string'}},
                'required': ['path', 'content']}}}
    observed = []

    async def transport(body, **kwargs):
        observed.append(deepcopy(body))
        if len(observed) == 1:
            return ProviderStreamResult.from_legacy(
                {'role': 'assistant', 'content': None, 'tool_calls': [call]},
                'tool_calls', {'prompt_tokens': 11, 'completion_tokens': 7,
                               'prompt_tokens_details': {'cached_tokens': 3}})
        kwargs['on_content']('client result accepted')
        return ProviderStreamResult.from_legacy(
            {'role': 'assistant', 'content': 'client result accepted'}, 'stop',
            {'prompt_tokens': 22, 'completion_tokens': 4, 'total_tokens': 26})

    monkeypatch.setattr(astream, 'async_stream_chat', transport)
    cli, headers = client_surface
    body = request_body(stream=stream, tools=[tool])
    body.update(tool_choice={'type': 'function', 'function': {'name': name}},
                top_p=0.75, seed=7, stop=['END'], parallel_tool_calls=False,
                response_format={'type': 'json_object'})
    response = await cli.post('/v1/chat/completions', json=body, headers=headers)
    data = await response.get_data(as_text=True)
    assert response.status_code == 200, data
    assert response.headers['X-Tofu-Request-Id'].startswith('direct-')
    assert not path.exists(), 'declaring a server tool name must not execute it'
    assert len(observed) == 1
    assert observed[0]['messages'] == body['messages']
    assert observed[0]['tools'] == [tool]
    for key in ('tool_choice', 'top_p', 'seed', 'stop', 'parallel_tool_calls', 'response_format'):
        assert observed[0][key] == body[key]
    if stream:
        output = frames(data)
        calls = [entry['choices'][0]['delta']['tool_calls'] for entry in output
                 if entry['choices'][0]['delta'].get('tool_calls')]
        assert calls == [[dict(call, index=0)]]
        assert output[-1]['choices'][0]['finish_reason'] == 'tool_calls'
        assert output[-1]['usage']['prompt_tokens_details']['cached_tokens'] == 3
        assert data.endswith('data: [DONE]\n\n')
    else:
        output = json.loads(data)
        assert output['choices'][0]['message']['tool_calls'] == [call]
        assert output['choices'][0]['finish_reason'] == 'tool_calls'
        assert output['usage']['total_tokens'] == 18
        assert output['usage']['prompt_tokens_details']['cached_tokens'] == 3
        assert 'task_id' not in output
    # Only the external client supplies the result; no fabricated tool message.
    body['stream'] = False
    body['tool_choice'] = 'none'
    body['messages'].extend([
        {'role': 'assistant', 'content': None, 'tool_calls': [call]},
        {'role': 'tool', 'tool_call_id': call['id'], 'content': 'CLIENT_ONLY_RESULT'},
    ])
    followup = await cli.post('/v1/chat/completions', json=body, headers=headers)
    assert followup.status_code == 200, await followup.get_data(as_text=True)
    assert (await followup.get_json())['choices'][0]['message']['content'] == 'client result accepted'
    assert len(observed) == 2
    expected = deepcopy(body['messages'])
    expected[1].pop('content')  # Shared body normalization removes null content.
    assert observed[1]['messages'] == expected
    assert not path.exists()


async def test_empty_catalog_is_single_round_without_injected_prompt_or_tools(client_surface, monkeypatch):
    from lib.llm import astream
    observed = []

    async def transport(body, **kwargs):
        observed.append(deepcopy(body))
        kwargs['on_content']('OK')
        return ProviderStreamResult.from_legacy({'content': 'OK'}, 'stop', {})

    monkeypatch.setattr(astream, 'async_stream_chat', transport)
    cli, headers = client_surface
    body = request_body()
    response = await cli.post('/v1/chat/completions', json=body, headers=headers)
    assert response.status_code == 200, await response.get_data(as_text=True)
    assert (await response.get_json())['choices'][0]['message']['content'] == 'OK'
    assert len(observed) == 1
    assert observed[0]['messages'] == body['messages']
    assert not observed[0].get('tools')


@pytest.mark.parametrize('invalid', [
    {'tools': None}, {'tools': {}},
    {'tools': [{'type': 'function', 'function': {'name': ['bad']}}]},
    {'tools': [{'type': 'function', 'function': {'name': 'a'}}],
     'tool_choice': {'type': 'function', 'function': {'name': []}}},
    {'tools': [], 'tool_choice': 'required'}, {'tools': [], 'n': 2},
])
async def test_invalid_client_catalog_refuses_before_dispatch(client_surface, monkeypatch, invalid):
    from lib.llm import astream
    async def forbidden(*args, **kwargs):
        pytest.fail('invalid catalog must not reach a provider')
    monkeypatch.setattr(astream, 'async_stream_chat', forbidden)
    cli, headers = client_surface
    body = request_body()
    body.update(invalid)
    response = await cli.post('/v1/chat/completions', json=body, headers=headers)
    assert response.status_code == 400, await response.get_data(as_text=True)


async def test_scope_and_foreign_owner_cannot_reach_provider(client_surface, monkeypatch):
    from lib.api_keys import create_key
    from lib.llm import astream
    async def forbidden(*args, **kwargs):
        pytest.fail('unauthorized request must not reach a provider')
    monkeypatch.setattr(astream, 'async_stream_chat', forbidden)
    cli, _ = client_surface
    for owner, scopes, status in [(OWNER, ['providers'], 403), (OWNER + 1, ['chat'], 400)]:
        _, token = create_key(owner_user_id=owner, name='no-authority', scopes=scopes)
        response = await cli.post('/v1/chat/completions', json=request_body(),
                                  headers={'Authorization': f'Bearer {token}'})
        assert response.status_code == status, await response.get_data(as_text=True)


async def test_duplicate_sync_request_replays_same_identity_without_provider_reentry(client_surface, monkeypatch):
    from lib.llm import astream
    observed = []
    async def transport(body, **kwargs):
        observed.append(body)
        return ProviderStreamResult.from_legacy({'content': 'once'}, 'stop',
                                                {'prompt_tokens': 3, 'completion_tokens': 1})
    monkeypatch.setattr(astream, 'async_stream_chat', transport)
    cli, headers = client_surface
    headers = {**headers, 'Idempotency-Key': 'client-tools-once'}
    first = await cli.post('/v1/chat/completions', json=request_body(), headers=headers)
    second = await cli.post('/v1/chat/completions', json=request_body(), headers=headers)
    assert first.status_code == second.status_code == 200
    assert await first.get_json() == await second.get_json()
    assert first.headers['X-Tofu-Request-Id'] == second.headers['X-Tofu-Request-Id']
    assert len(observed) == 1


@pytest.mark.parametrize('failure', ['none', 'billing_deferred', 'route_failed'])
async def test_paid_sync_call_settles_before_tool_handoff(
        client_surface, monkeypatch, failure):
    from lib.api_keys import create_key
    from lib.llm import astream
    import routes.api_v1.chat_direct as direct
    from lib.agent_core.admission import controller

    from lib.billing.users import create_user
    account = create_user(f'compat-paid-{failure}@example.test', password='test-only')
    install_native_test_model_route(owner_user_id=account.owner_user_id)
    observations = []
    tool = {'type': 'function', 'function': {'name': 'client_only', 'description': 'x' * 8000}}
    call = {'id': 'paid-call', 'type': 'function',
            'function': {'name': 'client_only', 'arguments': '{}'}}
    before = controller.in_flight
    async def transport(body, **kwargs):
        observations.append('provider')
        return ProviderStreamResult.from_legacy({'tool_calls': [call]}, 'tool_calls',
            {'prompt_tokens': 2010, 'completion_tokens': 8, 'total_tokens': 2018})
    def reserve(record, **kwargs):
        assert kwargs['prompt_tokens'] >= 2000
        observations.append('reserve')
        return 100
    def settle(record, **kwargs):
        observations.append('settle')
        assert record['usage']['total_tokens'] == 2018
        assert kwargs['user_id'] == account.id
        if failure == 'billing_deferred':
            raise RuntimeError('private settlement failure')
        return {'charged_micro': 17}
    monkeypatch.setattr(astream, 'async_stream_chat', transport)
    monkeypatch.setattr(direct, 'reserve_for_task', reserve)
    monkeypatch.setattr(direct, 'settle_task', settle)
    def release(*args, **kwargs):
        pytest.fail('started provider work cannot refund the reservation')
    monkeypatch.setattr(direct, 'release_reservation', release)
    dispose = direct.dispose_routed_slot_group
    def dispose_route(group):
        dispose(group)
        if failure == 'route_failed':
            raise RuntimeError('route release not acknowledged')
    monkeypatch.setattr(direct, 'dispose_routed_slot_group', dispose_route)
    cli, _ = client_surface
    _, token = create_key(owner_user_id=account.owner_user_id, account_user_id=account.id,
                          name='paid-client', scopes=['chat'])
    try:
        response = await cli.post('/v1/chat/completions', json=request_body(tools=[tool]),
                                  headers={'Authorization': f'Bearer {token}'})
    finally:
        clear_test_model_routing(owner_user_id=account.owner_user_id)
    data = await response.get_json()
    assert observations == ['reserve', 'provider', 'settle']
    assert controller.in_flight == before
    if failure == 'route_failed':
        assert response.status_code == 502
        assert 'error' in data and 'choices' not in data
        assert 'paid-call' not in json.dumps(data)
        assert 'private settlement failure' not in json.dumps(data)
    else:
        assert response.status_code == 200, data
        assert data['choices'][0]['message']['tool_calls'] == [call]
        if failure == 'billing_deferred':
            assert 'billing' not in data  # Existing recoverable hold; no invented invoice.
        else:
            assert data['billing'] == {'charged_micro': 17}


@pytest.mark.parametrize('billing_case', ['complete', 'unknown', 'partial', 'admission_lost_ack'])
@pytest.mark.parametrize('wire', ['json', 'sse'])
async def test_paid_request_uses_physical_usage_and_durable_wallet_barrier(
        client_surface, monkeypatch, billing_case, wire):
    import httpx
    from lib.api_keys import create_key
    from lib.billing import users, deposit, get_balance, compute_request_cost
    from lib.llm import astream
    from lib.storage import get_storage_client
    from lib.storage_authority_sql.generated_billing_contract import REQUEST_ADMISSION_KIND
    from lib.billing.request_settlement import recover, IncompleteRequestUsage
    import routes.api_v1.chat_direct as direct

    failures = []
    original_stream = direct.run_direct_stream
    async def observed_stream(*args, **kwargs):
        async for frame in original_stream(*args, **kwargs):
            yield frame
        if kwargs['dispatch_terminal'].get('exception'):
            failures.append(repr(kwargs['dispatch_terminal']['exception']))
    monkeypatch.setattr(direct, 'run_direct_stream', observed_stream)

    account = users.create_user(f'actual-wallet-{billing_case}@example.test')
    deposit(account.id, 10000, ref_id='seed')
    install_native_test_model_route(owner_user_id=account.owner_user_id, model_id='gpt-4o-mini')
    from lib.model_routing.repository import ModelRoutingRepository
    from lib.model_routing import OwnerBoundary
    repository = ModelRoutingRepository()
    boundary = OwnerBoundary.create(account.owner_user_id)
    current = repository.get(boundary)
    document = deepcopy(current.document)
    for connection in document['connections']:
        if connection['protocol'] == 'anthropic':
            connection['enabled'] = False
    repository.compare_and_swap(boundary, document, expected_revision=current.revision)
    monkeypatch.setattr('lib.relay_config.billing_enabled', lambda: True)
    monkeypatch.setattr('lib.desktop.egress.route_request', lambda *args, **kwargs: 'direct')
    monkeypatch.setattr('lib.proxy.subscription_route_candidates', lambda url, **kwargs: [])
    monkeypatch.setattr(astream, '_resolve_async_route', lambda url: (None, {'mode': 'fixture'}))
    client = get_storage_client(write=True)
    sends, identities = [], []
    command = client.command
    def observe_command(operation, payload, *args, **kwargs):
        result = command(operation, payload, *args, **kwargs)
        if operation == 'billing.wallet.apply' and payload.get('kind') == REQUEST_ADMISSION_KIND:
            identities.append(payload['ref_id'])
            if billing_case == 'admission_lost_ack':
                raise OSError('admission committed ACK lost')
        return result
    monkeypatch.setattr(client, 'command', observe_command)
    usage = {'prompt_tokens': 1000, 'completion_tokens': 10, 'total_tokens': 1010}
    def transport(request):
        sends.append(json.loads(request.content))
        assert identities, 'provider entered before durable admission'
        assert not any(key.startswith('_') for key in sends[-1])
        provider_frames = [{'choices': [{'index': 0, 'delta': {'content': 'verified'}, 'finish_reason': None}]},
                  {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}]
        if billing_case == 'partial':
            provider_frames.pop()  # Counts alone cannot prove a complete provider response.
        if billing_case in {'complete', 'partial'}:
            provider_frames.append({'choices': [], 'usage': usage})
        raw = ''.join('data: ' + json.dumps(frame) + '\n\n' for frame in provider_frames) + 'data: [DONE]\n\n'
        return httpx.Response(200, content=raw.encode(), headers={'content-type': 'text/event-stream'})
    _, token = create_key(owner_user_id=account.owner_user_id, account_user_id=account.id,
                          name='actual-wallet', scopes=['chat'])
    cli, _ = client_surface
    body = {**request_body(stream=wire == 'sse'), 'model': 'gpt-4o-mini'}
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        monkeypatch.setattr(astream, 'get_async_client', lambda proxy: http)
        response = await cli.post('/v1/chat/completions', json=body,
                                  headers={'Authorization': f'Bearer {token}'})
    data = (frames((await response.get_data()).decode())[-1] if wire == 'sse'
            else await response.get_json())
    assert len(identities) == 1
    request_id = identities[0]
    hold = client.query('billing.ledger.find', dict(user_id=account.id,
        kind='reserve', ref_type='reserve', ref_id=request_id))
    held = -hold['amount_micro']
    assert held > 0
    if billing_case == 'complete':
        assert response.status_code == 200, (data, failures)
        expected = compute_request_cost('gpt-4o-mini', provider_id='test-provider', raw_usage=usage).micro
        assert expected > 0 and data['billing']['cost_micro'] == expected
        assert data['billing']['attempt_count'] == 1
        assert get_balance(account.id) == 10000 - expected
        from lib.storage_authority_sql.generated_billing_contract import REQUEST_INVOICE_KIND
        invoice = client.query('billing.ledger.find', dict(user_id=account.id,
            kind=REQUEST_INVOICE_KIND, ref_type='reserve', ref_id=request_id))
        quote = json.loads(invoice['note'])['quotes'][0]
        assert quote['model'] == 'gpt-4o-mini' and quote['provider_id'] == 'test-provider'
        assert sends[0]['model'] == 'gpt-4o-mini-wire'
    else:
        assert 'billing' not in data
        assert get_balance(account.id) == 10000 - held
        with pytest.raises(IncompleteRequestUsage):
            recover(wallet_user_id=account.id, task_id=request_id, held_micro=held)
        if billing_case == 'admission_lost_ack':
            assert response.status_code == (502 if wire == 'json' else 200) and not sends
            assert data.get('error')
        elif billing_case == 'partial':
            assert response.status_code == (502 if wire == 'json' else 200) and len(sends) == 1
            assert data.get('error')
        else:
            assert response.status_code == 200 and len(sends) == 1
            assert data['billing_status'] == 'pending_usage'
