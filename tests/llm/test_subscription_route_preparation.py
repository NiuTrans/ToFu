"""Subscription admission and saved reasoning choices reach the selected wire.

All DNS, transport and task observers are synthetic; no credentials or live
provider requests are needed to reproduce failures before provider ingress.
"""
from copy import deepcopy
import socket

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('active', [True, False])
@pytest.mark.parametrize('depth,mode', [('max', 'preset'), ('lowest', 'lowest'),
                                       ('off', 'off'), ('default', 'default')])
def test_saved_depth_is_not_overridden_by_missing_legacy_switch(active, depth, mode):
    from lib.conv_config import resolve_conv_config
    from lib.llm.reasoning import capture_intent
    from lib.tasks_pkg.model_config import _resolve_model_config

    selected = {'model': 'gpt-6-astra', 'thinkingDepth': depth}
    cfg = resolve_conv_config(
        conv_settings=selected, overrides=selected if active else {},
        server_defaults={'thinkingEnabled': False}, is_active=active)
    resolved = _resolve_model_config(cfg, 'synthetic-routing')
    intent = capture_intent(thinking_enabled=resolved['thinking_enabled'],
                            thinking_depth=resolved['thinking_depth'])
    assert intent['mode'] == mode
    if mode == 'preset':
        assert intent['value'] == depth


@pytest.mark.parametrize('active', [True, False])
def test_explicit_legacy_switch_still_disables_reasoning(active):
    from lib.conv_config import resolve_conv_config
    from lib.llm.reasoning import capture_intent

    selected = {'model': 'future-model', 'thinkingDepth': 'max',
                'thinkingEnabled': False}
    cfg = resolve_conv_config(conv_settings=selected,
                              overrides=selected if active else {}, is_active=active)
    assert capture_intent(thinking_enabled=cfg['thinkingEnabled'],
                          thinking_depth=cfg['thinkingDepth'])['mode'] == 'off'


@pytest.mark.parametrize('protocol', ['openai_responses', 'responses'])
def test_stored_catalog_ladder_has_responses_wire_binding(protocol):
    from lib.model_routing.inference import resolve_inference_profile
    from lib.llm.reasoning import capture_intent, compile_reasoning

    offering = {'inference_profile': {'reasoning': {
        'support': 'supported', 'supported_efforts': ['low', 'medium', 'max'],
        'default_effort': 'medium'}}}
    before = deepcopy(offering)
    profile = resolve_inference_profile(
        offering=offering, connection={'protocol': protocol})
    body = {'_reasoning_intent': capture_intent(thinking_depth='max')}
    compile_reasoning(body, 'unknown-catalog-model', inference_profile=profile)
    assert body['reasoning_effort'] == 'max'
    assert offering == before


def test_responses_protocol_does_not_invent_support_or_override_explicit_dialect():
    from lib.model_routing.inference import resolve_inference_profile

    connection = {'protocol': 'openai_responses'}
    assert resolve_inference_profile(connection=connection) == {}
    for fmt in ('none', 'thinking_type'):
        profile = resolve_inference_profile(connection=connection, offering={
            'inference_profile': {'reasoning': {'support': 'supported',
                                               'wire_format': fmt}}})
        assert profile['reasoning']['wire_format'] == fmt


@pytest.mark.parametrize('url,allowed', [
    ('https://chatgpt.com/backend-api/codex', True),
    ('https://api.anthropic.com/v1', True),
    ('https://chatgpt.com.evil.invalid/v1', False),
    ('https://untrusted.invalid/v1', False),
    ('http://chatgpt.com/v1', False),
    ('https://chatgpt.com:8443/v1', False),
    ('https://user@chatgpt.com/v1', False),
])
def test_subscription_dns_failure_defers_to_approved_egress_only(monkeypatch, url, allowed):
    from lib.byo_egress import is_egress_allowed

    monkeypatch.delenv('TOFU_BYO_ALLOW_HOSTS', raising=False)
    def no_local_dns(*a, **kw):
        raise socket.gaierror(socket.EAI_NONAME, 'synthetic DNS failure')
    monkeypatch.setattr(socket, 'getaddrinfo', no_local_dns)
    assert is_egress_allowed(url)[0] is allowed


def test_subscription_resolving_to_metadata_is_still_denied(monkeypatch):
    from lib.byo_egress import is_egress_allowed

    monkeypatch.delenv('TOFU_BYO_ALLOW_HOSTS', raising=False)
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('169.254.169.254', 443))])
    assert not is_egress_allowed('https://chatgpt.com/backend-api/codex')[0]


