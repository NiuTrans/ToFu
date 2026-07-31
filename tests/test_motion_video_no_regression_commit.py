"""tests/test_motion_video_no_regression_commit.py — a re-run may only IMPROVE.

WHY (owner call after a measured near-miss, 2026-07-29). The compose stage
wrote every composition straight to ``index.html``. That made re-running an
ALREADY-GOOD film a bet on the LLM gateway: the moment the author loop degraded
a scene — an exhausted credit (HTTP 402), a 120 s read timeout, three transient
faults in a row — the fallback card overwrote finished work that had already
passed every gate, and the next concat baked it into ``final.mp4``.

The near-miss, measured: a re-run of the target film was started while the
gateway was answering 402 and timing out at 120 s. The contract gate had
correctly refused to adopt four stale scenes, so all four were back in the
author loop — four scenes whose ``index.html`` held authored compositions
(span 87.5–93.8%, 17 graphics, zero fallbacks). Had their retries run out, a
verified-good deliverable would have been destroyed in place by a run whose
entire purpose was to improve it. The run was stopped by hand; nothing should
ever depend on that.

The rule these tests pin: **a re-run may only ever RAISE a scene's grade.** A
worse result is kept as a draft for the next attempt, and the known-good
composition stays on disk — so re-running is always safe rather than a gamble.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.unit


def _authored(with_graphic: bool = True, dur: str = '10.238') -> str:
    svg = '<svg><rect width="10" height="10"/></svg>' if with_graphic else ''
    return (
        '<!doctype html><html><head><style>'
        "@font-face { font-family: 'Tofu Sans SC'; "
        "src: url('assets/cjk-sans.woff2') format('woff2'); }"
        "body { font-family: 'Tofu Sans SC', Inter, sans-serif; }"
        '</style></head><body>'
        f'<div id="root" data-composition-id="main" data-start="0" '
        f'data-duration="{dur}" data-width="1080" data-height="1440">'
        '<div style="position:absolute;inset:0"></div>'
        f'{svg}<h1 id="t">真实构图</h1></div>'
        '<script>window.__timelines={};'
        'const tl=gsap.timeline({paused:true});'
        "window.__timelines['main']=tl;</script></body></html>")


def _scene() -> dict:
    return {'id': 'scene-004', 'start': 30.476, 'end': 40.714,
            'text': '旁白', 'on_screen': 'LCS对齐:零成本自动生成编辑监督',
            'visual': '两行文字上下对齐连线'}


def _template_card() -> str:
    from lib.motion_video._template import render_scene_html
    return render_scene_html(_scene(), duration=10.238, scene_index=4,
                             total_scenes=6)


# ══════════════════════════════════════════════════════════
# The grade
# ══════════════════════════════════════════════════════════

def test_grades_are_ordered_template_bare_rich(tmp_path):
    from lib.motion_video._quality import scene_grade

    assert scene_grade(_authored(True), str(tmp_path),
                       mode='authored') == 'authored_rich'
    assert scene_grade(_authored(False), str(tmp_path),
                       mode='authored') == 'authored_bare'
    assert scene_grade(_template_card(), str(tmp_path),
                       mode='template') == 'template'


@pytest.mark.parametrize('old,new,regress', [
    ('authored_rich', 'template', True),
    ('authored_rich', 'authored_bare', True),
    ('authored_bare', 'template', True),
    ('template', 'authored_rich', False),
    ('template', 'authored_bare', False),
    ('authored_bare', 'authored_rich', False),
    ('authored_rich', 'authored_rich', False),
    ('template', 'template', False),
])
def test_is_regression_ordering(old, new, regress):
    from lib.motion_video._quality import is_regression

    assert is_regression(old, new) is regress


def test_unknown_grade_never_authorises_an_overwrite():
    """A grade nobody classified must not silently mean "safe to replace"."""
    from lib.motion_video._quality import is_regression

    assert is_regression('authored_rich', 'something_new') is True


# ══════════════════════════════════════════════════════════
# The commit seam — the actual guarantee
# ══════════════════════════════════════════════════════════

def test_fallback_card_never_overwrites_an_authored_composition(tmp_path):
    """THE near-miss, reproduced.

    An authored scene is on disk; the author loop degrades and returns the
    template. The good composition must survive.
    """
    from lib.motion_video.engine import _commit_scene_html

    scene_dir = tmp_path / 'scene-004'
    scene_dir.mkdir()
    index = scene_dir / 'index.html'
    good = _authored(True)
    index.write_text(good, encoding='utf-8')

    kept = _commit_scene_html(str(index), _template_card(), _scene(),
                              str(scene_dir), width=1080, height=1440,
                              duration=10.238, scene_index=4, total_scenes=6)

    assert index.read_text(encoding='utf-8') == good, (
        'the known-good composition must still be on disk')
    assert kept == good, (
        'the caller must be handed the composition that is ACTUALLY on disk, '
        'or its gates and telemetry describe a file the renderer will not read')


def test_rejected_composition_is_kept_as_a_draft(tmp_path):
    """Refusing the write must not throw the attempt away — the next run
    continues repairing it instead of starting from a blank page."""
    from lib.motion_video._scene_author import DRAFT_FILENAME
    from lib.motion_video.engine import _commit_scene_html

    scene_dir = tmp_path / 'scene-004'
    scene_dir.mkdir()
    index = scene_dir / 'index.html'
    index.write_text(_authored(True), encoding='utf-8')

    card = _template_card()
    _commit_scene_html(str(index), card, _scene(), str(scene_dir),
                       width=1080, height=1440, duration=10.238,
                       scene_index=4, total_scenes=6)

    draft = scene_dir / DRAFT_FILENAME
    assert draft.is_file(), 'the rejected attempt must survive as a draft'


def test_a_graphics_less_authored_scene_does_not_replace_a_rich_one(tmp_path):
    """Regression is not only about fallback cards: losing the imagery is a
    loss too, and it is exactly the axis the objective is about."""
    from lib.motion_video.engine import _commit_scene_html

    scene_dir = tmp_path / 'scene-004'
    scene_dir.mkdir()
    index = scene_dir / 'index.html'
    rich = _authored(True)
    index.write_text(rich, encoding='utf-8')

    kept = _commit_scene_html(str(index), _authored(False), _scene(),
                              str(scene_dir), width=1080, height=1440,
                              duration=10.238, scene_index=4, total_scenes=6)
    assert kept == rich
    assert index.read_text(encoding='utf-8') == rich


def test_an_improvement_is_committed(tmp_path):
    """The complement — the guarantee must not freeze the film.

    A template on disk replaced by an authored composition MUST be written,
    or scene-004 could never have been rescued in the first place.
    """
    from lib.motion_video.engine import _commit_scene_html

    scene_dir = tmp_path / 'scene-004'
    scene_dir.mkdir()
    index = scene_dir / 'index.html'
    index.write_text(_template_card(), encoding='utf-8')

    better = _authored(True)
    kept = _commit_scene_html(str(index), better, _scene(), str(scene_dir),
                              width=1080, height=1440, duration=10.238,
                              scene_index=4, total_scenes=6)
    assert kept == better
    assert index.read_text(encoding='utf-8') == better


def test_equal_grade_is_committed(tmp_path):
    """Same grade is not a regression — a re-author that is merely DIFFERENT
    must be allowed through, or repairs could never land."""
    from lib.motion_video.engine import _commit_scene_html

    scene_dir = tmp_path / 'scene-004'
    scene_dir.mkdir()
    index = scene_dir / 'index.html'
    index.write_text(_authored(True), encoding='utf-8')

    revised = _authored(True).replace('真实构图', '修订后的构图')
    kept = _commit_scene_html(str(index), revised, _scene(), str(scene_dir),
                              width=1080, height=1440, duration=10.238,
                              scene_index=4, total_scenes=6)
    assert kept == revised


def test_first_write_always_lands(tmp_path):
    """No file on disk = nothing to protect."""
    from lib.motion_video.engine import _commit_scene_html

    scene_dir = tmp_path / 'scene-004'
    scene_dir.mkdir()
    index = scene_dir / 'index.html'
    card = _template_card()
    kept = _commit_scene_html(str(index), card, _scene(), str(scene_dir),
                              width=1080, height=1440, duration=10.238,
                              scene_index=4, total_scenes=6)
    assert kept == card
    assert index.is_file()


def test_a_STALE_composition_is_never_preserved(tmp_path):
    """A composition with the wrong duration renders at the wrong length —
    worse than any grade, so it must not be protected as 'known-good'."""
    from lib.motion_video.engine import _commit_scene_html

    scene_dir = tmp_path / 'scene-004'
    scene_dir.mkdir()
    index = scene_dir / 'index.html'
    # Rich, but timed for a DIFFERENT cut of the film.
    index.write_text(_authored(True, dur='7.500'), encoding='utf-8')

    card = _template_card()          # correct duration, lower grade
    kept = _commit_scene_html(str(index), card, _scene(), str(scene_dir),
                              width=1080, height=1440, duration=10.238,
                              scene_index=4, total_scenes=6)
    assert kept == card, (
        'a stale composition must not block a correctly-timed one — wrong '
        'length is worse than a lower grade')


def test_the_engine_commits_through_the_guarded_seam():
    """The compose loop must not write index.html directly.

    A second write path is how this guarantee gets lost: the guard only holds
    if there is exactly ONE place a composition reaches disk.
    """
    import inspect

    from tests._source_scan import strip_comments
    from lib.motion_video import engine

    src = inspect.getsource(engine.run_motion_task)
    live = strip_comments(src, lang='python')
    assert '_commit_scene_html(' in live, (
        'the compose loop must commit through the no-regression seam')
    assert '_write(index_path' not in live, (
        'a direct write to index.html bypasses the no-regression guarantee')
