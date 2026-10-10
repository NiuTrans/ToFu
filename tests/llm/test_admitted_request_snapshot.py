"""No-network immutable ingress and bounded cache-research evidence."""
from dataclasses import FrozenInstanceError
import json

import pytest

from lib.llm._sse_core import prepare_request
from lib.llm.request import AdmittedRequest, transport_body
from lib.llm_errors import PromptTooLongError

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def local_window(monkeypatch):
    monkeypatch.setattr(
        'lib.tasks_pkg.compaction._tokens.resolve_model_context_limit',
        lambda model, provider_id='': 1000 if provider_id == 'small' else 100000)


def body():
    return {'model': 'gpt-4o', 'messages': [{'role': 'user', 'content': 'hello'}],
            'system': 'stable', 'stream': True,
            'tools': [{'type': 'function', 'function': {
                'name': 'lookup', 'description': 'find things',
                'parameters': {'type': 'object', 'properties': {}}}}]}


def prepare(value, **kwargs):
    return prepare_request(value, base_url='https://example.invalid/v1', **kwargs)


def test_snapshot_is_used_and_nested_reads_cannot_mutate_it():
    source = body()
    plan = prepare(source)
    assert isinstance(plan.body, AdmittedRequest)
    before = plan.body.payload
    source['messages'][0]['content'] = 'changed'
    source['tools'][0]['function']['description'] = 'changed'
    source['system'] = 'changed'
    detached = transport_body(plan.body)
    detached['messages'][0]['content'] = 'changed again'
    assert plan.body.payload == before
    assert transport_body(plan.body)['messages'][0]['content'] == 'hello'
    with pytest.raises(FrozenInstanceError):
        plan.body.input_tokens = 0
    assert not any(key.startswith('_') for key in json.loads(before))
    assert len(json.dumps(plan.body.diagnostics())) < 300


def test_ids_stable_but_provider_projection_separate(monkeypatch):
    calls = []
    def count(messages, **kwargs):
        calls.append(kwargs)
        assert kwargs['mode'] == 'tiktoken'
        return {'tokens': 20}
    monkeypatch.setattr('lib.token_counter.count_tokens', count)
    first = prepare(body()).body
    second = prepare(body()).body
    assert first.diagnostics() == second.diagnostics()
    other = prepare_request(body(), base_url='https://other.invalid/v1').body
    assert first.semantic_id == other.semantic_id
    assert first.provider_prefix_id != other.provider_prefix_id
    assert len(calls) == 3


@pytest.mark.parametrize('field', ['messages', 'tools', 'system'])
def test_mutated_input_never_borrows_admission(field):
    source = body()
    source['_admission_provider_id'] = 'small'
    admitted = prepare(source).body
    huge = 'hello world ' * 3000
    if field == 'messages':
        source[field][0]['content'] = huge
    elif field == 'tools':
        source[field][0]['function']['description'] = huge
    else:
        source[field] = huge
    source['_admitted_input_tokens'] = admitted.input_tokens
    with pytest.raises(PromptTooLongError):
        prepare(source)


def test_changed_route_rechecks_window():
    source = body()
    source['messages'][0]['content'] = 'hello world ' * 1000
    first = prepare(source).body
    assert first.input_tokens > 100
    source['_admission_provider_id'] = 'small'
    with pytest.raises(PromptTooLongError):
        prepare(source)


@pytest.mark.parametrize('protocol', ['openai', 'responses', 'anthropic'])
def test_internal_fields_never_enter_provider_wire(protocol):
    source = body()
    source['_private_test'] = 'not sent'
    plan = prepare(source, api_protocol=protocol)
    assert isinstance(plan.body, AdmittedRequest)
    assert not any(key.startswith('_') for key in transport_body(plan.body))
    assert plan.raw_dumper.request_identity == plan.body.diagnostics()


