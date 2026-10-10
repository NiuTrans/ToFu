"""Executable intent-to-wire parity contracts (no network)."""
import copy
import pytest
from lib.llm.body import build_body
from lib.llm.reasoning import ReasoningContractError, compile_reasoning
from lib.llm_dispatch._api_stream_state import _readjust_thinking_params

pytestmark = pytest.mark.unit


def test_conflicting_native_efforts_fail_atomically_at_ingress():
    body = {'effort': 'low', 'reasoning_effort': 'high'}
    original = copy.deepcopy(body)
    with pytest.raises(ReasoningContractError, match='reasoning_effort_conflict'):
        compile_reasoning(body, 'opaque', inference_profile=profile())
    assert body == original


def profile(fmt='reasoning_effort'):
    return {'reasoning': {'support': 'supported', 'wire_format': fmt,
                         'can_disable': True, 'supported_efforts': ['none', 'minimal', 'high', 'max'],
                         'disabled_effort': 'none', 'default_effort': 'minimal',
                         'effort_map': {'high': 'max'}}}


# A bare intent (no depth/effort choice) is mode='default': on the
# reasoning_effort wire the DECLARED default rung is emitted explicitly
# (here 'minimal') — a no-op for destinations honoring their documented
# default, a repair for destinations whose implicit default diverges.
@pytest.mark.parametrize('kwargs,expected', [({}, 'minimal'), ({'effort': 'high'}, 'high'),
    ({'thinking_depth': 'high'}, 'high'), ({'thinking_depth': 'lowest'}, 'minimal'),
    ({'thinking_depth': 'off'}, 'none')])
def test_future_model_exact_preset_default_off_lowest(kwargs, expected):
    p = profile()
    body = build_body('future-unrelated-name', [{'role': 'user', 'content': 'hi'}],
                      inference_profile=p, **kwargs)
    original = copy.deepcopy(body['_reasoning_intent'])
    assert body.get('reasoning_effort') == expected
    _readjust_thinking_params(body, 'another-future-name', '', p)
    assert body.get('reasoning_effort') == expected
    assert body['_reasoning_intent'] == original


def test_native_none_is_off_minimal_is_exact_and_template_cleaned():
    p = profile('enable_thinking')
    for effort, enabled in [('none', False), ('minimal', True)]:
        body = {'reasoning_effort': effort, 'chat_template_kwargs': {'unrelated': 7}}
        compile_reasoning(body, 'qwen', inference_profile=p)
        assert body['enable_thinking'] is enabled
        assert body['chat_template_kwargs'] == {'unrelated': 7}
    body = {'chat_template_kwargs': {'enable_thinking': False, 'unrelated': 7}}
    compile_reasoning(body, 'future', inference_profile=profile())
    assert body['reasoning_effort'] == 'none'
    assert body['chat_template_kwargs'] == {'unrelated': 7}


def test_sampling_restored_and_source_unmodified():
    extra = {'top_p': .4, 'chat_template_kwargs': {'unrelated': 7}}
    p = profile(); p['sampling'] = {'omit': ['temperature', 'top_p']}
    body = build_body('future', [{'role': 'user', 'content': 'hi'}],
                      temperature=.2, extra=extra, inference_profile=p)
    assert 'temperature' not in body
    compile_reasoning(body, 'destination', inference_profile=profile())
    assert body['temperature'] == .2 and body['top_p'] == .4
    assert extra['chat_template_kwargs'] == {'unrelated': 7}


def test_off_degrades_to_enabled_when_cannot_disable_kimi_code():
    # A thinking_type model with can_disable False and no effort ladder cannot
    # honor Off; it must degrade to an enabled control rather than 500 the turn.
    p = profile('thinking_type')
    p['reasoning']['can_disable'] = False
    body = build_body('kimi-k2.7-code', [{'role': 'user', 'content': 'hi'}],
                      thinking_depth='off', inference_profile=p)
    assert body['thinking']['type'] == 'enabled'
    assert body['_reasoning_resolution']['requested'] == 'off'
    assert body['_reasoning_resolution']['reason'] == \
        'disable_unavailable_fell_back_to_lowest'


def _kimi_k3_profile():
    """Thinking cannot be disabled; cheapest legal rung is ``low``."""
    return {'reasoning': {'support': 'supported', 'wire_format': 'reasoning_effort',
                          'can_disable': False,
                          'supported_efforts': ['low', 'high', 'max'],
                          'default_effort': 'max',
                          'effort_map': {'medium': 'high', 'xhigh': 'max'}}}