def test_local_fallback_failure_does_not_attempt_another_model(monkeypatch):
    from lib.llm_errors import LocalRequestPreparationError, PermissionError_
    from lib.tasks_pkg.llm_fallback import _call
    from tests.tasks_pkg.test_pool_rescue_fallback import _base_task, _patch_common, _set_gate

    calls = []
    error = LocalRequestPreparationError('synthetic reasoning incompatibility')
    def stream(task, body, **kw):
        calls.append(body['model'])
        if len(calls) == 1:
            raise PermissionError_('synthetic primary rejection', status_code=403)
        raise error
    _patch_common(monkeypatch, stream)
    gate = _set_gate(monkeypatch, has=True)
    messages = [{'role': 'user', 'content': 'synthetic'}]
    task = _base_task()
    result = _call._llm_call_with_fallback(
        task, {'model': 'gpt-6-astra', 'messages': messages},
        'gpt-6-astra', 0, 512, False, None, messages, 'low', False, {}, [])
    assert calls == ['gpt-6-astra', 'kimi-k3']
    assert not gate.calls
    assert result['finish_reason'] == 'error'
    assert task['error']['detail'] == str(error)


def test_zero_dispatch_attempts_do_not_report_retry_budget_as_requests(monkeypatch):
    from lib.llm_dispatch._api_budget import _raise_dispatch_exhausted
    from lib.error_envelope import from_exception

    monkeypatch.setattr('lib.key_stats.is_strict_billing_stop_admission', lambda: False)
    with pytest.raises(RuntimeError) as raised:
        _raise_dispatch_exhausted(None, max_retries=3, capability='text',
                                  prefer_model='gpt-6-astra', what='dispatch_stream')
    assert 'no upstream request was sent' in str(raised.value)
    assert from_exception(raised.value)['kind'] == 'no_slot'


def test_saved_max_reaches_selected_subscription_wire_when_local_dns_is_down(monkeypatch):
    import json
    from lib.conv_config import resolve_conv_config
    from lib.llm.body import build_body
    from lib.llm._sse_core import prepare_request
    from lib.llm_dispatch._api_stream_state import _adapt_stream_body_for_slot
    from lib.model_routing import (
        InMemoryModelRoutingRepository, NativeModelSelection, ModelRef,
        OwnerBoundary, RoutePolicy, mint_routed_slot_group, dispose_routed_slot_group,
    )
    from tests.llm_dispatch.test_dispatcher_model_routing_v2 import _document, _build

    document = _document()
    document['connections'][0].update(
        base_url='https://chatgpt.com/backend-api/codex', protocol='openai_responses')
    document['offerings'][0]['inference_profile'] = {'reasoning': {
        'support': 'supported', 'wire_format': 'reasoning_effort',
        'can_disable': False, 'supported_efforts': ['low', 'medium', 'max'],
        'default_effort': 'medium'}}
    dispatcher = _build(monkeypatch, document)
    monkeypatch.setattr('lib.llm_dispatch.factory.get_dispatcher', lambda: dispatcher)
    monkeypatch.setattr('lib.llm_dispatch.ephemeral._preflight_enabled', lambda: False)
    monkeypatch.delenv('TOFU_BYO_ALLOW_HOSTS', raising=False)
    def dns(host, *a, **kw):
        if host == 'chatgpt.com':
            raise socket.gaierror(socket.EAI_NONAME, 'synthetic local DNS failure')
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))]
    monkeypatch.setattr(socket, 'getaddrinfo', dns)
    monkeypatch.setattr('lib.token_counter.count_tokens', lambda *a, **kw: {'tokens': 20})
    monkeypatch.setattr('lib.desktop.egress.route_request', lambda *a, **kw: 'direct')
    repository = InMemoryModelRoutingRepository()
    owner = OwnerBoundary.create(1)
    repository.compare_and_swap(owner, document, expected_revision=0)
    group = mint_routed_slot_group(
        repository, owner, NativeModelSelection(ModelRef('creator', 'alpha'), None, ''),
        policy=RoutePolicy(required_capabilities=frozenset({'text', 'thinking'})))
    try:
        slot = group.primary.slot
        assert slot.routing_provider_id == 'provider-a'
        assert slot.model == 'alpha-wire-a'
        cfg = resolve_conv_config(
            conv_settings={'model': 'alpha', 'thinkingDepth': 'max'}, is_active=False)
        body = build_body('alpha', [{'role': 'user', 'content': 'synthetic'}],
                          thinking_enabled=cfg['thinkingEnabled'],
                          thinking_depth=cfg['thinkingDepth'], defer_reasoning=True)
        bound = _adapt_stream_body_for_slot(
            slot, body, True, tools=None, max_tokens=4096, temperature=1.0,
            thinking_enabled=cfg['thinkingEnabled'], preset='alpha', effort=None)
        plan = prepare_request(bound, base_url=slot.base_url,
                               api_key='synthetic', api_protocol=slot.protocol)
        wire = json.loads(plan.body.payload)
        assert plan.url.endswith('/responses')
        assert wire['model'] == 'alpha-wire-a'
        assert wire['reasoning']['effort'] == 'max'
        assert '_reasoning_intent' not in wire
    finally:
        dispose_routed_slot_group(group)
    assert all(handle.disposed for handle in group.handles)