def test_mapping_reads_parse_only_requested_complex_fields(monkeypatch):
    from lib.llm.request import seal_request
    image = 'data:image/png;base64,' + 'A' * 1000000
    source = {'model': 'gpt-4o', 'stream': True, 'max_tokens': 10,
              'messages': [{'role': 'user', 'content': [
                  {'type': 'image_url', 'image_url': {'url': image}}]}],
              'tools': [{'name': 'lookup'}]}
    request = seal_request(source, model='gpt-4o', input_tokens=1,
                           semantic_id='test')
    loads = json.loads
    sizes = []
    def tracked(value, *args, **kwargs):
        sizes.append(len(value))
        return loads(value, *args, **kwargs)
    monkeypatch.setattr('lib.llm.request.json.loads', tracked)
    assert request.get('model') == 'gpt-4o'
    assert request.get('missing') is None
    assert len(request) == len(source)
    assert list(request) == list(source)
    assert sizes == []
    decoded = dict(request)
    assert decoded == source
    assert len(sizes) == 2
    assert sum(sizes) < len(request.payload)
    decoded['messages'].clear()
    assert request['messages'] == source['messages']
    sizes.clear()
    assert transport_body(request) == source
    assert sizes == [len(request.payload)]


@pytest.mark.parametrize('streaming', [True, False])
def test_claude_cloak_must_fit_before_sealing(monkeypatch, streaming):
    monkeypatch.setattr(
        'lib.tasks_pkg.compaction._tokens.resolve_model_context_limit',
        lambda *args: 1000)
    monkeypatch.setattr('lib.oauth.outbound.resolve_oauth_request',
                        lambda oauth, body, headers, **kwargs: ('fake', {}, body))
    seen = []
    def count(messages, **kwargs):
        system = kwargs.get('system')
        seen.append(system)
        return {'tokens': 1001 if 'x-anthropic-billing-header' in str(system) else 10}
    monkeypatch.setattr('lib.token_counter.count_tokens', count)
    with pytest.raises(PromptTooLongError, match='before transport'):
        if streaming:
            prepare(body(), api_protocol='anthropic', oauth='claude')
        else:
            from lib.llm.chat import chat
            chat([{'role': 'user', 'content': 'hello'}], model='claude-sonnet-4',
                 api_protocol='anthropic', oauth='claude',
                 base_url='https://example.invalid/v1')
    assert 'x-anthropic-billing-header' in str(seen[-1])
    assert any('x-anthropic-billing-header' not in str(system) for system in seen)


def test_final_anthropic_recount_preserves_native_count_bearing_blocks(monkeypatch):
    from lib.llm.request import recount_anthropic_input
    from lib.token_counter.base import count_images, iter_message_texts
    def count(messages, **kwargs):
        assert count_images(messages) == 1
        texts = list(iter_message_texts(messages, kwargs['system'], kwargs['tools']))
        assert 'lookup' in texts
        assert '{"query":"needle"}' in texts
        assert 'reasoning' in texts
        assert 'receipt' in texts
        assert not any('opaque' in text for text in texts)
        return {'tokens': 77}
    monkeypatch.setattr('lib.token_counter.count_tokens', count)
    assert recount_anthropic_input({'messages': [{'role': 'assistant', 'content': [
        {'type': 'image', 'source': {'data': 'opaque pixels'}},
        {'type': 'tool_use', 'name': 'lookup', 'input': {'query': 'needle'}},
        {'type': 'tool_result', 'content': 'receipt'},
        {'type': 'thinking', 'thinking': 'reasoning', 'signature': 'opaque'},
    ]}]}, model='claude-sonnet-4') == 77


def test_probe_wire_projection_preserves_nested_user_fields():
    from copy import deepcopy
    from lib.llm.request import strip_internal_request_fields
    source = {'model': 'test', '_conversion_report': {'losses': []},
              '_private_future_field': 'internal',
              'tools': [{'parameters': {'properties': {'_user_field': {'type': 'string'}}}}]}
    before = deepcopy(source)
    wire = strip_internal_request_fields(source)
    assert set(wire) == {'model', 'tools'}
    assert '_user_field' in wire['tools'][0]['parameters']['properties']
    assert source == before
