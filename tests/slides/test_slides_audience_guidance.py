"""Research and interview decks preserve audience intent and evidence imagery."""

import pytest

from lib.design_sys.themes import classify_scenario, design_bible_text, list_themes
from lib.slides._creative_plan import normalise_deck_plan
from lib.slides.recipe import _build_outline_prompt

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('topic', [
    '分布式系统架构工程师面试', '求职项目经历与个人贡献',
    '工作整理：算法模型复盘', 'Engineering interview project walkthrough',
])
def test_interview_purpose_outranks_project_subject(topic):
    assert classify_scenario(topic) == 'career-interview'


@pytest.mark.parametrize(('topic', 'scenario', 'required'), [
    ('论文实验结果学术报告', 'academic-research', 'percentage'),
    ('面试项目复盘', 'career-interview', 'team outcome'),
])
@pytest.mark.parametrize('lang', ['zh', 'en'])
def test_planning_and_bounded_author_guidance_carry_evidence_rules(topic, scenario, required, lang):
    prompt = _build_outline_prompt(
        topic, lang=lang, max_pages=6, scenarios_doc=scenario, style_hint='')
    assert required in prompt
    assert required in design_bible_text(scenario, limit=2500)


@pytest.mark.parametrize('lang', ['zh', 'en'])
def test_outline_prompt_offers_the_curated_theme_choices(lang):
    # theme_id is a closed set: an id the planner cannot see is guessed, and a
    # guessed id silently degrades to the scenario default (light for interviews).
    prompt = _build_outline_prompt(
        '面试项目复盘', lang=lang, max_pages=6,
        scenarios_doc='career-interview(面试/工作整理)',
        style_hint='深石板底 + 暖金单点强调')
    for theme in list_themes():
        assert theme.id in prompt
        assert theme.mood in prompt


@pytest.mark.parametrize('scenario', ['academic-research', 'career-interview'])
@pytest.mark.parametrize('provided', [False, True])
def test_result_pages_do_not_generate_synthetic_evidence(scenario, provided):
    plan = normalise_deck_plan({'title': 'Evidence', 'scenario': scenario, 'pages': [
        {'pageType': 'cover', 'key_message': 'Question'},
        {'pageType': 'content', 'key_message': 'Our result',
         'visual_modality': 'annotated-evidence',
         'asset_mode': 'provided' if provided else 'generate'},
        {'pageType': 'final', 'key_message': 'Conclusion'},
    ]})
    page = plan['pages'][1]
    if provided:
        assert page['visual_modality'] == 'annotated-evidence'
        assert page['asset_mode'] == 'provided'
    else:
        assert page['visual_modality'] == 'native-diagram'
        assert page['asset_mode'] == 'code'
