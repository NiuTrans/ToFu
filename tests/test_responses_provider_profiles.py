"""Responses core compatibility and public-OpenAI feature boundaries."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]


def _tools(n=20):
    return [{
        'type': 'function',
        'function': {
            'name': f'tool_{i}', 'description': f'tool {i}',
            'parameters': {'type': 'object', 'properties': {}},
        },
    } for i in range(n)]


def test_profile_auto_is_fail_closed_except_canonical_openai_host():
    from lib.llm.responses_features import (
        normalize_responses_feature_profile as normalize,
    )

    assert normalize(protocol='responses',
                     base_url='https://api.openai.com/v1') == 'openai'
    assert normalize(protocol='responses',
                     base_url='https://gateway.example/v1') == 'compatible'
    assert normalize('openai', protocol='responses',
                     base_url='https://gateway.example/v1') == 'openai'
    assert normalize('openai', protocol='responses',
                     base_url='https://chatgpt.com/backend-api/codex',
                     oauth='codex') == 'codex'
    assert normalize('openai', protocol='openai',
                     base_url='https://api.openai.com/v1') == ''


def test_face_resolution_carries_effective_responses_profile():
    from lib.llm_dispatch.provider_face import resolve_face

    official = resolve_face({
        'id': 'openai', 'protocol': 'responses',
        'base_url': 'https://api.openai.com/v1',
    }, {'model_id': 'gpt-5.6-sol'}, dual_face_hosts=set())
    compatible = resolve_face({
        'id': 'gateway', 'protocol': 'responses',
        'base_url': 'https://gateway.example/v1',
    }, {'model_id': 'gpt-5.6-sol'}, dual_face_hosts=set())
    opted_in = resolve_face({
        'id': 'gateway', 'protocol': 'responses',
        'responses_profile': 'openai',
        'base_url': 'https://gateway.example/v1',
    }, {'model_id': 'gpt-5.6-sol'}, dual_face_hosts=set())

    assert official.responses_profile == 'openai'
    assert compatible.responses_profile == 'compatible'
    assert opted_in.responses_profile == 'openai'


def test_slot_adaptation_stamps_provider_profile_on_every_round():
    from lib.llm_dispatch.api import _adapt_stream_body_for_slot
    from lib.llm_dispatch.slot import Slot

    slot = Slot(
        key_name='k', api_key='secret', model='gpt-5.6-sol',
        capabilities={'text'}, protocol='responses',
        responses_profile='compatible')
    body = _adapt_stream_body_for_slot(
        slot, {'model': 'old',
               'messages': [{'role': 'user', 'content': 'hi'}]}, True,
        tools=None, max_tokens=64, temperature=0,
        thinking_enabled=False, preset='low', effort=None)

    assert body['_responses_feature_profile'] == 'compatible'


def test_dispatch_usage_reports_effective_profile_for_canary_diagnostics():
    from types import SimpleNamespace

    from lib.llm_dispatch.api import _settle_stream_result
    from lib.llm_dispatch.slot import Slot

    slot = Slot(
        key_name='k', api_key='secret', model='gpt-5.6-sol',
        capabilities={'text'}, provider_id='gateway',
        protocol='responses', responses_profile='compatible')
    usage = {'completion_tokens': 1}
    _settle_stream_result(
        slot, usage, latency=12, ttft=3,
        state=SimpleNamespace(hard_attempts=0, _429_count=0),
        cache_conv_id='', tag='[test]')

    assert usage['_dispatch']['protocol'] == 'responses'
    assert usage['_dispatch']['responses_profile'] == 'compatible'


def test_compatible_profile_blocks_all_public_only_fields_without_tools_loss():
    from lib.llm.responses_outbound import openai_body_to_responses

    wire, _ = openai_body_to_responses({
        'model': 'gpt-5.6-sol',
        '_responses_feature_profile': 'compatible',
        '_tool_search_mode': 'auto',
        '_programmatic_tool_calling': 'auto',
        '_responses_transport': 'websocket',
        '_reasoning_mode': 'pro', '_text_verbosity': 'high',
        '_image_detail': 'original', '_multi_agent_mode': 'read_only',
        '_working_set_tokens': 128000, '_conv_id': 'private-conv',
        '_safety_identifier': 'private-user',
        'messages': [{'role': 'user', 'content': 'work'}],
        'tools': _tools(),
    })

    assert {tool['name'] for tool in wire['tools']} == {
        f'tool_{i}' for i in range(20)}
    assert all(tool['type'] == 'function' for tool in wire['tools'])
    for key in ('multi_agent', 'context_management', 'prompt_cache_key',
                'safety_identifier', 'include'):
        assert key not in wire
    assert 'reasoning' not in wire
    assert 'text' not in wire


def test_websocket_requires_openai_feature_profile():
    from lib.llm._sse_core import prepare_request

    common = {
        'model': 'gpt-5.6-sol', '_task_id': 'task-profile',
        '_responses_transport': 'websocket',
        'messages': [{'role': 'user', 'content': 'hi'}],
    }
    compatible = prepare_request(
        {**common, '_responses_feature_profile': 'compatible'},
        api_key='k', base_url='https://gateway.example/v1',
        api_protocol='responses')
    openai = prepare_request(
        {**common, '_responses_feature_profile': 'openai'},
        api_key='k', base_url='https://api.openai.com/v1',
        api_protocol='responses')

    assert compatible.responses_transport == 'sse'
    assert openai.responses_transport == 'websocket'


def test_frontend_controls_and_runtime_passthrough_are_two_sided():
    panel = (ROOT / 'static/settings_panels/advanced.html').read_text()
    runtime = (ROOT / 'frontend/src/runtime/app-runtime.js').read_text()
    settings = lifecycle = provider = runtime
    route = (ROOT / 'routes/config.py').read_text()

    for token in ('settingToolSearch', 'settingProgrammaticCalling',
                  'settingResponsesTransport', 'settingResponsesMultiAgent'):
        assert token in panel and token in settings
    assert 'tool-execution-policy' in panel
    css = (ROOT / 'static/settings.css').read_text()
    assert '.tool-discovery-card' in css
    assert "executionScope: 'available'" in settings
    assert 'settingToolExecutionScope' not in panel
    toolbar_css = (ROOT / 'static/styles.css').read_text()
    main_js = runtime
    i18n = ''.join(
        path.read_text() for path in
        (ROOT / 'frontend/src/i18n/locales').glob('*.json'))
    assert '.submenu-item.discoverable::after' not in toolbar_css
    assert 'toolbar.toolExposureSearchable' not in main_js + i18n
    assert '可直呼' not in main_js + i18n
    assert '.submenu-item.tool-unavailable::after' in toolbar_css
    assert '_paintToolExposureState' in main_js
    for block in ('cache', 'tools', 'responses', 'compaction'):
        assert f'{block}: Object.assign' in lifecycle
    assert 'responses_profile' in provider
    assert "'responses_profile': r.responses_profile" in route


def test_conv_resolver_preserves_nested_experiment_blocks():
    from lib.conv_config import resolve_conv_config

    out = resolve_conv_config(
        overrides={
            'model': 'gpt-5.6-sol',
            'tools': {'toolSearch': 'auto'},
            'responses': {'transport': 'websocket'},
        },
        is_active=True)
    assert out['tools'] == {'toolSearch': 'auto'}
    assert out['responses'] == {'transport': 'websocket'}
