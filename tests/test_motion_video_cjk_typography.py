"""tests/test_motion_video_cjk_typography.py — CJK typography contract.

WHY, and what the defect actually IS (measured 2026-07-29 — the first reading
of this was WRONG in an instructive way, so both halves are pinned here).

The alarming reading: ``fc-match "Inter,system-ui,sans-serif:lang=zh"`` answers
``DejaVu Sans``, a face with ZERO CJK coverage. Read literally that says every
scene without an ``@font-face`` draws its Chinese in a font that has no Chinese
— i.e. tofu boxes.

That reading is FALSE, and the measurement that kills it: Chrome resolves font
fallback PER GLYPH, not per pattern. Driven in real headless Chrome against a
scene with no ``@font-face``, the string 「扩散语言模型」 renders **384 px wide
with 6,744 ink pixels** — and a deliberately BOGUS family name (`NoSuchFontXYZ`)
produces a byte-identical raster. The glyphs render, and they are legible.
``fc-match`` on a family LIST models a question the browser never asks.

The REAL defect is subtler and still worth a gate: a composition that ships no
face of its own has **no say in which face draws its Chinese** — the render
host decides. On this host that is a single ``Noto Serif CJK SC``. So a film
that mixes scenes shipping ``Tofu Sans SC`` with scenes that do not is
typographically inconsistent BY CONSTRUCTION, and every un-shipped scene
renders differently on the next host. That is a reproducibility defect as much
as an aesthetic one — and it is exactly what a resume path adopts silently,
because the composition is neither a fallback card nor mistimed.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.unit

_CJK_NO_FACE = (
    '<!doctype html><html><body><div id="root" data-composition-id="main" '
    'data-start="0" data-duration="5" data-width="1080" data-height="1440">'
    '<div style="position:absolute;inset:0"></div>'
    '<h1 style="font-family: Inter, system-ui, sans-serif">扩散语言模型</h1>'
    '</div><script>window.__timelines={};'
    'const tl=gsap.timeline({paused:true});'
    "window.__timelines['main']=tl;</script></body></html>")

_CJK_WITH_FACE = (
    '<!doctype html><html><head><style>'
    "@font-face { font-family: 'Tofu Sans SC'; "
    "src: url('assets/cjk-sans.woff2') format('woff2'); }"
    "body { font-family: 'Tofu Sans SC', Inter, sans-serif; }"
    '</style></head><body><div id="root" data-composition-id="main" '
    'data-start="0" data-duration="5" data-width="1080" data-height="1440">'
    '<div style="position:absolute;inset:0"></div>'
    '<h1>扩散语言模型</h1></div><script>window.__timelines={};'
    'const tl=gsap.timeline({paused:true});'
    "window.__timelines['main']=tl;</script></body></html>")

_LATIN_ONLY = (
    '<!doctype html><html><body><div id="root" data-composition-id="main" '
    'data-start="0" data-duration="5" data-width="1080" data-height="1440">'
    '<div style="position:absolute;inset:0"></div>'
    '<h1 style="font-family: Inter, sans-serif">Diffusion models</h1>'
    '</div><script>window.__timelines={};'
    'const tl=gsap.timeline({paused:true});'
    "window.__timelines['main']=tl;</script></body></html>")


# ══════════════════════════════════════════════════════════
# has_cjk_text — the precondition
# ══════════════════════════════════════════════════════════

def test_cjk_detected_in_visible_text():
    from lib.motion_video._fonts import has_cjk_text

    assert has_cjk_text(_CJK_NO_FACE) is True
    assert has_cjk_text(_LATIN_ONLY) is False


def test_chinese_inside_script_or_style_is_not_visible_text():
    """A Chinese comment or identifier must not make a Latin composition look
    like it needs a Chinese face — the check judges what a VIEWER reads."""
    from lib.motion_video._fonts import has_cjk_text

    html = _LATIN_ONLY.replace(
        '<script>', '<script>/* 中文注释，不会出现在画面上 */')
    assert has_cjk_text(html) is False


# ══════════════════════════════════════════════════════════
# The finding itself
# ══════════════════════════════════════════════════════════

def test_cjk_without_a_shipped_face_is_flagged():
    from lib.motion_video._fonts import cjk_fallback_findings

    findings = cjk_fallback_findings(_CJK_NO_FACE)
    assert findings, 'a CJK scene shipping no face must be reported'
    assert 'render host' in findings[0].lower()


def test_the_finding_does_not_claim_missing_glyphs():
    """Pinning the corrected diagnosis in the finding TEXT.

    Measured: the glyphs DO render (384 px / 6,744 ink px, identical to a
    bogus family name). A finding that says "the Chinese is missing" would
    send the next reader chasing a bug that does not exist.
    """
    from lib.motion_video._fonts import cjk_fallback_findings

    text = cjk_fallback_findings(_CJK_NO_FACE)[0].lower()
    assert 'do render' in text or 'not a missing-glyph' in text, (
        'the finding must state that the glyphs DO render — the defect is '
        'loss of control over WHICH face, not absent coverage')


def test_a_shipped_face_clears_the_finding():
    from lib.motion_video._fonts import cjk_fallback_findings

    assert cjk_fallback_findings(_CJK_WITH_FACE) == []


def test_latin_only_scene_is_never_asked_for_a_cjk_face():
    from lib.motion_video._fonts import cjk_fallback_findings

    assert cjk_fallback_findings(_LATIN_ONLY) == []


# ══════════════════════════════════════════════════════════
# The font-family SCANNER (a parser bug this suite exposed)
# ══════════════════════════════════════════════════════════

def test_inline_style_font_family_does_not_swallow_the_document():
    """Measured 2026-07-29, found by this suite's own fixture.

    The value pattern was ``[^;}]+``, which does not stop at a quote or ``>``.
    An inline ``style="font-family: Inter, sans-serif"`` with no trailing
    semicolon therefore ran past the closing quote and consumed the rest of
    the document, reporting the whole tail as one bogus family — which made
    ``_full_gate`` return a contract error and skip its entire advisory block.
    An attribute is an ordinary way to set a font, so the scanner must
    terminate on the attribute delimiter too.
    """
    from lib.motion_video._fonts import undeclared_font_families

    html = ('<h1 style="font-family: Inter, system-ui, sans-serif">扩散语言模型'
            '</h1><script>var x=1;</script>')
    assert undeclared_font_families(html) == [], (
        'an inline style with safe families must yield NO findings — a '
        'swallowed document tail is not a font family')


def test_scanner_still_catches_a_real_undeclared_family():
    """The complement: fixing the over-reach must not blind the scanner."""
    from lib.motion_video._fonts import undeclared_font_families

    html = '<style>h1 { font-family: PingFang SC, sans-serif; }</style>'
    assert undeclared_font_families(html) == ['PingFang SC']


def test_quoted_font_face_family_is_still_read():
    from lib.motion_video._fonts import declared_font_families

    css = ("<style>@font-face { font-family: 'Tofu Sans SC'; "
           "src: url('assets/f.woff2') format('woff2'); }</style>")
    assert 'Tofu Sans SC' in declared_font_families(css)


# ══════════════════════════════════════════════════════════
# The resume contract — the root cause
# ══════════════════════════════════════════════════════════

def test_stale_typography_composition_is_not_adopted_on_resume(tmp_path):
    """The root cause: _existing_composition asked only about IDENTITY.

    A composition that is neither a fallback card nor mistimed was adopted
    verbatim for ever — so five scenes authored before the font channel
    existed kept shipping host-chosen typography while the one re-authored
    scene shipped its own, and job.json called the film 6/6 clean.
    """
    from lib.motion_video.engine import _existing_composition

    scene_dir = tmp_path / 'scene-001'
    scene_dir.mkdir()
    (scene_dir / 'index.html').write_text(_CJK_NO_FACE, encoding='utf-8')
    scene = {'id': 'scene-001', 'text': '扩散', 'on_screen': '扩散语言模型'}

    got = _existing_composition(str(scene_dir / 'index.html'), 5.0, scene)
    assert got is None, (
        'a composition predating the CJK font contract must be re-authored, '
        'not adopted — otherwise the film mixes two typographies silently')


def test_compliant_composition_is_still_adopted_on_resume(tmp_path):
    """The complement: the contract check must not force a re-author of work
    that already meets it — that would re-spend an agent loop every restart."""
    from lib.motion_video.engine import _existing_composition

    scene_dir = tmp_path / 'scene-001'
    assets = scene_dir / 'assets'
    assets.mkdir(parents=True)
    (assets / 'cjk-sans.woff2').write_bytes(b'wOF2' + b'0' * 64)
    (scene_dir / 'index.html').write_text(_CJK_WITH_FACE, encoding='utf-8')
    scene = {'id': 'scene-001', 'text': '扩散', 'on_screen': '扩散语言模型'}

    got = _existing_composition(str(scene_dir / 'index.html'), 5.0, scene)
    assert got is not None, (
        'a composition that meets the contract must still be reused')


def test_latin_composition_is_adopted_unchanged(tmp_path):
    """A Latin-only scene has no CJK contract to meet."""
    from lib.motion_video.engine import _existing_composition

    scene_dir = tmp_path / 'scene-002'
    scene_dir.mkdir()
    (scene_dir / 'index.html').write_text(_LATIN_ONLY, encoding='utf-8')
    got = _existing_composition(str(scene_dir / 'index.html'), 5.0,
                                {'id': 'scene-002', 'text': 'Diffusion'})
    assert got is not None


def test_contract_findings_are_narrow_by_design():
    """Only defects a re-author would FIX and the identity checks cannot see.

    Cosmetic drift must not trigger a re-author — that would re-spend an agent
    loop per restart for no gain.
    """
    from lib.motion_video.engine import _composition_contract_findings

    assert _composition_contract_findings(_CJK_WITH_FACE, '') == []
    assert _composition_contract_findings(_LATIN_ONLY, '') == []
    assert _composition_contract_findings(_CJK_NO_FACE, '')


# ══════════════════════════════════════════════════════════
# The author's advisory channel carries it too
# ══════════════════════════════════════════════════════════

def test_advisory_gate_reports_cjk_fallback(monkeypatch, tmp_path):
    """Every author path must inherit it — same rule as the asset floor."""
    from lib.motion_video import _render, _scene_author

    monkeypatch.setattr(_render, 'check_project',
                        lambda *a, **k: {'ok': True, 'category': '',
                                         'errors': [], 'fix_hints': []})
    monkeypatch.setattr(
        'lib.motion_video._fill.check_composition_fill', lambda *a, **k: [])
    # Give it a graphic so the asset floor is silent and only the font
    # finding can be responsible for the result.
    html = _CJK_NO_FACE.replace('<h1', '<svg><rect width="9" height="9"/></svg><h1')

    findings = _scene_author._full_gate(html, str(tmp_path),
                                        scene={'id': 'scene-001'},
                                        advisory=True)
    assert any('render host' in f.lower() for f in findings), (
        'advisory=True must report the CJK typography finding')


def test_accept_reject_verdict_does_not_reject_on_typography(monkeypatch,
                                                             tmp_path):
    """Rejecting would degrade the scene to the template — which ships no face
    either, so the metric would get strictly worse. Same trap as the fill gate
    and the asset floor."""
    from lib.motion_video import _render, _scene_author

    monkeypatch.setattr(_render, 'check_project',
                        lambda *a, **k: {'ok': True, 'category': '',
                                         'errors': [], 'fix_hints': []})
    monkeypatch.setattr(
        'lib.motion_video._fill.check_composition_fill', lambda *a, **k: [])

    plain = _scene_author._full_gate(_CJK_NO_FACE, str(tmp_path),
                                     scene={'id': 'scene-001'})
    assert not any('render host' in f.lower() for f in plain)
