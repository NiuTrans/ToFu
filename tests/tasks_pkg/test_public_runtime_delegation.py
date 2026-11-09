"""A run's delegated children route through that run's own model-routing authority.

Only the provider transport (``lib.llm.stream_chat``) is simulated. Runtime
admission, the root agent loop, ``spawn_agents``/``poll``, the swarm session and
its follow-up wave, child invocation, routed slot minting and the child agent
loop are production code.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import json
import socket
import threading

import pytest

from tests.support.standalone_model_routing import standalone_model_routing_envelope

_WORKER_POLICY = 'Work only on the delegated objective'
_CHILDREN = ['child-a', 'child-b']
_CONFIG = {'tools': '*', 'tools.nativeExposure': 'full'}


@pytest.fixture
def storage_free_process(monkeypatch, tmp_path):
    """The process storage runtime exists but was never started (`tofu-agent run`)."""
    from lib.storage import StorageRuntime, StorageSupervisor
    from lib.storage import service as storage_service

    runtime = StorageRuntime(
        StorageSupervisor(project_root=tmp_path / 'absent', backend='sqlite'),
        auto_restart=False)
    monkeypatch.setattr(storage_service, '_runtime', runtime)
    return runtime


@pytest.fixture
def provider_urls():
    """Owned TCP listeners satisfy production route reachability preflight."""
    endpoints = [socket.create_server(('127.0.0.1', 0)) for _ in range(3)]
    try:
        yield [f'http://127.0.0.1:{endpoint.getsockname()[1]}/v1' for endpoint in endpoints]
    finally:
        for endpoint in endpoints:
            endpoint.close()


def _delegating_provider(monkeypatch):
    """Root: spawn child-a, add child-b as a follow-up wave, wait for both.

    child-a's model call is held until the follow-up wave was submitted, so the
    second wave joins the live session through its retained assignment proxy.
    The root answers with the poll evidence.
    """
    import lib.llm
    from lib.llm.stream_result import ProviderStreamResult, ProviderStreamState

    requests = []
    lock = threading.Lock()
    follow_up_submitted = defaultdict(threading.Event)

    def call(identifier, name, arguments):
        return {'id': identifier, 'type': 'function',
                'function': {'name': name, 'arguments': json.dumps(arguments)}}

    def spawn(run, agent_id):
        return call(f'{run}-spawn-{agent_id}', 'spawn_agents', {'agents': [{
            'id': agent_id, 'role': 'general',
            'objective': f'Reply ready for {run} as {agent_id}.'}]})

    def stream(body, **kwargs):
        transcript = json.dumps(body['messages'], ensure_ascii=False)
        run = next(label for label in ('run-a', 'run-b') if label in transcript)
        child = _WORKER_POLICY in transcript
        tool_results = [m for m in body['messages'] if m.get('role') == 'tool']
        with lock:
            requests.append({'run': run, 'actor': 'child' if child else 'root',
                             'base_url': kwargs.get('base_url'),
                             'api_key': kwargs.get('api_key'),
                             'model': body.get('model')})
        calls, content = [], ''
        if child:
            if 'as child-a' in transcript:
                assert follow_up_submitted[run].wait(30), 'follow-up wave never submitted'
            content = f'ready from {run}'
        elif len(tool_results) == 0:
            calls = [spawn(run, 'child-a')]
        elif len(tool_results) == 1:
            calls = [spawn(run, 'child-b')]
        elif len(tool_results) == 2:
            follow_up_submitted[run].set()
            calls = [call(f'{run}-wait', 'poll',
                          {'ids': _CHILDREN, 'wait_s': 60, 'mode': 'all'})]
        else:
            content = str(tool_results[-1]['content'])
        for tool_call in calls:
            kwargs['on_tool_call_ready'](tool_call)
        if content:
            kwargs['on_content'](content)
        message = {'role': 'assistant', 'content': content}
        if calls:
            message['tool_calls'] = calls
        finish = 'tool_calls' if calls else 'stop'
        return ProviderStreamResult(
            message=message, compatibility_finish_reason=finish,
            provider_finish_reason=finish, state=ProviderStreamState.PROVIDER_FINISHED,
            saw_done=True, saw_finish_reason=True,
            usage={'prompt_tokens': 10, 'completion_tokens': 5,
                   'prompt_tokens_details': {'cached_tokens': 0}})

    monkeypatch.setattr(lib.llm, 'stream_chat', stream)
    return requests


def _assert_children_completed(run, result):
    assert result.ok, (run, result.error)
    assert result.content.count('status: completed') == len(_CHILDREN), result.content
    assert result.content.count(f'ready from {run}') == len(_CHILDREN), result.content


@pytest.mark.unit
def test_spawned_workers_route_through_their_own_runs_envelope(
        monkeypatch, tmp_path, storage_free_process, provider_urls):
    """Two concurrent runs of one owner keep separate request authorities.

    Each run's workers, initial and follow-up wave alike, re-resolve the run's
    ``modelRef`` against that run's envelope: its Connection, Deployment and
    Credential. Neither the absent durable owner repository nor the other run's
    envelope may answer.
    """
    from tofu_agent import AgentRuntime, ModelRoutingConfig

    requests = _delegating_provider(monkeypatch)
    envelopes = {
        'run-a': standalone_model_routing_envelope(
            base_url=provider_urls[0], secret='sk-run-a', model_id='model-a',
            provider_id='provider-a'),
        'run-b': standalone_model_routing_envelope(
            base_url=provider_urls[1], secret='sk-run-b', model_id='model-b',
            provider_id='provider-b'),
    }
    config = {**_CONFIG, 'project': str(tmp_path)}
    access = ModelRoutingConfig.from_mapping(envelopes['run-a'])
    with AgentRuntime.local(model_routing=access) as runtime:
        executions = {
            'run-a': runtime.start([{'role': 'user', 'content': 'Delegate for run-a.'}],
                                   config=config, timeout_s=60),
            'run-b': runtime.start([{'role': 'user', 'content': 'Delegate for run-b.'}],
                                   config=config, model_routing=envelopes['run-b'],
                                   model=envelopes['run-b']['model'], timeout_s=60),
        }
        results = {run: execution.result() for run, execution in executions.items()}

    for run, result in results.items():
        _assert_children_completed(run, result)
    expected = {
        'run-a': (provider_urls[0], 'sk-run-a', 'wire/model-a'),
        'run-b': (provider_urls[1], 'sk-run-b', 'wire/model-b'),
    }
    assert all((row['base_url'], row['api_key'], row['model']) == expected[row['run']]
               for row in requests), requests
    assert Counter((row['run'], row['actor']) for row in requests) == {
        ('run-a', 'root'): 4, ('run-a', 'child'): 2,
        ('run-b', 'root'): 4, ('run-b', 'child'): 2}


@pytest.mark.unit
def test_workers_inherit_the_runs_provider_preference(
        monkeypatch, tmp_path, storage_free_process, provider_urls):
    """The run's ``routing`` policy, not just its table, reaches its workers.

    Two Providers offer the same Model; the lower-ranked one is the run's
    preferred Provider. Workers must follow the run's preference instead of
    re-ranking the table without it.
    """
    from tofu_agent import AgentRuntime, ModelRoutingConfig

    envelope = standalone_model_routing_envelope(
        base_url=provider_urls[0], secret='sk-provider-x', provider_id='provider-x')
    preferred = standalone_model_routing_envelope(
        base_url=provider_urls[1], secret='sk-provider-y', provider_id='provider-y')
    for collection in ('providers', 'provider_accesses', 'connections', 'credentials',
                       'offerings', 'deployments'):
        envelope['model_routing'][collection] += preferred['model_routing'][collection]
    for offering in preferred['model_routing']['offerings']:
        offering['priority'] = 1
    envelope['credential_secrets'].update(preferred['credential_secrets'])
    envelope['routing'] = {'preferred_provider_id': 'provider-y'}

    requests = _delegating_provider(monkeypatch)
    with AgentRuntime.local(model_routing=ModelRoutingConfig.from_mapping(envelope)) as runtime:
        result = runtime.run([{'role': 'user', 'content': 'Delegate for run-a.'}],
                             config={**_CONFIG, 'project': str(tmp_path)}, timeout_s=60)

    _assert_children_completed('run-a', result)
    assert {(row['actor'], row['base_url'], row['api_key']) for row in requests} == {
        ('root', provider_urls[1], 'sk-provider-y'),
        ('child', provider_urls[1], 'sk-provider-y')}, requests


@pytest.fixture
def native_owner_authority(monkeypatch, tmp_path):
    """A real native storage authority serving this process, as in the server."""
    monkeypatch.setenv('TOFUDB_OWNER_ID', '7')
    monkeypatch.setenv('TOFUDB_TRANSPORT', 'daemon')
    from lib.storage import StorageRuntime, StorageSupervisor
    from lib.storage import service as storage_service
    from lib.storage.supervisor import _resolve_tofudb_binary
    try:
        _resolve_tofudb_binary()
    except RuntimeError as exc:
        pytest.skip(f'Explicit matching native test engine unavailable: {exc}')
    runtime = StorageRuntime(
        StorageSupervisor(project_root=tmp_path / 'authority', backend='tofudb',
                          startup_timeout=60),
        auto_restart=False)
    monkeypatch.setattr(storage_service, '_runtime', runtime)
    runtime.start()
    try:
        yield 7
    finally:
        runtime.stop()


@pytest.mark.api
def test_workers_keep_the_runs_envelope_beside_a_native_owner_authority(
        monkeypatch, tmp_path, native_owner_authority, provider_urls):
    """A durable owner table for the same model is never the child's authority.

    The owner's native authority routes ``test-creator/model-a`` to another
    Connection and Credential. The run admitted its root through its own
    envelope, so its workers must reach exactly that envelope's Deployment.
    """
    from lib.identity import PrincipalContext
    from lib.model_routing import ModelRoutingRepository, OwnerBoundary
    from tofu_agent import AgentRuntime, ModelRoutingConfig

    owner = native_owner_authority
    durable = standalone_model_routing_envelope(
        base_url=provider_urls[2], secret='sk-durable-owner', model_id='model-a',
        provider_id='durable-provider')
    boundary = OwnerBoundary.create(owner)
    repository = ModelRoutingRepository()
    for reference, secret in durable['credential_secrets'].items():
        repository.put_secret(boundary, secret, secret_reference=reference)
    repository.compare_and_swap(boundary, durable['model_routing'], expected_revision=0)

    requests = _delegating_provider(monkeypatch)
    envelope = standalone_model_routing_envelope(
        base_url=provider_urls[0], secret='sk-run-a', model_id='model-a',
        provider_id='provider-a')
    runtime = AgentRuntime(
        principal=PrincipalContext.user(subject_id='test:native-owner', owner_user_id=owner),
        model_routing=ModelRoutingConfig.from_mapping(envelope))
    with runtime:
        result = runtime.run([{'role': 'user', 'content': 'Delegate for run-a.'}],
                             config={**_CONFIG, 'project': str(tmp_path)}, timeout_s=60)

    _assert_children_completed('run-a', result)
    assert {(row['base_url'], row['api_key']) for row in requests} == {
        (provider_urls[0], 'sk-run-a')}, requests
    assert Counter(row['actor'] for row in requests) == {'root': 4, 'child': 2}
