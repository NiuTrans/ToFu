"""Explicit catalog evidence survives protocol discovery and endpoint import."""
from copy import deepcopy

import pytest

from lib.llm_dispatch.discovery import discover_models
from lib.llm_dispatch.discovery._capabilities import _infer_capabilities
from lib.model_routing.local_provider import build_local_provider_bundle
from lib.oauth.codex_catalog import _normalise_models, _provider_models

pytestmark = pytest.mark.unit


@pytest.fixture
def transport(monkeypatch):
    calls = []
    pages = []
    monkeypatch.setattr('lib.byo.byo_egress.validate_egress_url', lambda url: None)

    def get(url, **kwargs):
        calls.append((url, kwargs))
        payload = pages.pop(0)
        class Response:
            ok = True
            def json(self):
                return deepcopy(payload)
        return Response()

    monkeypatch.setattr('lib.llm_dispatch.discovery.http_get', get)
    return calls, pages


@pytest.mark.parametrize('protocol,suffix', [('openai', '/chat/completions'), ('openai_responses', '/responses')])
def test_models_path_and_unknown_reasoning(transport, protocol, suffix):
    calls, pages = transport
    pages.append({'data': [{'id': 'qwen-999-future'}]})
    rows = discover_models('https://catalog.example/v1' + suffix, 'test-key', protocol=protocol)
    assert calls[0][0] == 'https://catalog.example/v1/models'
    assert calls[0][1]['headers']['Authorization'] == 'Bearer test-key'
    assert rows[0]['capabilities'] == ['text']
    assert not rows[0]['inference_profile'].get('reasoning')
    assert 'thinking_default' not in rows[0]


@pytest.mark.parametrize('model_id, task', [
    ('whisper-large-v3', 'transcription'),
    ('Doubao-Seed-ASR-2.0', 'transcription'),
    ('gpt-4o-mini-tts', 'tts'),
    ('speech-02-hd', 'tts'),
    ('veo-3.1-generate-001', 'video_gen'),
    ('doubao-seedance-1-0-pro-250528', 'video_gen'),
    ('wan2.2-i2v-plus', 'video_gen'),
    ('wan2.1-image-to-video', 'video_gen'),
    ('lyria-3.5', 'audio_gen'),
    ('imagen-4.0-generate-001', 'image_gen'),
    ('doubao-seedream-4-0-250828', 'image_gen'),
    ('Qwen3-Reranker-0.6B', 'reranking'),
    ('text-embedding-3-large', 'embedding'),
])
def test_bare_catalog_ids_keep_their_task_family(model_id, task):
    """A /models row without declared capabilities still proves no optional
    feature, but its unmistakable task family never becomes a chat 'text'."""
    assert _infer_capabilities(model_id, {'id': model_id}) == {task}
    assert _infer_capabilities(model_id) == {task}


@pytest.mark.parametrize('model_id', [
    'gpt-5.6', 'claude-opus-5-5', 'gemini-3-pro-preview', 'qwen3-omni-flash',
    'gpt-4o-audio-preview', 'deepseek-v4', 'kimi-k3', 'qwen-vl-max',
])
def test_chat_ids_are_not_mistaken_for_media_models(model_id):
    assert _infer_capabilities(model_id, {'id': model_id}) == {'text'}
    assert 'text' in _infer_capabilities(model_id)


def test_explicit_capabilities_not_unioned():
    assert _infer_capabilities('gpt-5-reasoning-vision', {'capabilities': ['CUSTOM']}) == {'CUSTOM'}
    assert _infer_capabilities('qwen3', {'capabilities': []}) == set()
    assert _infer_capabilities('claude-99', {'capabilities': {'thinking': {'supported': False}}}) == set()


def test_listing_preserves_context_evidence_and_marks_absence(transport):
    _, pages = transport
    pages.append({'data': [{'id': 'bare-model'},
        {'id': 'limited', 'context_window': 32768, 'capabilities': ['text']},
        {'id': 'alternate-field', 'context_length': 500000},
        {'id': 'invalid-context', 'context_window': True}]})
    rows = {r['model_id']: r for r in discover_models('https://catalog.example/v1', 'test-key')}
    assert 'context_window' not in rows['bare-model']
    assert set(rows['bare-model']['inherited_model_fields']) == {'capabilities', 'context_window'}
    assert rows['limited']['context_window'] == 32768
    assert rows['limited']['inherited_model_fields'] == []
    assert rows['alternate-field']['context_window'] == 500000
    assert rows['alternate-field']['inherited_model_fields'] == ['capabilities']
    assert 'context_window' in rows['invalid-context']['inherited_model_fields']


