"""The Spark Codex model has no reasoning_effort=none rung."""

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