# Reproduces the reported whole-turn 500: a *default* ``thinkingEnabled=False``
# that leaked in (no depth, strict=False) was compiled as an unconditional off
# against kimi-k3 (can_disable false) and raised ``cannot_disable``. A default
# is not an explicit choice, so it must degrade to the cheapest legal rung
# instead of failing the turn.
def test_default_off_degrades_to_lowest_on_cannot_disable():
    body = build_body('kimi-k3', [{'role': 'user', 'content': 'hi'}],
                      thinking_enabled=False, inference_profile=_kimi_k3_profile())
    assert body['reasoning_effort'] == 'low'
    assert body['_reasoning_resolution']['reason'] == \
        'disable_unavailable_fell_back_to_lowest'
    assert body['_reasoning_resolution']['requested'] == 'off'
    assert body['_reasoning_resolution']['effective'] == 'lowest'


# 2026-09-22 incident: kimi-k3 pinned to the Anthropic face (2026-09-10)
# streams little/no thinking when the wire carries no reasoning control,
# while the composer stores 'default' for the "follow the model default"
# slider position and the declared default rung is max. A default on the
# effort-ladder wire must be emitted as the declared default rung.
def test_default_emits_declared_default_effort_kimi_k3():
    p = _kimi_k3_profile()
    # Mirror the registered K3 sampling contract: temperature is fixed
    # server-side, so the wire omits it (2026-09-22 raw-archive evidence).
    p['sampling'] = {'omit': ['temperature', 'top_p',
                              'presence_penalty', 'frequency_penalty']}
    body = build_body('kimi-k3', [{'role': 'user', 'content': 'hi'}],
                      thinking_depth='default', inference_profile=p)
    assert body['reasoning_effort'] == 'max'
    assert 'temperature' not in body  # K3 sampling stays omitted
    assert body['_reasoning_resolution']['requested'] == 'default'
    assert body['_reasoning_resolution']['effective'] == 'default'


# The exact production shape from the incident: resolve_conv_config derives
# thinkingEnabled=False for a stored 'default' depth; the intent still
# captures as mode='default' and must emit the declared default rung.
def test_legacy_toggle_false_with_default_depth_emits_default():
    body = build_body('kimi-k3', [{'role': 'user', 'content': 'hi'}],
                      thinking_enabled=False, thinking_depth='default',
                      inference_profile=_kimi_k3_profile())
    assert body['reasoning_effort'] == 'max'
    assert body['_reasoning_resolution']['requested'] == 'default'


# A default intent re-bound to a different destination emits THAT
# destination's declared default, never the previous attempt's rung.
def test_default_retarget_emits_new_destination_default():
    p = profile()
    body = build_body('future-a', [{'role': 'user', 'content': 'hi'}],
                      thinking_depth='default', inference_profile=p)
    assert body['reasoning_effort'] == 'minimal'
    original = copy.deepcopy(body['_reasoning_intent'])
    _readjust_thinking_params(body, 'kimi-k3', '', _kimi_k3_profile())
    assert body['reasoning_effort'] == 'max'
    assert body['_reasoning_intent'] == original


# Without a declared default the default keeps omission semantics.
def test_default_without_declared_default_still_omits():
    p = _kimi_k3_profile()
    del p['reasoning']['default_effort']
    body = build_body('kimi-k3', [{'role': 'user', 'content': 'hi'}],
                      thinking_depth='default', inference_profile=p)
    assert 'reasoning_effort' not in body


# Other wire formats keep omission semantics: a default must not become an
# explicit thinking control (Claude adaptive / K2.7 keep-all shapes).
def test_default_still_omits_on_thinking_type():
    p = {'reasoning': {'support': 'supported', 'wire_format': 'thinking_type',
                       'can_disable': False}}
    body = build_body('kimi-k2.7-code', [{'role': 'user', 'content': 'hi'}],
                      thinking_depth='default', inference_profile=p)
    assert 'thinking' not in body
    assert 'reasoning_effort' not in body


# An explicit product off (depth 'off', the slider's choice) is a product
# intent, not an API guarantee: on a model that cannot disable it must degrade
# to the cheapest legal rung rather than fail the whole turn with a 500.
def test_explicit_off_degrades_to_lowest_on_cannot_disable_kimi_k3():
    body = build_body('kimi-k3', [{'role': 'user', 'content': 'hi'}],
                      thinking_depth='off', inference_profile=_kimi_k3_profile())
    assert body['reasoning_effort'] == 'low'
    assert body['_reasoning_resolution']['requested'] == 'off'
    assert body['_reasoning_resolution']['reason'] == \
        'disable_unavailable_fell_back_to_lowest'


