"""tests/test_motion_video_quality_telemetry.py — per-scene quality telemetry
+ the asset floor.

Why these tests exist (measured 2026-07-29, owner-directed):

  * ``job.json`` recorded only ``gate_failed_scenes`` / ``authored_scenes`` —
    a verdict with no numbers behind it. The owner had to re-measure all six
    scenes of a shipped film by hand in a browser to establish that a 32%
    dead-band alarm was 100% attributable to ONE 2-node fallback card and not
    to any layout defect. A verdict that cannot be DIFFED across runs cannot
    show progress.
  * Imagery was PERMITTED by the author prompt and REQUIRED by nothing, so the
    measured water-line was one background image per scene — and some scenes
    with none. "Materials are scarce" was invisible to every gate, because a
    text-only card is perfectly well-formed.

The floor's scope is the load-bearing part and is pinned here in both
directions: it must fire on an image-less AUTHORED scene, and must NOT fire on
a template fallback (already reported as a degrade — reporting it twice buries
the real signal) nor on a declared text-only hold.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.unit


# ══════════════════════════════════════════════════════════
# Graphic counting
# ══════════════════════════════════════════════════════════

def _scene(**over):
    sc = {'id': 'scene-001', 'start': 0.0, 'end': 5.0, 'text': '旁白',
          'on_screen': '标题', 'visual': '一个图示'}
    sc.update(over)
    return sc


def test_counts_a_real_image_file_that_exists(tmp_path):
    from lib.motion_video._quality import count_scene_graphics

    assets = tmp_path / 'assets'
    assets.mkdir()
    (assets / 'bg.png').write_bytes(b'\x89PNG\r\n\x1a\n' + b'0' * 64)
    html = '<img src="assets/bg.png">'
    counts = count_scene_graphics(html, str(tmp_path))
    assert counts['asset_files'] == 1
    assert counts['graphics'] == 1


def test_invented_asset_reference_is_not_counted(tmp_path):
    """A reference to a file that is NOT there renders as a blank rectangle.

    Crediting it would let the floor pass on a visibly broken frame.
    """
    from lib.motion_video._quality import count_scene_graphics

    html = '<img src="assets/does-not-exist.png">'
    counts = count_scene_graphics(html, str(tmp_path))
    assert counts['asset_files'] == 0
    assert counts['graphics'] == 0


def test_font_is_not_a_graphic(tmp_path):
    """Every scene is handed a CJK sans face by the author loop.

    Counting fonts would make the floor self-satisfying — it would report
    success on precisely the all-text card deck it exists to reject.
    """
    from lib.motion_video._quality import count_scene_graphics

    assets = tmp_path / 'assets'
    assets.mkdir()
    (assets / 'cjk-sans.woff2').write_bytes(b'wOF2' + b'0' * 64)
    html = ("@font-face { font-family: 'Tofu Sans SC'; "
            "src: url('assets/cjk-sans.woff2') format('woff2'); }")
    counts = count_scene_graphics(html, str(tmp_path))
    assert counts['asset_files'] == 0
    assert counts['graphics'] == 0
    assert counts['font_faces'] == 1


def test_inline_svg_that_draws_counts_as_a_graphic(tmp_path):
    """A hand-authored SVG gauge is richer than a stock background PNG.

    Counting only files would push authors toward the worse artefact.
    """
    from lib.motion_video._quality import count_scene_graphics

    html = ('<svg width="380" height="240">'
            '<path d="M 40 210 A 150 150 0 0 1 340 210" stroke="#40beff"/>'
            '<circle cx="190" cy="210" r="14"/></svg>')
    counts = count_scene_graphics(html, str(tmp_path))
    assert counts['inline_svg'] == 1
    assert counts['graphics'] == 1


def test_empty_svg_shell_does_not_count(tmp_path):
    """An <svg> holding only <defs> paints nothing."""
    from lib.motion_video._quality import count_scene_graphics

    html = '<svg width="10" height="10"><defs><title>x</title></defs></svg>'
    counts = count_scene_graphics(html, str(tmp_path))
    assert counts['inline_svg'] == 0
    assert counts['graphics'] == 0


# ══════════════════════════════════════════════════════════
# The asset floor
# ══════════════════════════════════════════════════════════

def test_floor_fires_on_an_imageless_authored_scene(tmp_path):
    from lib.motion_video._quality import asset_floor_findings

    html = '<div class="headline">纯文字卡</div>'
    findings = asset_floor_findings(_scene(), html, str(tmp_path),
                                    mode='authored')
    assert findings, 'an authored scene with no graphic must be flagged'
    assert 'no real graphic' in findings[0].lower()


def test_floor_silent_when_the_scene_has_a_graphic(tmp_path):
    from lib.motion_video._quality import asset_floor_findings

    html = '<svg><rect width="10" height="10"/></svg>'
    assert asset_floor_findings(_scene(), html, str(tmp_path),
                                mode='authored') == []


def test_floor_does_not_double_report_a_template_fallback(tmp_path):
    """A fallback card is ALREADY reported on the quality axis as a degrade.

    Failing it here too would report one defect twice and bury the signal
    that actually matters (the fallback itself).
    """
    from lib.motion_video._quality import asset_floor_findings

    html = '<div class="headline">兜底卡</div>'
    assert asset_floor_findings(_scene(), html, str(tmp_path),
                                mode='template') == []


def test_declared_text_only_hold_is_exempt(tmp_path):
    from lib.motion_video._quality import asset_floor_findings

    sc = _scene(text_only_reason='silent transition hold between acts')
    html = '<div class="headline">静默转场</div>'
    assert asset_floor_findings(sc, html, str(tmp_path), mode='authored') == []


def test_sources_end_card_is_exempt(tmp_path):
    """The reserved 'sources' marker is a credits frame by construction."""
    from lib.motion_video._quality import asset_floor_findings

    sc = _scene(visual='sources')
    html = '<div class="headline">来源</div>'
    assert asset_floor_findings(sc, html, str(tmp_path), mode='authored') == []


def test_exemption_must_be_declared_not_inferred(tmp_path):
    """A scene that merely FORGOT its imagery is not exempt.

    An inferred exemption is indistinguishable from the failure it excuses.
    """
    from lib.motion_video._quality import is_text_only_exempt

    exempt, _ = is_text_only_exempt(_scene())
    assert exempt is False


# ══════════════════════════════════════════════════════════
# Telemetry record
# ══════════════════════════════════════════════════════════

def test_telemetry_carries_every_number_the_owner_measured_by_hand(tmp_path):
    from lib.motion_video._quality import scene_telemetry

    fill = {'span': 0.895, 'bottom_dead': 0.047, 'top_dead': 0.058,
            'nodes': 15}
    rec = scene_telemetry(_scene(), '<svg><rect width="1" height="1"/></svg>',
                          str(tmp_path), mode='authored', fill=fill,
                          rounds=3, tokens=23985)
    for key in ('scene_id', 'mode', 'span', 'bottom_dead', 'top_dead',
                'paint_nodes', 'asset_files', 'inline_svg', 'graphics',
                'font_faces', 'author_rounds', 'author_tokens'):
        assert key in rec, f'telemetry must carry {key!r}'
    assert rec['span'] == 0.895
    assert rec['paint_nodes'] == 15
    assert rec['graphics'] == 1
    assert rec['author_tokens'] == 23985


def test_unmeasurable_fill_is_none_never_zero(tmp_path):
    """A measurement that did not happen is UNKNOWN.

    A 0 would read as "the frame is empty" — the worst possible score — in
    every future diff, i.e. an infra outcome would masquerade as the most
    severe defect.
    """
    from lib.motion_video._quality import scene_telemetry

    rec = scene_telemetry(_scene(), '<div>x</div>', str(tmp_path),
                          mode='authored', fill=None)
    assert rec['span'] is None
    assert rec['bottom_dead'] is None
    assert rec['paint_nodes'] is None


def test_summary_reports_how_many_scenes_were_actually_measured():
    """A mean computed over a silent subset is how 'it improved' gets claimed
    from two scenes out of eight."""
    from lib.motion_video._quality import film_quality_summary

    records = [
        {'scene_id': 'a', 'mode': 'authored', 'span': 0.90,
         'bottom_dead': 0.05, 'graphics': 2, 'font_faces': 1},
        {'scene_id': 'b', 'mode': 'authored', 'span': None,
         'bottom_dead': None, 'graphics': 1, 'font_faces': 1},
        {'scene_id': 'c', 'mode': 'template', 'span': 0.64,
         'bottom_dead': 0.325, 'graphics': 0, 'font_faces': 0},
    ]
    s = film_quality_summary(records)
    assert s['scenes'] == 3
    assert s['measured'] == 2, 'must not silently average over the unmeasured'
    assert s['authored'] == 2
    assert s['total_graphics'] == 3
    assert s['scenes_without_graphics'] == 1
    assert s['max_bottom_dead'] == 0.325


# ══════════════════════════════════════════════════════════
# The film-level verdict
# ══════════════════════════════════════════════════════════

def test_verdict_flags_an_all_text_film():
    from lib.motion_video.engine import _quality_verdict

    records = [{'scene_id': 'a', 'mode': 'authored', 'graphics': 0},
               {'scene_id': 'b', 'mode': 'authored', 'graphics': 0}]
    v = _quality_verdict(degraded_narration=False, scene_gate_issues={},
                         authoring=True, authored=2, total=2,
                         scene_records=records)
    assert v['degraded'] is True
    assert 'no real graphic' in v['reason'].lower()


def test_verdict_flags_a_SINGLE_bare_scene_among_rich_ones():
    """PER SCENE, not per film.

    Measured 2026-07-29: the first version only fired when EVERY authored
    scene was bare, so a rescue film with one text-only frame among five rich
    ones reported clean on this axis while a viewer sees exactly the
    "materials are scarce" defect. One bare authored scene is a defect.
    """
    from lib.motion_video.engine import _quality_verdict

    records = [{'scene_id': 'scene-001', 'mode': 'authored', 'graphics': 0},
               {'scene_id': 'scene-002', 'mode': 'authored', 'graphics': 4},
               {'scene_id': 'scene-003', 'mode': 'authored', 'graphics': 4},
               {'scene_id': 'scene-004', 'mode': 'authored', 'graphics': 1},
               {'scene_id': 'scene-005', 'mode': 'authored', 'graphics': 1},
               {'scene_id': 'scene-006', 'mode': 'authored', 'graphics': 2}]
    v = _quality_verdict(degraded_narration=False, scene_gate_issues={},
                         authoring=True, authored=6, total=6,
                         scene_records=records)
    assert v['degraded'] is True, (
        'one bare authored scene among five rich ones is still a defect')
    assert 'scene-001' in v['reason'], (
        'the verdict must NAME the bare scene so it is actionable')
    assert 'scene-002' not in v['reason'], 'must not blame the rich scenes'


def test_verdict_clean_when_scenes_carry_imagery():
    from lib.motion_video.engine import _quality_verdict

    records = [{'scene_id': 'a', 'mode': 'authored', 'graphics': 2},
               {'scene_id': 'b', 'mode': 'authored', 'graphics': 1}]
    v = _quality_verdict(degraded_narration=False, scene_gate_issues={},
                         authoring=True, authored=2, total=2,
                         scene_records=records)
    assert v['degraded'] is False, v['reason']


def test_verdict_ignores_exempt_scenes_when_judging_the_film():
    """A film whose only bare scene is a declared hold is not an all-text film."""
    from lib.motion_video.engine import _quality_verdict

    records = [{'scene_id': 'a', 'mode': 'authored', 'graphics': 3},
               {'scene_id': 'b', 'mode': 'authored', 'graphics': 0,
                'text_only_exempt': 'sources end card'}]
    v = _quality_verdict(degraded_narration=False, scene_gate_issues={},
                         authoring=True, authored=2, total=2,
                         scene_records=records)
    assert v['degraded'] is False, v['reason']


def test_verdict_still_reports_the_legacy_three_axes():
    """The new axis must not displace the ones already relied on."""
    from lib.motion_video.engine import _quality_verdict

    v = _quality_verdict(degraded_narration=True, scene_gate_issues={},
                         authoring=True, authored=1, total=1,
                         scene_records=[{'scene_id': 'a', 'mode': 'authored',
                                         'graphics': 1}])
    assert v['degraded'] is True
    assert 'TTS' in v['reason']


# ══════════════════════════════════════════════════════════
# Manifest persistence
# ══════════════════════════════════════════════════════════

def test_manifest_persists_the_quality_numbers():
    """job.json is the ONLY thing the panel can read after a restart.

    Telemetry that is not persisted survives exactly until the next restart
    and then silently disappears — the same failure the verdict itself had.
    """
    from lib.motion_video.engine import _MANIFEST_FIELDS

    assert 'scene_quality' in _MANIFEST_FIELDS
    assert 'quality_summary' in _MANIFEST_FIELDS


# ══════════════════════════════════════════════════════════
# The single-source-of-default regression
# ══════════════════════════════════════════════════════════

def test_author_default_budget_is_not_shadowed_by_call_sites():
    """Measured 2026-07-29: _DEFAULT_TOKEN_BUDGET was deliberately raised
    60000 → 90000, but engine.py and routes/api_v1/motion.py both passed
    ``or 60000`` explicitly — so the raise was DEAD on every production path,
    and the reading-mode panel (which passes neither knob) was capped at the
    old ceiling. A per-caller default is a copy that stops matching.
    """
    import inspect

    from lib.motion_video import engine
    from lib.motion_video._scene_author import (_DEFAULT_TOKEN_BUDGET,
                                                author_scene)

    sig = inspect.signature(author_scene)
    assert sig.parameters['token_budget'].default is None, \
        'author_scene must take None = "no preference", not a literal copy'
    assert 'max_rounds' not in sig.parameters

    src = inspect.getsource(engine.run_motion_task)
    assert 'or 60000' not in src, \
        'engine must not hardcode a stale copy of the token budget'
    assert str(_DEFAULT_TOKEN_BUDGET) not in src, \
        'engine must not restate the default at all — it must pass None'

    route_src = open(
        os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(engine.__file__))), '..', 'routes', 'api_v1',
            'motion.py'), encoding='utf-8').read()
    assert 'or 60000' not in route_src, \
        'the REST route must not hardcode a stale copy either'


def test_author_accepts_none_and_resolves_the_module_default(monkeypatch,
                                                             tmp_path):
    """Passing None must reach the loop as the module default, not as 0.

    A falsy budget silently meaning "unlimited" (or "stop immediately") is the
    obvious way to get this wrong.
    """
    from lib.motion_video import _scene_author as sa

    seen = {}

    def _fake_once(scene, scene_dir, **kw):
        seen.update(kw)
        return {'outcome': 'quality', 'html': '', 'rounds': 0, 'tokens': 0,
                'detail': 'stub'}

    monkeypatch.setattr(sa, '_author_once', _fake_once)
    sa.author_scene(_scene(), str(tmp_path), width=1080, height=1440,
                    duration=5.0, scene_index=1, total_scenes=1,
                    token_budget=None)
    assert seen['token_budget'] == sa._DEFAULT_TOKEN_BUDGET


# ══════════════════════════════════════════════════════════
# One measurement, not two
# ══════════════════════════════════════════════════════════

def test_fill_findings_are_derivable_without_a_browser():
    """The engine needs findings AND raw numbers from ONE measurement.

    Measuring twice doubles the browser boots and lets the telemetry and the
    verdict disagree about the same composition.
    """
    from lib.motion_video._fill import findings_for_fill

    bad = {'span': 0.642, 'bottom_dead': 0.325, 'top_dead': 0.033, 'nodes': 2}
    findings = findings_for_fill(bad)
    assert findings and 'empty below the last element' in findings[0]

    good = {'span': 0.895, 'bottom_dead': 0.047, 'top_dead': 0.058,
            'nodes': 15}
    assert findings_for_fill(good) == []
    assert findings_for_fill(None) == [], 'unmeasurable is not a defect'
