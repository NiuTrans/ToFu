#!/usr/bin/env python3
"""tests/test_paper_video_scene_budget.py — the scene-budget contract.

Guards the root fix for "paper video renders a wall of unreadable text":
a scene's three jobs are carried by three SEPARATE fields, and a storyboard
whose text does not fit its frame or its time is REJECTED rather than
silently clamped onto the duration ceiling.

These are BEHAVIOUR guards (charter: assert the RESULT, not the
implementation). They deliberately do not assert on private helpers or
source-text anchors — they build a storyboard shaped like the one that
actually shipped and assert what the gate/renderer DO with it, so the
implementation can be rewritten and the guards still bite.

The historical failure (job motion_fac7615398424af4) is reproduced exactly:
8 scenes each pinned at 15.000s carrying ~1900 chars of report prose, which
``check_storyboard`` passed green while the frame overflowed ~8x.
"""

from __future__ import annotations

import pytest

from lib import motion_video as mv
from lib.motion_video._template import (MIN_FONT_PX, on_screen_capacity,
                                        render_scene_html, scene_on_screen)

pytestmark = pytest.mark.unit

WIDTH, HEIGHT = 1080, 1440
MAX_SCENE_S = 15.0


def _shipped_shape(n: int = 8, chars: int = 1900) -> list[dict]:
    """A storyboard shaped like the one that shipped: saturated + wall of text."""
    return [{'id': f'scene-{i:03d}',
             'start': (i - 1) * MAX_SCENE_S,
             'end': i * MAX_SCENE_S,
             'text': '字' * chars,
             'visual': ''}
            for i in range(1, n + 1)]


# ══════════════════════════════════════════════════════════
#  capacity model — the gate and the renderer must agree
# ══════════════════════════════════════════════════════════

def test_capacity_shrinks_as_font_grows():
    """Bigger type fits fewer glyphs — the model tracks real geometry."""
    caps = [on_screen_capacity(WIDTH, HEIGHT, px)
            for px in (120, 96, 76, 60, 46)]
    assert caps == sorted(caps), f'capacity must grow as font shrinks: {caps}'
    assert all(c > 0 for c in caps)
    # A 1080x1440 frame at the floor size cannot hold a report paragraph.
    assert on_screen_capacity(WIDTH, HEIGHT, MIN_FONT_PX) < 500


def test_renderer_never_exceeds_the_gate_capacity():
    """Whatever font the renderer picks, the gate's verdict matches reality.

    If these two ever disagree the gate becomes decorative: it would pass
    captions the renderer then clips, or reject ones that fit fine.
    """
    for n in (10, 40, 80, 140, 240):
        scene = {'id': 'scene-001', 'start': 0.0, 'end': 5.0,
                 'text': 'x', 'on_screen': '字' * n}
        errors = mv.check_scene_budget([scene], width=WIDTH, height=HEIGHT,
                                       max_scene_s=MAX_SCENE_S,
                                       narration=False)
        capacity_errors = [e for e in errors if 'on_screen caption' in e]
        fits = n <= on_screen_capacity(WIDTH, HEIGHT, MIN_FONT_PX)
        assert bool(capacity_errors) != fits, (
            f'{n} chars: gate says {"reject" if capacity_errors else "accept"} '
            f'but capacity model says {"fits" if fits else "overflows"}')


# ══════════════════════════════════════════════════════════
#  the gate bites the shape that actually shipped
# ══════════════════════════════════════════════════════════

def test_old_timeline_gate_is_blind_to_this():
    """The pre-existing gate passes the broken storyboard — that's WHY the
    budget gate exists. If this ever starts failing, the two gates have
    overlapped and this suite's premise needs re-checking."""
    scenes = _shipped_shape()
    span = (scenes[0]['start'], scenes[-1]['end'])
    assert mv.check_storyboard(scenes, span) == []


