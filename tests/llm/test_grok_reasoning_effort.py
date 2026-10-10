"""xAI Grok reasoning contract: mandatory reasoning_effort ladder, no off
rung, and no presencePenalty/frequencyPenalty/stop on the wire."""

from __future__ import annotations

import pytest

from lib.model_info import grok_reasoning_effort, is_grok


pytestmark = pytest.mark.unit


@pytest.mark.parametrize('model', [
    'grok-4.6',
    'grok-4.20',
    'grok-4.1-fast',
    'xai/grok-4.6',
])
def test_family_predicate_matches_grok_lines(model):
    assert is_grok(model)


@pytest.mark.parametrize('model', ['glm-5.3', 'gpt-5.6', 'muse-spark-1.3'])
def test_family_predicate_rejects_other_families(model):
    assert not is_grok(model)


def test_disabled_thinking_uses_low_floor():
    assert grok_reasoning_effort(None, False) == 'low'


@pytest.mark.parametrize('requested', ['off', 'minimal', 'none'])
def test_explicit_disabled_rungs_clamp_to_low(requested):
    # Reasoning cannot be disabled on Grok (docs.x.ai, 2026-08-12).
    assert grok_reasoning_effort(requested, True) == 'low'


@pytest.mark.parametrize('requested, expected', [
    ('low', 'low'),
    ('medium', 'medium'),
    ('high', 'high'),
    ('xhigh', 'xhigh'),
])
def test_ga_rungs_pass_through_verbatim(requested, expected):
    assert grok_reasoning_effort(requested, True) == expected


@pytest.mark.parametrize('requested', ['max', 'ultra'])
def test_above_ga_ceiling_clamps_to_xhigh(requested):
    # Grok's top rung is xhigh (grok-4.6+); 'max'/'ultra' are other
    # families' labels.
    assert grok_reasoning_effort(requested, True) == 'xhigh'


def test_default_effort_is_medium():
    assert grok_reasoning_effort(None, True) == 'medium'


def test_build_body_speaks_effort():
    from lib.llm import build_body

    body = build_body('grok-4.6', [{'role': 'user', 'content': 'hi'}],
                      thinking_enabled=True, thinking_depth='max',
                      stream=False)
    assert body['reasoning_effort'] == 'xhigh'
    assert 'thinking' not in body
    assert 'enable_thinking' not in body


def test_build_body_off_depth_degrades_to_low():
    from lib.llm import build_body

    body = build_body('grok-4.6', [{'role': 'user', 'content': 'hi'}],
                      thinking_enabled=False, stream=False)
    assert body['reasoning_effort'] == 'low'


def test_build_body_strips_rejected_sampling_params():
    from lib.llm import build_body

    body = build_body('grok-4.6', [{'role': 'user', 'content': 'hi'}],
                      thinking_enabled=True, stream=False,
                      extra={'presence_penalty': 0.5,
                             'frequency_penalty': 0.5,
                             'stop': ['\n']})
    for key in ('presence_penalty', 'frequency_penalty', 'stop'):
        assert key not in body


def test_legacy_enable_thinking_format_does_not_shadow_grok():
    # The YourProvider template declares provider-level
    # thinking_format='enable_thinking' (Qwen-oriented). On Grok that field
    # is dead on the wire (live-probed 2026-09-08 on your-llm-gateway.example.com), so
    # the family branch must claim the body instead.
    from lib.llm import build_body

    body = build_body('grok-4.6', [{'role': 'user', 'content': 'hi'}],
                      thinking_enabled=True, thinking_depth='high',
                      stream=False, thinking_format='enable_thinking')
    assert body['reasoning_effort'] == 'high'
    assert 'enable_thinking' not in body


def test_failover_readjust_recovers_grok_dialect():
    # A body built for Qwen (enable_thinking) swapped onto Grok must be
    # rewritten to the effort dialect, with rejected params stripped.
    from lib.llm_dispatch._api_stream_state import _readjust_thinking_params

    body = {'enable_thinking': True, 'temperature': 0.7,
            'presence_penalty': 0.5, 'stop': ['\n'],
            'messages': [{'role': 'user', 'content': 'hi'}]}
    _readjust_thinking_params(body, 'grok-4.6', '')
    assert body['reasoning_effort'] == 'medium'
    assert 'enable_thinking' not in body
    assert 'presence_penalty' not in body
    assert 'stop' not in body


def test_failover_readjust_unshadows_legacy_format():
    from lib.llm_dispatch._api_stream_state import _readjust_thinking_params

    body = {'enable_thinking': True, 'temperature': 0.7, 'effort': 'max',
            'messages': [{'role': 'user', 'content': 'hi'}]}
    _readjust_thinking_params(body, 'grok-4.6', 'enable_thinking')
    assert body['reasoning_effort'] == 'xhigh'
    assert 'enable_thinking' not in body


def test_discovery_votes_reasoning_effort_for_grok():
    from lib.llm_dispatch.discovery import _detect_thinking_format

    assert _detect_thinking_format(
        [{'model_id': 'grok-4.6'}], 'generic') == 'reasoning_effort'
