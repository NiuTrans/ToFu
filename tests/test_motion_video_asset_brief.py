"""tests/test_motion_video_asset_brief.py — the storyboard NAMES the imagery.

WHY (measured 2026-07-29). ``generate_asset`` had been called **zero** times
across every film the pipeline ever produced, and all 17 graphics of the target
film were inline SVG. The cause was not a broken capability: image generation
answers in ~20 s with a real ~300 KB PNG when called directly. The cause was
that nothing ever ASKED. The author prompt said "REAL IMAGERY IS AVAILABLE —
use it when the beat calls for it", which is purely permissive, so the model
always took the cheaper route and drew an SVG.

So imagery becomes a DELIVERABLE of the storyboard rather than a permission:

  * the script stage writes an ``assets`` brief per beat —
    ``[{role: subject|diagram|background, prompt: ...}]``;
  * the author prompt carries that brief and marks the required items;
  * the floor checks they arrived, and for ``subject`` / ``diagram`` an inline
    SVG does NOT substitute — those roles exist precisely for imagery a
    composition cannot draw itself.

``background`` stays optional on purpose: a gradient or a drawn backdrop is a
legitimate answer, and demanding a generated file for it would burn an image
call per scene for no visible gain.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


_SVG_ONLY = '<svg><rect width="9" height="9"/></svg><h1>标题</h1>'
_TEXT_ONLY = '<h1>标题</h1>'


# ══════════════════════════════════════════════════════════
# Brief normalisation
# ══════════════════════════════════════════════════════════

def test_valid_roles_are_kept_and_lowercased():
    from lib.motion_video._recipe import normalise_assets

    got = normalise_assets([
        {'role': 'subject', 'prompt': 'isometric tofu block, flat vector'},
        {'role': 'DIAGRAM', 'prompt': 'a flow of three steps'},
    ])
    assert [a['role'] for a in got] == ['subject', 'diagram']


def test_unknown_role_degrades_to_background_not_to_a_new_obligation():
    """An invented role must never silently CREATE a requirement.

    The role decides whether a real file is mandatory, so passing an
    unrecognised value through would change what the gate demands based on a
    model typo. Under-asking is the safe direction.
    """
    from lib.motion_video._recipe import normalise_assets

    got = normalise_assets([{'role': 'hero-shot', 'prompt': 'x'}])
    assert got == [{'role': 'background', 'prompt': 'x'}]


def test_promptless_asset_is_dropped():
    """An asset request with no prompt cannot be generated — keeping it would
    create an obligation nothing can satisfy."""
    from lib.motion_video._recipe import normalise_assets

    assert normalise_assets([{'role': 'subject', 'prompt': '   '}]) == []


def test_non_list_and_junk_entries_are_ignored():
    from lib.motion_video._recipe import normalise_assets

    assert normalise_assets(None) == []
    assert normalise_assets('subject') == []
    assert normalise_assets([None, 42, {'role': 'subject', 'prompt': 'ok'}]) == \
        [{'role': 'subject', 'prompt': 'ok'}]


# ══════════════════════════════════════════════════════════
# required_asset_roles
# ══════════════════════════════════════════════════════════

def test_subject_and_diagram_oblige_a_file_background_does_not():
    from lib.motion_video._quality import required_asset_roles

    assert required_asset_roles(
        {'assets': [{'role': 'subject', 'prompt': 'p'}]}) == ['subject']
    assert required_asset_roles(
        {'assets': [{'role': 'diagram', 'prompt': 'p'}]}) == ['diagram']
    assert required_asset_roles(
        {'assets': [{'role': 'background', 'prompt': 'p'}]}) == []
    assert required_asset_roles({}) == []
    assert required_asset_roles(None) == []


# ══════════════════════════════════════════════════════════
# The role floor — inline SVG must NOT substitute
# ══════════════════════════════════════════════════════════

def test_inline_svg_does_not_satisfy_a_subject_brief(tmp_path):
    """THE point of this batch.

    An SVG-only composition satisfies the GENERIC floor, which is exactly how
    every film so far shipped with zero generated imagery while reporting
    clean.
    """
    from lib.motion_video._quality import asset_floor_findings

    scene = {'id': 'scene-001',
             'assets': [{'role': 'subject', 'prompt': 'isometric tofu block'}]}
    findings = asset_floor_findings(scene, _SVG_ONLY, str(tmp_path),
                                    mode='authored')
    assert findings, 'a briefed subject with no real file must be flagged'
    assert 'does not substitute' in findings[-1].lower()


def test_a_real_file_satisfies_the_subject_brief(tmp_path):
    from lib.motion_video._quality import asset_floor_findings

    assets = tmp_path / 'assets'
    assets.mkdir()
    (assets / 'hero.png').write_bytes(b'\x89PNG\r\n\x1a\n' + b'0' * 64)
    html = '<img src="assets/hero.png"><h1>标题</h1>'
    scene = {'id': 'scene-001',
             'assets': [{'role': 'subject', 'prompt': 'isometric tofu block'}]}
    assert asset_floor_findings(scene, html, str(tmp_path),
                                mode='authored') == []


def test_background_only_brief_is_satisfied_by_inline_svg(tmp_path):
    """background is texture — a drawn backdrop is a legitimate answer, and
    demanding a generated file would burn an image call per scene for nothing.
    """
    from lib.motion_video._quality import asset_floor_findings

    scene = {'id': 'scene-001',
             'assets': [{'role': 'background', 'prompt': 'soft gradient mesh'}]}
    assert asset_floor_findings(scene, _SVG_ONLY, str(tmp_path),
                                mode='authored') == []


def test_no_brief_falls_back_to_the_generic_floor(tmp_path):
    """A legacy storyboard without a brief keeps the old behaviour: one
    graphic of any kind is enough."""
    from lib.motion_video._quality import asset_floor_findings

    scene = {'id': 'scene-001'}
    assert asset_floor_findings(scene, _SVG_ONLY, str(tmp_path),
                                mode='authored') == []
    assert asset_floor_findings(scene, _TEXT_ONLY, str(tmp_path),
                                mode='authored')


def test_declared_text_only_beat_is_exempt_from_the_role_floor_too(tmp_path):
    from lib.motion_video._quality import asset_floor_findings

    scene = {'id': 'scene-001', 'text_only_reason': 'silent transition hold',
             'assets': [{'role': 'subject', 'prompt': 'x'}]}
    assert asset_floor_findings(scene, _TEXT_ONLY, str(tmp_path),
                                mode='authored') == []


def test_template_scenes_are_not_double_reported(tmp_path):
    from lib.motion_video._quality import asset_floor_findings

    scene = {'id': 'scene-001',
             'assets': [{'role': 'subject', 'prompt': 'x'}]}
    assert asset_floor_findings(scene, _TEXT_ONLY, str(tmp_path),
                                mode='template') == []


# ══════════════════════════════════════════════════════════
# The prompt must CARRY the brief
# ══════════════════════════════════════════════════════════

def test_author_prompt_names_the_required_assets():
    """A floor without an instruction is a red light with no road sign.

    The author has to be TOLD what the beat was briefed for, or the gate
    fails it for something it was never asked to do.
    """
    from lib.motion_video._scene_author import _build_prompt

    scene = {'id': 'scene-001', 'text': '旁白', 'on_screen': '标题',
             'visual': '一个图示',
             'assets': [{'role': 'subject',
                         'prompt': 'isometric cream tofu block, flat vector'},
                        {'role': 'background', 'prompt': 'soft mesh'}]}
    prompt = _build_prompt(scene, width=1080, height=1440, duration=8.0,
                           scene_index=1, total_scenes=6)
    assert 'Asset brief' in prompt
    assert 'isometric cream tofu block' in prompt
    assert 'REQUIRED' in prompt, 'the required item must be marked as such'
    assert 'generate_asset' in prompt


def test_prompt_without_a_brief_has_no_empty_asset_section():
    """A legacy scene must not grow a hollow 'Asset brief' heading."""
    from lib.motion_video._scene_author import _build_prompt

    prompt = _build_prompt({'id': 'scene-001', 'text': 'x', 'on_screen': 'y'},
                           width=1080, height=1440, duration=8.0,
                           scene_index=1, total_scenes=6)
    assert 'Asset brief' not in prompt


# ══════════════════════════════════════════════════════════
# The brief survives the whole storyboard chain
# ══════════════════════════════════════════════════════════

def test_brief_reaches_the_scene_dict(monkeypatch):
    """recipe beat -> paper scene. A brief that dies in transit is a gate
    nobody can satisfy."""
    from lib.paper import video_abstract as va

    beats = [{'text': '第一段旁白内容足够长以构成一个镜头。',
              'on_screen': '要点一',
              'visual': '主体插画',
              'assets': [{'role': 'subject', 'prompt': 'a tofu block'}]},
             {'text': '第二段旁白内容也足够长以构成一个镜头。',
              'on_screen': '要点二', 'visual': '图示',
              'assets': [{'role': 'diagram', 'prompt': 'three step flow'}]}]
    monkeypatch.setattr(va, '_llm_beats', lambda *a, **k: beats)

    scenes = va.build_abstract_scenes('some source text', lang='zh')
    assert scenes, 'expected a storyboard'
    assert scenes[0].get('assets') == [{'role': 'subject',
                                        'prompt': 'a tofu block'}]
    assert scenes[1].get('assets') == [{'role': 'diagram',
                                        'prompt': 'three step flow'}]


def test_split_beat_keeps_its_brief_on_every_piece(monkeypatch):
    """A split beat is ONE beat continuing across two scenes — its imagery
    applies to both pieces, exactly as its caption and art direction do."""
    from lib.paper import video_abstract as va

    long_text = '这是一段非常长的旁白内容需要被拆分成多个镜头才能装下。' * 4
    beats = [{'text': long_text, 'on_screen': '要点',
              'visual': '主体插画',
              'assets': [{'role': 'subject', 'prompt': 'a tofu block'}]}]
    monkeypatch.setattr(va, '_llm_beats', lambda *a, **k: beats)

    scenes = va.build_abstract_scenes('src', lang='zh')
    assert len(scenes) >= 2, 'expected the long beat to split'
    for sc in scenes:
        assert sc.get('assets') == [{'role': 'subject',
                                     'prompt': 'a tofu block'}], \
            'every piece of a split beat keeps the brief'


def test_recipe_prompt_asks_for_the_asset_brief():
    from lib.motion_video._recipe import _build_source_beat_prompt

    for lang in ('zh', 'en'):
        p = _build_source_beat_prompt('material', lang=lang, max_scenes=6,
                                      char_budget=58, caption_capacity=88)
        assert 'assets' in p
        assert 'subject' in p and 'diagram' in p and 'background' in p
