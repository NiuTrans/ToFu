"""Creative-plan contract for storyboard coherence and mandatory blueprints."""

from __future__ import annotations

import pytest

from lib.motion_video._creative_plan import (
    BLUEPRINTS, frame_packet, normalise_film_plan, normalise_scene_plan,
)
from lib.motion_video._shot_recipes import (
    SHOT_CONTRACT_VERSION,
    shot_contract_errors,
    shot_plan_findings,
)

pytestmark = pytest.mark.unit


def test_legacy_storyboard_gets_a_complete_creative_plan():
    scenes = [
        {'id': 's1', 'text': '性能提升 44.4%'},
        {'id': 's2', 'text': '旧方案与新方案对比'},
        {'id': 's3', 'text': '资料来源', 'visual': 'sources', 'spoken': False},
    ]
    normalise_film_plan(scenes)
    assert scenes[0]['narrative_role'] == 'hook'
    assert scenes[0]['blueprint'] in BLUEPRINTS
    assert scenes[1]['narrative_role'] == 'comparison'
    assert scenes[1]['blueprint'] == 'comparison-split-cards'
    assert scenes[2]['narrative_role'] == 'credits'
    assert scenes[2]['blueprint'] == ''
    assert all(s.get('narrative_why') for s in scenes)
    assert all(s.get('transition_in') for s in scenes)
    assert scenes[0]['shot_contract_version'] == SHOT_CONTRACT_VERSION
    assert scenes[0]['shot_recipe'] == scenes[0]['blueprint']
    assert scenes[0]['motion_family'] == 'metric-impact'
    assert len(scenes[0]['qa_progresses']) == 4
    assert scenes[0]['hold_s'] >= 0.5
    assert shot_contract_errors(scenes) == []


def test_invalid_model_fields_cannot_create_a_dead_reference():
    scene = {'text': '解释工作原理', 'narrative_role': 'made-up',
             'blueprint': '../../../etc/passwd', 'transition_in': 'spin'}
    normalise_scene_plan(scene, 2, 4)
    assert scene['narrative_role'] == 'mechanism'
    assert scene['blueprint'] == 'concept-demo-decode-pan'
    assert scene['transition_in'] == 'match-cut'


def test_valid_model_plan_is_preserved():
    scene = {'text': '结果', 'narrative_role': 'evidence',
             'narrative_why': 'This is the decisive benchmark.',
             'blueprint': 'metric-video-text-pivot',
             'transition_in': 'wipe', 'signature_move': 'pivot on the number'}
    normalise_scene_plan(scene, 3, 5)
    assert scene['narrative_why'] == 'This is the decisive benchmark.'
    assert scene['blueprint'] == 'metric-video-text-pivot'
    assert scene['signature_move'] == 'pivot on the number'


def test_semantics_select_a_real_page_demo_recipe():
    scene = {
        'id': 's', 'text': '展示页面中的座椅控制功能如何操作',
        'visual': '真实产品界面滚动到控制面板',
        'narrative_role': 'mechanism',
    }
    normalise_scene_plan(scene, 2, 4)
    assert scene['shot_recipe'] == 'demo-page-scroll-spotlight'
    assert scene['motion_family'] == 'product-demo'
    assert 'exact feature' in ' '.join(scene['recipe_constraints'])


def test_film_planner_avoids_adjacent_motion_family_when_it_can():
    scenes = [
        {'id': 's1', 'text': '这意味着效率提升',
         'narrative_role': 'implication'},
        {'id': 's2', 'text': '让团队从此更从容',
         'narrative_role': 'implication'},
    ]
    normalise_film_plan(scenes)
    assert scenes[0]['motion_family'] != scenes[1]['motion_family']
    assert shot_plan_findings(scenes) == []


def test_shot_plan_reports_explicit_repetition_without_destroying_choice():
    scenes = [
        {'id': 's1', 'text': '价值一', 'narrative_role': 'implication',
         'shot_recipe': 'messaging-multi-phrase'},
        {'id': 's2', 'text': '价值二', 'narrative_role': 'implication',
         'shot_recipe': 'messaging-multi-phrase'},
    ]
    normalise_film_plan(scenes)
    assert scenes[1]['shot_recipe'] == 'messaging-multi-phrase'
    findings = shot_plan_findings(scenes)
    assert any('repeat motion family' in item['issue'] for item in findings)


def test_frame_packet_includes_the_contract_without_a_corpus(monkeypatch):
    from lib.motion_video import _craft

    monkeypatch.setattr(_craft, 'craft_reference', lambda _name: '')
    scene = {'id': 's', 'text': 'x'}
    normalise_scene_plan(scene, 1, 3)
    packet = frame_packet(scene)
    assert 'Mandatory frame packet' in packet
    assert 'narrative role: hook' in packet
    assert 'hook-counter-burst' in packet
    assert 'meaningful midpoint' in packet
    assert 'motion family: metric-impact' in packet
    assert 'QA anchors:' in packet
    assert 'minimum resolved hold:' in packet


def test_frame_packet_makes_outgoing_handle_a_frozen_resolved_tail(monkeypatch):
    from lib.motion_video import _craft

    monkeypatch.setattr(_craft, 'craft_reference', lambda _name: '')
    scene = {'id': 's', 'text': 'x', 'content_duration_s': 4.0,
             'render_duration_s': 4.4, 'outgoing_handle_s': 0.4}
    normalise_scene_plan(scene, 1, 3)
    packet = frame_packet(scene)
    assert 'finish every narrative/action animation by program end' in packet
    assert 'preserve the exact resolved state through render end' in packet
    assert 'never extra story time' in packet


def test_frame_packet_injects_the_full_cited_reference(monkeypatch):
    from lib.motion_video import _craft

    monkeypatch.setattr(
        _craft, 'craft_reference',
        lambda name: f'# craft reference: blueprints/{name}.md\nFULL BODY')
    scene = {'id': 's', 'text': 'x'}
    normalise_scene_plan(scene, 1, 3)
    packet = frame_packet(scene)
    assert 'Full cited blueprint' in packet
    assert 'FULL BODY' in packet
