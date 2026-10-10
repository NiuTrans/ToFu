"""Muse Spark has no reasoning_effort=none rung and no GA max rung."""

from __future__ import annotations

import pytest

from lib.model_info import is_muse_spark, muse_spark_reasoning_effort


pytestmark = pytest.mark.unit


@pytest.mark.parametrize('model', [
    'muse-spark-1.3',
    'muse-spark-1.3-contributor',
    'meta/muse-spark-1.3',
])
def test_family_predicate_matches_skus_and_namespaces(model):
    assert is_muse_spark(model)


@pytest.mark.parametrize('model', ['gpt-5.3-codex-spark', 'glm-5.3'])
def test_family_predicate_rejects_other_spark_and_families(model):
    assert not is_muse_spark(model)


def test_disabled_thinking_uses_minimal_floor():
    assert muse_spark_reasoning_effort(None, False) == 'minimal'


@pytest.mark.parametrize('requested', ['off', 'minimal', 'none'])
def test_explicit_disabled_rungs_clamp_to_minimal(requested):
    assert muse_spark_reasoning_effort(requested, True) == 'minimal'


@pytest.mark.parametrize('requested, expected', [
    ('low', 'low'),
    ('medium', 'medium'),
    ('high', 'high'),
    ('xhigh', 'xhigh'),
])
def test_ga_rungs_pass_through_verbatim(requested, expected):
    assert muse_spark_reasoning_effort(requested, True) == expected


@pytest.mark.parametrize('requested', ['max', 'ultra'])
def test_above_ga_ceiling_clamps_to_xhigh(requested):
    # 'max' reasoning is a partner limited preview at the 2026-09-02 launch;
    # the generally-available ceiling is xhigh.
    assert muse_spark_reasoning_effort(requested, True) == 'xhigh'


def test_default_effort_is_medium():
    assert muse_spark_reasoning_effort(None, True) == 'medium'


def test_build_body_speaks_effort_only():
    from lib.llm import build_body

    body = build_body('muse-spark-1.3', [{'role': 'user', 'content': 'hi'}],
                      thinking_enabled=True, thinking_depth='high',
                      stream=False)
    assert body['reasoning_effort'] == 'high'
    assert 'thinking' not in body
    assert 'enable_thinking' not in body
    # Sampling params are not part of the verified contract.
    assert 'temperature' not in body


def test_build_body_off_depth_degrades_to_minimal():
    from lib.llm import build_body

    body = build_body('muse-spark-1.3-contributor',
                      [{'role': 'user', 'content': 'hi'}],
                      thinking_enabled=False, stream=False)
    assert body['reasoning_effort'] == 'minimal'
    assert 'temperature' not in body


def test_failover_readjust_recovers_muse_dialect():
    # A body built for Qwen (enable_thinking + temperature) swapped onto
    # Muse Spark must be rewritten to the effort dialect.
    from lib.llm_dispatch._api_stream_state import _readjust_thinking_params

    body = {'enable_thinking': True, 'temperature': 0.7,
            'messages': [{'role': 'user', 'content': 'hi'}]}
    _readjust_thinking_params(body, 'muse-spark-1.3', '')
    assert body['reasoning_effort'] == 'medium'
    assert 'enable_thinking' not in body
    assert 'temperature' not in body