def test_budget_gate_rejects_the_shipped_shape():
    """All three failure modes are reported, per scene."""
    scenes = _shipped_shape()
    errors = mv.check_scene_budget(scenes, width=WIDTH, height=HEIGHT,
                                   max_scene_s=MAX_SCENE_S)
    assert errors, 'the shipped storyboard must not pass'
    blob = ' | '.join(errors)
    assert 'saturated' in blob, 'clamped duration must be reported'
    assert 'on_screen caption' in blob, 'overflowing caption must be reported'
    assert 'narration needs' in blob, 'unspeakable narration must be reported'
    # every scene is guilty of all three
    for sc in scenes:
        assert sum(1 for e in errors if sc['id'] in e) == 3, sc['id']


def test_saturation_alone_is_an_error():
    """A scene sitting exactly on the ceiling is a swallowed error even when
    its text is perfectly fine — clamping means the builder gave up."""
    scene = {'id': 'scene-001', 'start': 0.0, 'end': MAX_SCENE_S,
             'text': '短旁白。', 'on_screen': '短标题', 'visual': ''}
    errors = mv.check_scene_budget([scene], width=WIDTH, height=HEIGHT,
                                   max_scene_s=MAX_SCENE_S)
    assert any('saturated' in e for e in errors), errors


def test_narration_overflow_is_caught():
    """Spoken text that cannot be uttered in the slot stretches the film."""
    scene = {'id': 'scene-001', 'start': 0.0, 'end': 5.0,
             'text': '字' * 2000, 'on_screen': '标题', 'visual': ''}
    errors = mv.check_scene_budget([scene], width=WIDTH, height=HEIGHT,
                                   max_scene_s=MAX_SCENE_S)
    assert any('narration needs' in e for e in errors), errors


def test_a_healthy_storyboard_passes_both_gates():
    scenes = [{'id': 'scene-001', 'start': 0.0, 'end': 6.0,
               'text': '这是一段可以在六秒内说完的旁白。', 'on_screen': '第一个要点',
               'visual': '俯拍城市夜景'},
              {'id': 'scene-002', 'start': 6.0, 'end': 12.0,
               'text': '第二段旁白同样简短。', 'on_screen': '第二个要点',
               'visual': '特写手部动作'}]
    span = (scenes[0]['start'], scenes[-1]['end'])
    assert mv.check_storyboard(scenes, span) == []
    assert mv.check_scene_budget(scenes, width=WIDTH, height=HEIGHT,
                                 max_scene_s=MAX_SCENE_S) == []


# ══════════════════════════════════════════════════════════
#  the three fields stay three fields
# ══════════════════════════════════════════════════════════

def test_visual_is_never_drawn_as_the_headline():
    """``visual`` is art direction. The end card proves it: the topic recipe
    marks it ``visual='sources'`` while its human-readable credit line lives
    in ``text`` — rendering ``visual`` would put the literal word 'sources'
    on screen."""
    end_card = {'id': 'scene-003', 'start': 0.0, 'end': 3.5,
                'text': '资料来源:arxiv.org · nature.com',
                'visual': 'sources', 'spoken': False}
    assert scene_on_screen(end_card) == '资料来源:arxiv.org · nature.com'
    html = render_scene_html(end_card, width=WIDTH, height=HEIGHT,
                             scene_index=3, total_scenes=3)
    assert '资料来源:arxiv.org · nature.com' in html
    assert '>sources<' not in html


def test_sources_end_card_survives_the_real_recipe_path():
    """Same guarantee, but through the recipe that actually builds it, so a
    change to the end-card shape cannot slip past the hand-made fixture."""
    from lib.motion_video._recipe import _provisional_scenes

    scenes = _provisional_scenes(['第一段口播。', '第二段口播。'],
                                 '资料来源:arxiv.org · nature.com')
    tail = scenes[-1]
    assert tail['visual'] == 'sources' and tail['spoken'] is False
    html = render_scene_html(tail, width=WIDTH, height=HEIGHT,
                             scene_index=len(scenes), total_scenes=len(scenes))
    assert '资料来源' in html
    assert '>sources<' not in html