def test_native_anthropic_metadata_and_pagination(transport):
    calls, pages = transport
    pages.extend([
        {'data': [{'id': 'future-native', 'capabilities': {
            'thinking': {'supported': True, 'types': ['enabled', 'adaptive']},
            'effort': {'supported': True, 'levels': ['low', 'high', 'future']},
            'vision': {'supported': False},
        }}], 'has_more': True, 'last_id': 'future-native'},
        {'data': [{'id': 'plain', 'capabilities': {'thinking': {'supported': False}}}], 'has_more': False},
    ])
    rows = discover_models('https://catalog.example', 'native-key', protocol='anthropic')
    assert calls[0][0] == 'https://catalog.example/v1/models'
    assert calls[1][0].endswith('?after_id=future-native')
    headers = calls[0][1]['headers']
    assert headers['x-api-key'] == 'native-key'
    assert headers['anthropic-version'] == '2023-06-01'
    assert 'Authorization' not in headers
    profile = next(row for row in rows if row['model_id'] == 'future-native')['inference_profile']
    assert profile['reasoning']['enabled_type'] == 'adaptive'
    assert profile['reasoning']['supported_efforts'] == ['low', 'high', 'future']
    assert profile['features']['vision'] == 'unsupported'
    plain = next(row for row in rows if row['model_id'] == 'plain')
    assert plain['inference_profile']['reasoning']['support'] == 'unsupported'


def test_native_cursor_loop_fails_closed(transport):
    calls, pages = transport
    pages.extend([{'data': [{'id': 'x'}], 'has_more': True, 'last_id': 'x'}] * 2)
    assert discover_models('https://catalog.example/v1', '', protocol='anthropic') == []
    assert len(calls) == 2


def test_codex_levels_survive_projection_into_offering():
    rows = _normalise_models({'models': [{'slug': 'future-model',
        'supported_reasoning_levels': [{'effort': 'low'}, {'effort': 'future'}],
        'default_reasoning_level': 'future'}]})
    models = _provider_models(rows)
    bundle = build_local_provider_bundle(provider_id='local-fixture', display_name='Fixture',
        base_url='http://localhost:12345/v1', models=models)
    profile = bundle['offerings'][0]['inference_profile']
    assert profile['reasoning']['supported_efforts'] == ['low', 'future']
    assert profile['reasoning']['default_effort'] == 'future'
    assert profile['reasoning']['wire_format'] == 'reasoning_effort'
    assert profile['source'] == 'provider_catalog'
    assert bundle['deployments'][0]['probe_status'] == 'unprobed'


def test_declared_local_dialect_is_not_lost():
    profile = {'source': 'configured', 'reasoning': {'support': 'supported',
        'wire_format': 'chat_template_kwargs', 'can_disable': True}}
    bundle = build_local_provider_bundle(provider_id='local-fixture', display_name='Fixture',
        base_url='http://localhost:12345/v1', models=[{'model_id': 'unknown',
        'capabilities': [], 'inference_profile': profile}])
    assert bundle['offerings'][0]['inference_profile'] == profile
    assert bundle['offerings'][0]['capabilities'] == []
    assert 'models' not in bundle


def test_oauth_refresh_preserves_owner_profile_and_grants():
    from lib.model_routing import InMemoryModelRoutingRepository, OwnerBoundary, empty_document
    from lib.oauth.outbound import _upsert_oauth_provider
    repo = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(73, 'discovery-fixture')
    repo.compare_and_swap(boundary, empty_document(), expected_revision=0)
    entry = {'id': 'oauth-fixture', 'name': 'Fixture', 'base_url': 'https://catalog.example/v1',
             'protocol': 'openai_responses', 'oauth': 'codex', 'api_keys': ['oauth-managed'],
             'models': [{'model_id': 'unknown-future', 'capabilities': ['text'],
                         'inference_profile': {'source': 'provider_catalog', 'reasoning': {
                             'support': 'supported', 'supported_efforts': ['low', 'high']}}}]}
    _upsert_oauth_provider(entry, owner_user_id=73, tenant_id='discovery-fixture', repository=repo)
    override = {'source': 'owner', 'reasoning': {'support': 'unsupported', 'wire_format': 'none'}}

    def disable(doc):
        doc['provider_accesses'][0]['enabled'] = False
        doc['offerings'][0]['enabled'] = False
        doc['offerings'][0]['inference_profile'] = override
        doc['credentials'][0]['enabled'] = False
        doc['credentials'][0]['authorization']['excluded_wire_model_ids'] = ['unknown-future']
        return doc

    repo.mutate(boundary, disable)
    grants = deepcopy(repo.get(boundary).document['credentials'][0]['authorization'])
    entry['models'][0]['inference_profile']['reasoning']['supported_efforts'] = ['high']
    _upsert_oauth_provider(entry, owner_user_id=73, tenant_id='discovery-fixture', repository=repo)
    doc = repo.get(boundary).document
    assert doc['offerings'][0]['inference_profile'] == override
    assert doc['offerings'][0]['enabled'] is False
    assert doc['provider_accesses'][0]['enabled'] is False
    assert doc['credentials'][0]['enabled'] is False
    assert doc['credentials'][0]['authorization'] == grants


@pytest.mark.parametrize('protocol', ['openai', 'openai_responses', 'anthropic'])
def test_catalog_sync_target_protocol(protocol):
    from lib.llm_dispatch.provider_catalog_sync import _eligible_targets
    document = {'provider_accesses': [{'provider_access_id': 'a', 'provider_id': 'p'}],
        'connections': [{'connection_id': 'c', 'provider_access_id': 'a',
            'base_url': 'https://catalog.example/v1', 'protocol': protocol}],
        'credentials': [{'provider_access_id': 'a', 'kind': 'api_key',
            'secret_reference': 'ref', 'authorization': {'connection_ids': ['c']}}]}
    assert _eligible_targets(document)[0]['protocol'] == protocol
