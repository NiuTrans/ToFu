"""Spark and Astra Codex models have no reasoning_effort=none rung.

Astra (gpt-6-astra) additionally keeps the 5.6-class max top rung — the
2026-09-07 fix that taught the generation gate (is_gpt5) and the effort
mapper about the gpt-6 line; before it, build_body silently dropped the
user's chosen effort for astra and the wire defaulted to medium."""

from __future__ import annotations

import pytest

from lib.model_info import gpt_reasoning_effort


pytestmark = pytest.mark.unit


@pytest.mark.parametrize('model', [
    'gpt-5.3-codex-spark',
    'openai/gpt-5.3-codex-spark',
])
def test_spark_disabled_thinking_uses_supported_floor(model):
    assert gpt_reasoning_effort(None, False, model) == 'low'


@pytest.mark.parametrize('requested', ['off', 'minimal', 'none'])
def test_spark_explicit_disabled_rungs_clamp_to_low(requested):
    assert gpt_reasoning_effort(
        requested, True, 'gpt-5.3-codex-spark') == 'low'


def test_non_spark_codex_keeps_none_when_disabled():
    assert gpt_reasoning_effort(None, False, 'gpt-5.3-codex') == 'none'


@pytest.mark.parametrize('model', [
    'gpt-6-astra',
    'openai/gpt-6-astra',
])
def test_astra_disabled_thinking_uses_supported_floor(model):
    assert gpt_reasoning_effort(None, False, model) == 'low'


@pytest.mark.parametrize('requested', ['off', 'minimal', 'none'])
def test_astra_explicit_disabled_rungs_clamp_to_low(requested):
    assert gpt_reasoning_effort(requested, True, 'gpt-6-astra') == 'low'


@pytest.mark.parametrize('requested,expected', [
    ('low', 'low'),
    ('medium', 'medium'),
    ('high', 'high'),
    ('xhigh', 'xhigh'),
    ('max', 'max'),
    ('ultra', 'max'),
])
def test_astra_enabled_rungs_keep_the_max_tier(requested, expected):
    assert gpt_reasoning_effort(requested, True, 'gpt-6-astra') == expected


def test_astra_is_gpt_reasoning_family_and_56_plus():
    from lib.model_info._family import is_gpt5, is_gpt_56
    assert is_gpt5('gpt-6-astra')
    assert is_gpt_56('gpt-6-astra')
    assert not is_gpt5('gpt-4o')


def test_astra_build_body_stamps_reasoning_effort():
    """End-to-end gate pin: build_body must emit the native effort string
    for astra (the is_gpt5 gate used to exclude it), never a thinking block."""
    from lib.llm import build_body
    body = build_body('gpt-6-astra', [{'role': 'user', 'content': 'hi'}],
                      max_tokens=4096, thinking_enabled=True,
                      thinking_depth='max', stream=False)
    assert body.get('reasoning_effort') == 'max', body
    assert 'thinking' not in body
    assert 'enable_thinking' not in body