def test_on_screen_wins_over_text_but_text_is_the_fallback():
    """Legacy storyboards (no on_screen) must keep rendering."""
    assert scene_on_screen({'text': '旁白', 'on_screen': '标题'}) == '标题'
    assert scene_on_screen({'text': '旁白'}) == '旁白'
    assert scene_on_screen({'text': '旁白', 'on_screen': '   '}) == '旁白'


def test_scene_author_still_receives_visual_as_art_direction():
    """The per-scene author's art-direction channel must not be repurposed."""
    from lib.motion_video._scene_author import _build_prompt

    prompt = _build_prompt({'id': 'scene-001', 'text': '旁白内容',
                            'visual': '俯拍城市夜景,霓虹渐显'},
                           width=WIDTH, height=HEIGHT, duration=4.0,
                           scene_index=1, total_scenes=3)
    assert 'visual direction: 俯拍城市夜景,霓虹渐显' in prompt


# ══════════════════════════════════════════════════════════
#  NEUTER — prove each check is load-bearing
# ══════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════
#  a caption is a COMPRESSION, not a second copy of the narration
# ══════════════════════════════════════════════════════════

def test_caption_budget_is_a_title_length_not_the_floor():
    """The authoring budget is the capacity at title size, not at the 46px
    floor. The floor only exists so a legacy over-long caption still renders;
    treating it as the budget is what let a caption be 247 chars of prose."""
    from lib.motion_video._template import CAPTION_FONT_PX

    assert CAPTION_FONT_PX > MIN_FONT_PX
    caption_budget = on_screen_capacity(WIDTH, HEIGHT, CAPTION_FONT_PX)
    assert 0 < caption_budget < on_screen_capacity(WIDTH, HEIGHT, MIN_FONT_PX)


