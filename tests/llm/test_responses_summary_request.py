"""Readable-summary opt-in belongs to the selected route, independently of replay."""
from __future__ import annotations

import copy

import pytest

from lib.llm.responses_outbound import openai_body_to_responses
from lib.llm.route_features import route_feature

pytestmark = pytest.mark.unit


def _body(declaration=None):
    profile = {'reasoning': {'support': 'supported', 'wire_format': 'reasoning_effort',
                             'supported_efforts': ['low', 'high'], 'default_effort': 'low'}}
    if declaration is not None:
        profile['features'] = {'responses_reasoning_summary': declaration}
    return {'model': 'opaque-route-model', 'messages': [{'role': 'user', 'content': 'work'}],
            '_route_inference_profile': profile, '_responses_feature_profile': 'compatible',
            'reasoning_effort': 'low'}


def test_declared_summary_route_requests_auto_without_replay_or_caller_mutation():
    body = _body('supported')
    before = copy.deepcopy(body)
    wire, _ = openai_body_to_responses(body, stream=True)
    assert wire['reasoning'] == {'effort': 'low', 'summary': 'auto'}
    assert 'include' not in wire
    assert body == before
    assert not any(key.startswith('_') for key in wire)


@pytest.mark.parametrize('declaration', [None, 'unknown', 'unsupported'])
def test_generic_route_does_not_invent_summary_support(declaration):
    wire, _ = openai_body_to_responses(_body(declaration), stream=True)
    assert wire['reasoning'] == {'effort': 'low'}


@pytest.mark.parametrize('summary', [None, 'auto', 'concise', 'detailed'])
def test_explicit_summary_setting_overrides_automatic_request(summary):
    body = _body('supported')
    body['reasoning'] = {'summary': summary}
    wire, _ = openai_body_to_responses(body)
    assert wire['reasoning']['summary'] == summary


@pytest.mark.parametrize('declaration', ['unknown', 'unsupported'])
def test_explicit_route_denial_narrows_codex_summary_default(declaration):
    wire, _ = openai_body_to_responses(_body(declaration), profile='codex', stream=True)
    assert 'summary' not in wire['reasoning']


def test_codex_installed_summary_binding_is_independent_of_public_extensions():
    profile = _body()['_route_inference_profile']
    assert route_feature(profile, 'responses_reasoning_summary', protocol='responses', oauth='codex') is True
    assert route_feature(profile, 'responses_reasoning_summary', protocol='anthropic', oauth='codex') is False
    wire, _ = openai_body_to_responses(_body(), profile='codex', stream=True)
    assert wire['reasoning']['summary'] == 'auto'


@pytest.mark.parametrize('support', ['unknown', 'unsupported'])
def test_endpoint_summary_capability_does_not_enable_unknown_or_nonreasoning_model(support):
    body = _body('supported')
    body['_route_inference_profile']['reasoning'] = {'support': support, 'wire_format': 'none'}
    body.pop('reasoning_effort')
    wire, _ = openai_body_to_responses(body)
    assert 'reasoning' not in wire


def test_automatic_summary_is_omitted_when_reasoning_effort_is_off():
    body = _body('supported')
    body['_route_inference_profile']['reasoning'].update(
        supported_efforts=['none', 'low', 'high'], can_disable=True)
    body['reasoning_effort'] = 'none'
    wire, _ = openai_body_to_responses(body)
    assert wire['reasoning'] == {'effort': 'none'}


def test_explicit_native_summary_request_remains_available_without_default_opt_in():
    body = _body()
    body['reasoning'] = {'summary': 'detailed'}
    wire, _ = openai_body_to_responses(body)
    assert wire['reasoning'] == {'effort': 'low', 'summary': 'detailed'}


def test_declared_summary_does_not_enable_other_protocol_extensions():
    profile = _body('supported')['_route_inference_profile']
    assert route_feature(profile, 'responses_reasoning_summary', protocol='openai_responses') is True
    assert route_feature(profile, 'responses_reasoning_summary', protocol='openai') is False
    assert route_feature(profile, 'responses_programmatic', protocol='responses') is False



@pytest.mark.parametrize('summary', ['auto', None])
def test_summary_policy_reaches_final_sealed_request_without_private_fields(monkeypatch, summary):
    from lib.llm._sse_core import prepare_request

    monkeypatch.setattr('lib.token_counter.count_tokens', lambda *args, **kwargs: {'tokens': 20})
    body = _body('supported')
    if summary is None:
        body['reasoning'] = {'summary': None}
    plan = prepare_request(body, api_key='synthetic-key', base_url='https://gateway.example/v1',
                           api_protocol='responses')
    wire = plan.body.wire_body()
    assert wire['reasoning']['summary'] == summary
    assert wire['reasoning']['effort'] == 'low'
    assert '_route_inference_profile' not in wire
    assert 'include' not in wire



def test_official_openai_connection_declares_summary_without_generic_gateway_inheritance():
    import json
    from pathlib import Path
    from types import SimpleNamespace

    from lib.model_routing.inference import candidate_inference_metadata
    from lib.model_routing.migration import plan_legacy_migration
    from lib.provider.provider_template_recipes import provider_from_template

    raw = json.loads((Path(__file__).parents[2] / 'lib/model_info/data/openai.json').read_text())
    provider = provider_from_template(raw, 'openai')
    plan = plan_legacy_migration({'providers': [provider]})
    assert not plan.blocking_issues
    doc = plan.document
    offering = next(row for row in doc['offerings'] if row.get('model', {}).get('model_id') == 'gpt-6.1-sol')
    deployment = next(row for row in doc['deployments'] if row['offering_id'] == offering['offering_id'])
    connection = next(row for row in doc['connections'] if row['connection_id'] == deployment['connection_id'])
    model = next(row for row in doc['models'] if row['model_id'] == 'gpt-6.1-sol')
    selected = candidate_inference_metadata(SimpleNamespace(
        model=model, offering=offering, connection=connection, deployment=deployment))['inference_profile']
    assert selected['features']['responses_reasoning_summary'] == 'supported'
    wire, _ = openai_body_to_responses({**_body(), '_route_inference_profile': selected})
    assert wire['reasoning']['summary'] == 'auto'
    # Transport capability is scoped to the official Connection, not the Model.
    assert 'responses_reasoning_summary' not in model.get('inference_profile', {}).get('features', {})
