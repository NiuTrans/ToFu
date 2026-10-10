"""Public extensions require a declared selected-route binding, never a name."""
from __future__ import annotations

import pytest

from lib.llm.route_features import route_feature
from lib.llm_dispatch.compaction_policy import native_compaction_mode_for_route
from lib.tools.gateway import resolve_tool_search_backend
from lib.tools.programmatic import resolve_programmatic_backend
from lib.swarm.routing import resolve_multi_agent_backend

pytestmark = pytest.mark.unit


def _profile(*names):
    return {'features': {name: 'supported' for name in names}}


@pytest.mark.parametrize('model', ['future-unregistered-release', 'gpt-6-unrecognized', 'provider-arbitrary-id'])
def test_declared_unknown_model_extensions_resolve_without_name_checks(model):
    profile = _profile('responses_tool_search', 'responses_programmatic',
                       'responses_multi_agent', 'responses_compaction')
    kwargs = {'model': model, 'protocol': 'responses', 'inference_profile': profile,
              'base_url': 'https://gateway.example/v1', 'responses_profile': 'compatible'}
    assert resolve_tool_search_backend('auto', **kwargs) == 'native_openai'
    assert resolve_programmatic_backend('on', **kwargs) == 'native_openai'
    assert resolve_multi_agent_backend('read_only', **kwargs) == 'native_openai'
    assert native_compaction_mode_for_route(**kwargs) == 'openai_responses'


@pytest.mark.parametrize('profile', [{}, {'features': {}}, {'features': {
    'responses_tool_search': 'unknown', 'responses_programmatic': 'unsupported',
    'responses_multi_agent': 'unsupported', 'responses_compaction': 'unknown'}}])
def test_explicit_profile_never_falls_back_to_old_name_inference(profile):
    kwargs = {'model': 'gpt-5.6-sol', 'protocol': 'responses', 'inference_profile': profile,
              'base_url': 'https://api.openai.com/v1', 'responses_profile': 'openai'}
    assert resolve_tool_search_backend('auto', **kwargs) == 'local'
    assert resolve_programmatic_backend('on', **kwargs) == 'local'
    assert resolve_multi_agent_backend('read_only', local_swarm_available=True, **kwargs) == 'local_swarm'
    assert native_compaction_mode_for_route(**kwargs) == ''


def test_declared_features_cannot_cross_protocol_or_subscription_boundaries():
    profile = _profile('responses_compaction', 'responses_programmatic',
                       'responses_multi_agent', 'responses_websocket', 'responses_tool_search')
    for name in profile['features']:
        assert route_feature(profile, name, protocol='anthropic') is False
        assert route_feature(profile, name, protocol='responses', oauth='codex') is False
    assert route_feature(None, 'responses_websocket', protocol='responses') is None
    assert route_feature(profile, 'uninstalled_future_feature', protocol='responses') is False


@pytest.mark.parametrize('feature', ['responses_reasoning_replay', 'responses_prompt_cache'])
def test_codex_installed_stateful_bindings_preserve_explicit_denial(feature):
    assert route_feature({'reasoning': {'support': 'supported'}}, feature,
                         protocol='responses', oauth='codex') is True
    for declaration in ('unsupported', 'unknown', None, False, '', {}):
        assert route_feature({'features': {feature: declaration}}, feature,
                             protocol='responses', oauth='codex') is False
    assert route_feature({}, feature, protocol='responses', oauth='claude') is False
    assert route_feature({}, feature, protocol='responses') is False


def test_explicit_anthropic_compatible_gateway_can_declare_native_extensions():
    profile = _profile('anthropic_compaction', 'anthropic_tool_search')
    kwargs = {'protocol': 'anthropic', 'model': 'any-model',
              'base_url': 'https://gateway.example/v1', 'inference_profile': profile}
    assert native_compaction_mode_for_route(**kwargs) == 'anthropic_messages'
    assert resolve_tool_search_backend('auto', **kwargs) == 'native_anthropic'
    assert native_compaction_mode_for_route(**kwargs, oauth='claude') == ''


def test_support_is_not_task_authorization_or_requested_activation():
    profile = _profile('responses_multi_agent', 'responses_tool_search', 'responses_programmatic')
    kwargs = {'protocol': 'responses', 'inference_profile': profile}
    assert resolve_tool_search_backend('off', **kwargs) == 'full'
    assert resolve_tool_search_backend('local', **kwargs) == 'local'
    assert resolve_programmatic_backend('off', **kwargs) == 'off'
    assert resolve_programmatic_backend('on', eligible_present=False, **kwargs) == 'off'
    assert resolve_multi_agent_backend('off', **kwargs) == 'off'


def test_declared_websocket_reaches_sealed_request_for_an_unknown_model(monkeypatch):
    from lib.llm._sse_core import prepare_request
    monkeypatch.setattr('lib.token_counter.count_tokens', lambda *a, **kw: {'tokens': 20})
    body = {'model': 'future-unrecognized-model', '_task_id': 'synthetic',
            '_responses_transport': 'websocket',
            '_route_inference_profile': _profile('responses_websocket'),
            'messages': [{'role': 'user', 'content': 'hi'}]}
    plan = prepare_request(body, base_url='https://gateway.example/v1',
                           api_protocol='responses')
    assert plan.responses_transport == 'websocket'
    assert '_route_inference_profile' not in plan.body.wire_body()
    assert 'context_management' not in plan.body.wire_body()
