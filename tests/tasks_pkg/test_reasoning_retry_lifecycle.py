"""Reasoning retries keep intent; only a selected route may authorize its wire."""
from copy import deepcopy
from types import SimpleNamespace
import json

import pytest

from lib.llm.body import build_body
from lib.llm.reasoning import ReasoningContractError
from lib.llm._sse_core import prepare_request
from lib.llm_dispatch._api_stream_state import _adapt_stream_body_for_slot

pytestmark = pytest.mark.unit

PROFILE = {'reasoning': {'support': 'supported', 'wire_format': 'thinking_type',
                         'can_disable': True, 'min_budget_tokens': 1024,
                         'max_budget_tokens': 8192},
           'sampling': {'temperature': 'omit'}}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr('lib.token_counter.count_tokens', lambda *a, **k: {'tokens': 20})
    monkeypatch.setattr('lib.tasks_pkg.compaction._tokens.resolve_model_context_limit',
                        lambda *a, **k: 200000)
    monkeypatch.setattr('lib.desktop.egress.route_request', lambda *a, **k: 'direct')
    monkeypatch.setattr('lib.tasks_pkg.compaction.api.reactive_compact', lambda *a, **k: None)
    monkeypatch.setattr('lib.tasks_pkg.context_composer.checklist.restore_checklist_context',
                        lambda *a, **k: None)


def source_body():
    return build_body('future-unrelated-name', [{'role': 'user', 'content': 'hi'}],
                      extra={'thinking': {'type': 'enabled', 'budget_tokens': 2048}},
                      inference_profile=PROFILE)


@pytest.mark.parametrize(('config', 'expected_mode', 'expected_value'), [
    ({}, 'default', None),
    ({'thinkingDepth': 'max'}, 'preset', 'max'),
    ({'thinkingEnabled': True}, 'enabled', 'enabled'),
    ({'thinkingEnabled': False}, 'off', None),
])
def test_task_ingress_preserves_absent_and_explicit_reasoning(config, expected_mode, expected_value):
    from lib.tasks_pkg.entry import build_chat_config
    from lib.tasks_pkg.model_config import _resolve_model_config
    cfg = build_chat_config(model='opaque-request-id', config=config)
    resolved = _resolve_model_config(cfg, 'intent-test')
    body = build_body(resolved['model'], [{'role': 'user', 'content': 'hi'}],
        thinking_enabled=resolved['thinking_enabled'],
        thinking_depth=resolved['thinking_depth'], preset=resolved['preset'],
        defer_reasoning=True)
    assert body['_reasoning_intent']['mode'] == expected_mode
    assert body['_reasoning_intent']['value'] == expected_value


def test_empty_conversation_does_not_manufacture_off():
    from lib.conv_config import resolve_conv_config
    resolved = resolve_conv_config(overrides={'model': 'opaque-request-id'})
    assert resolved['thinkingEnabled'] is None


def bind(body, profile=PROFILE):
    slot = SimpleNamespace(model=body['model'], protocol='anthropic', provider_id='',
                           thinking_format='', inference_profile=profile)
    return _adapt_stream_body_for_slot(
        slot, body, True, tools=None, max_tokens=4096, temperature=1.0,
        thinking_enabled=True, preset='medium', effort=None)


def test_same_model_reactive_rebuild_keeps_explicit_profile_and_budget():
    from lib.tasks_pkg.llm_fallback import _call
    from lib.llm.llm_errors import PromptTooLongError
    body = source_body()
    before = deepcopy(body)
    rebuilt = _call._prepare_reactive_retry_body(
        {'id': 'synthetic', 'config': {}}, body, body['model'], body['messages'],
        [], 'medium', True, PromptTooLongError('synthetic'), 'synthetic')
    assert rebuilt is not None
    assert rebuilt['_route_inference_profile'] == PROFILE
    assert rebuilt['_reasoning_intent'] == before['_reasoning_intent']
    plan = prepare_request(bind(rebuilt), base_url='https://example.invalid/v1',
                           api_protocol='anthropic')
    wire = json.loads(plan.body.payload)
    assert wire['thinking'] == {'type': 'enabled', 'budget_tokens': 2048}
    assert 'temperature' not in wire
    assert body == before