# The legacy toggle (thinkingEnabled=False + a depth) is also a product off;
# it too degrades on a model that cannot disable. Capture-side,
# test_subscription_route_preparation.test_explicit_legacy_switch_still_disables_reasoning
# still pins that such a payload captures as mode='off' — only compilation
# degrades it.
def test_explicit_legacy_off_degrades_to_lowest_on_cannot_disable():
    body = build_body('kimi-k3', [{'role': 'user', 'content': 'hi'}],
                      thinking_enabled=False, thinking_depth='max',
                      inference_profile=_kimi_k3_profile())
    assert body['reasoning_effort'] == 'low'
    assert body['_reasoning_resolution']['requested'] == 'off'


# A thinking_type model that cannot disable and has no effort ladder still
# degrades: a default off becomes an enabled thinking block (no rung needed)
# rather than a cannot_disable failure.
def test_default_off_degrades_to_enabled_on_thinking_type():
    p = {'reasoning': {'support': 'supported', 'wire_format': 'thinking_type',
                       'can_disable': False, 'preserve_history': 'keep_all',
                       'thinking_extra': {'keep': 'all'}}}
    body = build_body('kimi-k2.7-code', [{'role': 'user', 'content': 'hi'}],
                      thinking_enabled=False, inference_profile=p)
    assert body['thinking']['type'] == 'enabled'
    assert body['_reasoning_resolution']['reason'] == \
        'disable_unavailable_fell_back_to_lowest'


# Control: an off on a model that CAN disable must stay off.
def test_default_off_still_disables_when_allowed():
    body = build_body('claude-sonnet-5', [{'role': 'user', 'content': 'hi'}],
                      thinking_enabled=False, inference_profile=profile())
    assert body['reasoning_effort'] == 'none'
    assert body['_reasoning_resolution']['effective'] == 'off'


def test_plugin_failure_is_loud(monkeypatch):
    from lib.llm_dispatch import provider_registry
    class Plugin:
        def apply_build(self, *args, **kwargs):
            raise RuntimeError('private provider data')
    monkeypatch.setattr(provider_registry, 'get_dialect', lambda key: Plugin())
    with pytest.raises(ReasoningContractError, match='plugin_failure') as error:
        build_body('future', [{'role': 'user', 'content': 'hi'}],
                   thinking_depth='enabled', inference_profile=profile('custom'))
    assert 'private' not in str(error.value)


def test_strict_source_survives_extras_retarget_and_sampling_restore():
    p = profile('chat_template_kwargs')
    p['sampling'] = {'temperature': 'omit', 'omit_params': ['top_p']}
    body = build_body('unknown-engine', [{'role': 'user', 'content': 'hi'}],
                      thinking_depth='off', temperature=.25, inference_profile=p,
                      extra={'top_p': .8, 'enable_thinking': True,
                             'chat_template_kwargs': {'enable_thinking': True, 'other': 9},
                             'output_config': {'format': {'type': 'json'}}})
    assert body['chat_template_kwargs'] == {'enable_thinking': False, 'other': 9}
    assert 'temperature' not in body
    rebuilt = build_body('unknown-engine', body['messages'],
                         reasoning_intent=body['_reasoning_intent'], inference_profile=profile())
    assert rebuilt['reasoning_effort'] == 'none'
    assert rebuilt['temperature'] == .25
    compile_reasoning(body, 'unknown-engine', inference_profile=profile())
    assert body['reasoning_effort'] == 'none'
    assert body['temperature'] == .25 and body['top_p'] == .8
    assert body['output_config']['format'] == {'type': 'json'}
    assert body['chat_template_kwargs'] == {'other': 9}


@pytest.mark.parametrize('support', ['unknown', 'unsupported'])
def test_explicit_effort_rejects_unsupported_failover(support):
    body = build_body('future', [{'role': 'user', 'content': 'hi'}],
                      effort='minimal', inference_profile=profile())
    p = profile(); p['reasoning']['support'] = support
    with pytest.raises(ReasoningContractError, match='unsupported_reasoning'):
        _readjust_thinking_params(body, 'same-model', '', p)


def test_none_is_off_unknown_exact_raises_and_unknown_preset_clamps():
    body = build_body('future', [{'role': 'user', 'content': 'hi'}],
                      effort='none', inference_profile=profile())
    assert body['_reasoning_intent']['mode'] == 'off'
    # An exact native effort is a precise API guarantee: never defaulted.
    with pytest.raises(ReasoningContractError, match='unsupported_effort'):
        build_body('future', body['messages'], effort='imaginary', inference_profile=profile())
    # A preset depth is a product intent: an unrepresentable name clamps to the
    # nearest representable rung (here the destination default), recorded.
    clamped = build_body('future', body['messages'], thinking_depth='imaginary',
                         inference_profile=profile())
    assert clamped['reasoning_effort'] == 'minimal'
    assert clamped['_reasoning_resolution']['reason'] == 'preset_clamped_to_representable'