def test_gate_rejects_a_caption_that_is_the_narration_verbatim():
    """A caption UNDER the 46px floor still fails when it is the narration
    copied verbatim and over the title budget — the capacity check alone
    cannot see this, which is how 'three fields' silently became two."""
    from lib.motion_video._template import CAPTION_FONT_PX

    budget = on_screen_capacity(WIDTH, HEIGHT, CAPTION_FONT_PX)
    floor = on_screen_capacity(WIDTH, HEIGHT, MIN_FONT_PX)
    prose = '字' * ((budget + floor) // 2)   # fits the floor, over the budget
    scene = {'id': 'scene-001', 'start': 0.0, 'end': 6.0,
             'text': prose, 'on_screen': prose, 'visual': ''}
    errors = mv.check_scene_budget([scene], width=WIDTH, height=HEIGHT,
                                   max_scene_s=MAX_SCENE_S, narration=False)
    assert any('duplicates the narration' in e for e in errors), errors
    # A genuine condensation of the same beat passes.
    scene['on_screen'] = '字' * min(12, budget)
    assert not any('duplicates the narration' in e for e in
                   mv.check_scene_budget([scene], width=WIDTH, height=HEIGHT,
                                         max_scene_s=MAX_SCENE_S,
                                         narration=False))


# ══════════════════════════════════════════════════════════
#  the film samples the WHOLE report, not its first 3%
# ══════════════════════════════════════════════════════════

def _long_report(sections: int = 40) -> str:
    """A report whose sections are individually identifiable, so we can tell
    WHERE in the document a beat came from."""
    return '\n\n'.join(
        f'第{i}节的论述内容在这里展开，包含足够长度的说明文字以构成一个完整段落。'
        for i in range(1, sections + 1))


def test_beats_are_drawn_from_across_the_whole_document():
    """A ``max_scenes``-beat film holds far less text than a full report, so
    reading from the top until the budget runs out ships the opening and
    discards the results and limitations. Beats must SAMPLE the document.

    Guards a property, not a splitter: any implementation that covers the
    document keeps this green.
    """
    from lib.paper.video_abstract import build_abstract_scenes

    report = _long_report(40)
    scenes = build_abstract_scenes(report, max_scenes=8, use_llm=False)
    assert scenes, 'a long report must yield beats'
    # Where does each beat's text sit in the source?
    positions = [report.find(sc['text'][:16]) for sc in scenes]
    assert all(p >= 0 for p in positions), positions
    # The last beat must come from the document's back half — the precise
    # failure being guarded is "everything came from the top".
    assert max(positions) > len(report) * 0.5, (
        f'beats only cover up to {max(positions) / len(report):.0%} of the '
        f'report — the tail (results/limitations) was discarded')
    assert positions == sorted(positions), 'beats must stay in document order'


def test_no_beat_is_a_three_second_runt():
    """Filling each piece to the budget leaves a tiny tail (67 chars at a
    58-char budget -> 58 + 9), and a 9-char scene is floored to the minimum
    duration: seconds of screen time carrying almost nothing."""
    from lib.paper.video_abstract import (_MIN_SCENE_S, build_abstract_scenes)

    scenes = build_abstract_scenes(_long_report(40), max_scenes=8,
                                   use_llm=False)
    runts = [sc['id'] for sc in scenes
             if (sc['end'] - sc['start']) <= _MIN_SCENE_S + 1e-6
             and len(sc['text']) < 20]
    assert not runts, f'runt scenes carrying almost no narration: {runts}'


def test_split_beats_keep_their_authored_caption_and_art_direction():
    """A beat split for length is ONE beat continuing across two scenes, so
    the authored caption and art direction apply to every piece. Blanking
    them for the tail produced placeholder captions on real LLM output."""
    from lib.paper.video_abstract import _BEAT_CHAR_BUDGET
    import lib.paper.video_abstract as VA

    long_beat = '这是一段需要被切分的很长旁白内容。' * 6   # well over the budget
    assert len(long_beat) > _BEAT_CHAR_BUDGET * 1.5
    beats = [{'text': long_beat, 'on_screen': '被授权的标题',
              'visual': '俯拍城市夜景'}]
    orig = VA._llm_beats
    VA._llm_beats = lambda *a, **k: [dict(b) for b in beats]
    try:
        scenes = VA.build_abstract_scenes('irrelevant', max_scenes=8,
                                          use_llm=True)
    finally:
        VA._llm_beats = orig
    assert len(scenes) >= 2, 'the beat should have been split'
    assert all(sc['on_screen'] == '被授权的标题' for sc in scenes), \
        [sc['on_screen'] for sc in scenes]
    assert all(sc['visual'] == '俯拍城市夜景' for sc in scenes), \
        [sc['visual'] for sc in scenes]


def test_NEUTER_without_saturation_check_the_shipped_shape_slips_through():
    """Drop the saturation + capacity + narration findings and the broken
    storyboard is accepted — i.e. these findings ARE the defence, not
    decoration."""
    scenes = _shipped_shape()
    errors = mv.check_scene_budget(scenes, width=WIDTH, height=HEIGHT,
                                   max_scene_s=MAX_SCENE_S)
    neutered = [e for e in errors
                if 'saturated' not in e
                and 'on_screen caption' not in e
                and 'narration needs' not in e]
    assert neutered == [], (
        'something ELSE is rejecting this storyboard — the three findings '
        'under test are no longer the thing that catches it')


def test_NEUTER_raising_the_ceiling_hides_the_clamp():
    """Saturation is judged against the ceiling in force. If a future change
    quietly raises _MAX_SCENE_S, the same 15s scenes stop reading as clamped
    — this pins that the check tracks the ceiling rather than the number 15."""
    scenes = _shipped_shape(n=1)
    at_ceiling = mv.check_scene_budget(scenes, width=WIDTH, height=HEIGHT,
                                       max_scene_s=MAX_SCENE_S)
    raised = mv.check_scene_budget(scenes, width=WIDTH, height=HEIGHT,
                                   max_scene_s=60.0)
    assert any('saturated' in e for e in at_ceiling)
    assert not any('saturated' in e for e in raised)