@pytest.mark.parametrize('changed_model', [False, True])
def test_reactive_pending_or_changed_destination_cannot_inherit_compilation(changed_model):
    from lib.tasks_pkg.llm_fallback import _call
    from lib.llm.llm_errors import PromptTooLongError
    body = source_body()
    if not changed_model:
        body = build_body(body['model'], body['messages'],
                          reasoning_intent=body['_reasoning_intent'], defer_reasoning=True)
    before = deepcopy(body)
    model = 'different-destination' if changed_model else body['model']
    retry = _call._prepare_reactive_retry_body(
        {'id': 'synthetic', 'config': {}}, body, model, body['messages'],
        [], 'medium', True, PromptTooLongError('synthetic'), 'synthetic')
    assert retry is not None
    assert retry['_reasoning_pending'] is True
    assert '_route_inference_profile' not in retry
    assert retry['_reasoning_intent'] == body['_reasoning_intent']
    with pytest.raises(ReasoningContractError, match='unbound_reasoning'):
        prepare_request(retry, base_url='https://example.invalid/v1')
    assert body == before


def test_cross_model_rebuild_waits_for_destination_then_emits_exact_budget():
    source = source_body()
    staged = build_body('different-future-model', source['messages'],
                        reasoning_intent=source['_reasoning_intent'], defer_reasoning=True)
    assert staged['_reasoning_pending'] is True
    assert '_route_inference_profile' not in staged
    assert 'thinking' not in staged
    with pytest.raises(ReasoningContractError, match='unbound_reasoning'):
        prepare_request(staged, base_url='https://example.invalid/v1', api_protocol='anthropic')
    bound = bind(staged)
    assert '_reasoning_pending' not in bound
    wire = prepare_request(bound, base_url='https://example.invalid/v1', api_protocol='anthropic').body
    assert wire['thinking']['budget_tokens'] == 2048
    assert staged['_reasoning_pending'] is True
    assert staged['_reasoning_intent'] == source['_reasoning_intent']


def test_incompatible_destination_does_not_borrow_source_capability_or_mutate_intent():
    source = source_body()
    staged = build_body('different-future-model', source['messages'],
                        reasoning_intent=source['_reasoning_intent'], defer_reasoning=True)
    before = deepcopy(staged)
    with pytest.raises(ReasoningContractError, match='unsupported_reasoning'):
        bind(staged, {'reasoning': {'support': 'unknown'}})
    assert staged == before


@pytest.mark.parametrize('depth,mode', [('default', 'default'), ('lowest', 'lowest'),
                                       ('off', 'off'), ('high', 'preset')])
def test_first_task_round_stages_choice_without_name_based_compilation(monkeypatch, depth, mode):
    from lib.tasks_pkg.orchestrator import _round_request_prep as prep
    from lib.llm import _reasoning_legacy

    monkeypatch.setattr(_reasoning_legacy, 'apply_legacy',
                        lambda *a, **k: pytest.fail('task intent compiled before route selection'))
    monkeypatch.setattr(prep.orchestrator_ports, 'build_request_body', build_body)
    monkeypatch.setattr(prep, 'emit_messages_snapshot_event', lambda *a, **k: None)
    monkeypatch.setattr('lib.context_telemetry.capture_round_context', lambda *a, **k: None)
    task = {'id': 'synthetic', 'convId': 'synthetic-conversation', '_userId': 1, 'config': {}}
    state = SimpleNamespace(model='future-unrelated-name', preset='medium', thinking_enabled=True)
    _, body = prep.build_round_request(
        task, state, [{'role': 'user', 'content': 'hello'}], [],
        round_num=0, tid='synthetic', thinking_depth=depth, temperature=0.7,
        max_tokens=4096, response_format=None)
    assert body['_reasoning_pending'] is True
    assert body['_reasoning_intent']['mode'] == mode
    assert '_route_inference_profile' not in body
    assert not {'thinking', 'reasoning_effort', 'effort', 'enable_thinking'} & body.keys()
    with pytest.raises(ReasoningContractError, match='unbound_reasoning'):
        prepare_request(body, base_url='https://example.invalid/v1')
    profile = {'reasoning': {'support': 'supported', 'wire_format': 'reasoning_effort',
                            'can_disable': True, 'supported_efforts': ['none', 'low', 'high']}}
    bound = bind(body, profile)
    assert bound.get('reasoning_effort') == {
        'default': None, 'lowest': 'low', 'off': 'none', 'high': 'high'}[depth]
    assert body['_reasoning_pending'] is True
