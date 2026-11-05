"""Caller-supplied material drives a deck instead of an over-length topic."""
from __future__ import annotations

import pytest

from lib.slides.contracts import (
    MAX_SLIDE_MATERIAL_CHARS,
    normalise_slide_material,
    normalise_slide_topic,
)
from lib.slides.recipe import _build_outline_prompt, _run_research
from lib.error_envelope import BadRequest

pytestmark = pytest.mark.unit


def test_material_is_bounded_but_admits_a_full_brief():
    brief = '本项目真实数据：困惑度 34.40 vs 基线 38.68。' * 100
    assert normalise_slide_material(brief) == brief
    with pytest.raises(BadRequest):
        normalise_slide_material('x' * (MAX_SLIDE_MATERIAL_CHARS + 1))


def test_material_is_optional_and_defaults_to_empty():
    assert normalise_slide_material(None) == ''
    assert normalise_slide_material('  ') == ''


def test_topic_limit_is_unchanged():
    with pytest.raises(BadRequest):
        normalise_slide_topic('x' * 4001)


def test_research_stage_attaches_supplied_material(monkeypatch):
    captured = {}

    def fake_research(topic):
        captured['topic'] = topic
        return {'topic': topic, 'cards': []}

    monkeypatch.setattr(
        'lib.production.research.research_topic', fake_research)
    material = '## 真实证据\nCARD 困惑度 34.40 vs AR 38.68。'
    result = _run_research({'topic': 'CARD 汇报', 'material': material})
    assert captured['topic'] == 'CARD 汇报'
    assert result['supplied_material'] == material


def test_research_stage_without_material_changes_nothing(monkeypatch):
    monkeypatch.setattr(
        'lib.production.research.research_topic',
        lambda topic: {'topic': topic, 'cards': []})
    result = _run_research({'topic': 'plain', 'material': ''})
    assert 'supplied_material' not in result


def test_outline_prompt_marks_material_as_authoritative():
    prompt = _build_outline_prompt(
        'CARD 汇报', lang='zh', max_pages=12, scenarios_doc='tech(技术)',
        style_hint='', research_doc='', research_as_of='2026-11',
        material='真实证据：困惑度 34.40。')
    assert 'AUTHORITATIVE' in prompt
    assert '真实证据：困惑度 34.40。' in prompt
    assert 'Never infer personal achievements' in prompt
    # Material must not be silently dropped when research is absent.
    assert prompt.index('AUTHORITATIVE') < prompt.index('Audience and evidence')