@pytest.mark.parametrize('native', [
    {'reasoning': {'effort': 'minimal', 'summary': 'auto'}},
    {'output_config': {'effort': 'minimal', 'format': {'type': 'json'}}},
])
def test_nested_native_effort_is_ingress_only_and_survives_retarget(native):
    body = copy.deepcopy(native)
    compile_reasoning(body, 'future-first', inference_profile=profile())
    assert body['_reasoning_intent']['value'] == 'minimal'
    compile_reasoning(body, 'future-second', inference_profile=profile('thinking_type'))
    assert body['reasoning_effort'] == 'minimal'
    for key in ('reasoning', 'output_config'):
        assert 'effort' not in body.get(key, {})
        if key in native:
            assert body[key] == {k: v for k, v in native[key].items() if k != 'effort'}


@pytest.mark.parametrize('invalid', [
    {'reasoning': []}, {'reasoning': {'support': ['supported']}},
    {'reasoning': {'wire_format': {'bad': 'SECRET'}}},
])
def test_invalid_profile_is_typed_redacted_and_atomic(invalid):
    body = {'messages': [{'role': 'user', 'content': 'original'}], 'temperature': .2}
    original = copy.deepcopy(body)
    with pytest.raises(ReasoningContractError, match='invalid_profile') as error:
        compile_reasoning(body, 'new-model', inference_profile=invalid)
    assert 'SECRET' not in str(error.value)
    assert body == original


@pytest.mark.parametrize('field,value', [
    ('sampling', {'model': 'unauthorized-route'}),
    ('sampling', {'temperature': float('nan')}),
    ('sampling', {'stop': ['x'] * 65}),
    ('value', ['SECRET']), ('mode', ['SECRET']), ('version', True),
])
def test_intent_cannot_overwrite_request_authority(field, value):
    from lib.llm.reasoning import capture_intent
    intent = capture_intent(effort='minimal')
    intent[field] = value
    body = {'model': 'original-route', '_reasoning_intent': intent}
    with pytest.raises(ReasoningContractError, match='invalid_intent'):
        compile_reasoning(body, 'destination', inference_profile=profile())
    assert body['model'] == 'original-route'
    assert body['_reasoning_intent'] is intent


def test_failed_plugin_cannot_mutate_caller_nested_messages(monkeypatch):
    from lib.llm.reasoning import capture_intent
    from lib.llm_dispatch import provider_registry
    class Plugin:
        def apply_build(self, body, **kwargs):
            body['messages'][0]['content'] = 'corrupted'
            raise RuntimeError('SECRET')
    monkeypatch.setattr(provider_registry, 'get_dialect', lambda key: Plugin())
    body = {'messages': [{'role': 'user', 'content': 'original'}],
            '_reasoning_intent': capture_intent(thinking_depth='enabled')}
    original = copy.deepcopy(body)
    with pytest.raises(ReasoningContractError, match='plugin_failure'):
        compile_reasoning(body, 'future', inference_profile=profile('custom'))
    assert body == original


def test_native_explicit_budget_is_preserved_or_rejected_never_defaulted():
    body = {'thinking': {'type': 'enabled', 'budget_tokens': 2048}}
    p = profile('thinking_type')
    p['reasoning'].update(min_budget_tokens=1024, max_budget_tokens=4096,
                          default_budget_tokens=1024)
    compile_reasoning(body, 'future-native', inference_profile=p)
    assert body['thinking']['budget_tokens'] == 2048
    before = copy.deepcopy(body)
    with pytest.raises(ReasoningContractError, match='unrepresentable_budget'):
        compile_reasoning(body, 'effort-only', inference_profile=profile())
    assert body == before
    p['reasoning']['max_budget_tokens'] = 1536
    with pytest.raises(ReasoningContractError, match='unsupported_budget'):
        compile_reasoning(body, 'smaller-budget', inference_profile=p)
    assert body == before


@pytest.mark.parametrize('kwargs', [{'effort': 'minimal'}, {'thinking_depth': 'off'}])
def test_failed_builtin_retarget_is_atomic(kwargs):
    body = build_body('future', [{'role': 'user', 'content': 'hi'}],
                      inference_profile=profile(), **kwargs)
    before = copy.deepcopy(body)
    with pytest.raises(ReasoningContractError):
        compile_reasoning(body, 'unknown-contract', inference_profile={
            'reasoning': {'support': 'unknown'}})
    assert body == before
