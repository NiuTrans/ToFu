"""Guards for the vertical-fill gate.

Why this file exists: the craft guide gained vertical-composition instructions
and the measured span went 58.9% → 87.0%, but that improvement was GUIDANCE
ONLY. No function measured fill, so if the next model ignored the paragraph
every frame would regress to 34% dead space with the whole suite still green —
the charter's own criterion ("if production dropped this behaviour today, would
the test go red?") answered no.

Discipline: these assert RESULTS — the findings the gate returns for a given
composition, and the findings that reach the quality verdict — never the
threshold constants. Re-tuning `MIN_VERTICAL_SPAN` cannot make them falsely
green, while removing the gate or its wiring turns them red.
"""

from __future__ import annotations

import glob
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── Synthetic compositions with KNOWN geometry ────────────
#
# Deliberately explicit pixel boxes rather than authored scenes: the assertion
# is about the gate's verdict on a known shape. The real-corpus check below is
# what bounds false positives.

def _frame(inner: str, *, height: int = 1440) -> str:
    return (f'<html><body style="margin:0">'
            f'<div data-composition-id="s1" data-width="1080" '
            f'data-height="{height}" '
            f'style="position:relative;width:1080px;height:{height}px;'
            f'overflow:hidden">{inner}</div></body></html>')


#: One centred line — the template shape this whole effort is replacing.
_UNDERFILLED = _frame(
    '<div style="position:absolute;top:660px;left:80px;width:920px;'
    'height:120px">One centred line</div>')

#: Content pooled in the upper half: tall-ish span, huge bottom dead band.
_BOTTOM_HEAVY = _frame(
    '<div style="position:absolute;top:60px;left:80px;width:920px;'
    'height:60px">eyebrow</div>'
    '<div style="position:absolute;top:200px;left:80px;width:920px;'
    'height:600px">headline block</div>')

#: Distributed across the tall axis, as the guide instructs.
_WELL_FILLED = _frame(
    '<div style="position:absolute;top:100px;left:80px;width:920px;'
    'height:60px">eyebrow</div>'
    '<div style="position:absolute;top:300px;left:80px;width:920px;'
    'height:420px">headline</div>'
    '<div style="position:absolute;top:900px;left:80px;width:920px;'
    'height:200px">supporting row</div>'
    '<div style="position:absolute;top:1290px;left:80px;width:920px;'
    'height:40px">03 / 08</div>')


def _fill_or_skip(html: str):
    """Measure, or skip when this host has no browser.

    A skip is honest here: the gate's own contract is that an unavailable
    browser is an infrastructure outcome, so there is nothing to assert.
    """
    from lib.motion_video import measure_fill

    m = measure_fill(html)
    if m is None:
        pytest.skip('no headless browser on this host to measure fill')
    return m


def test_a_centred_single_line_is_reported_as_underfilled():
    """The exact shape users described as "text flying around, no formatting"."""
    from lib.motion_video import check_composition_fill

    _fill_or_skip(_UNDERFILLED)
    findings = check_composition_fill(_UNDERFILLED)
    assert findings, 'a one-line centred frame passes the fill gate'
    assert any('vertical span' in f for f in findings)
    # The finding must name a remedy, not just a verdict — it is fed to the
    # author's repair loop, which cannot act on "too small".
    assert any('space-between' in f or 'Distribute' in f for f in findings)


def test_a_bottom_heavy_frame_is_reported_even_when_the_span_is_large():
    """Span alone is not sufficient — content can be tall but sit high.

    Without the dead-band arm, a composition filling the top two thirds and
    leaving a third empty below reads as clean.
    """
    from lib.motion_video import check_composition_fill

    m = _fill_or_skip(_BOTTOM_HEAVY)
    findings = check_composition_fill(_BOTTOM_HEAVY)
    assert findings, f'bottom-heavy frame passed the gate (measured {m})'
    assert any('below the last element' in f for f in findings), (
        f'the dead-band arm did not fire; findings={findings}')


def test_a_well_distributed_frame_is_clean():
    """The complement: the gate must not be a constant reject.

    Without this, "always report a finding" satisfies every assertion above
    while degrading every scene and making the axis meaningless.
    """
    from lib.motion_video import check_composition_fill

    m = _fill_or_skip(_WELL_FILLED)
    assert check_composition_fill(_WELL_FILLED) == [], (
        f'a frame distributed across the tall axis was flagged (measured {m})')


def test_a_full_bleed_backdrop_does_not_fake_a_full_frame():
    """A 100%x100% gradient must not count as content.

    This is the loophole that would make the gate useless: every composition
    has a full-bleed background child, so counting it reports a perfect span
    for a frame holding one centred line.
    """
    from lib.motion_video import check_composition_fill

    with_backdrop = _frame(
        '<div style="position:absolute;inset:0;'
        'background:linear-gradient(#123,#456)"></div>'
        '<div style="position:absolute;top:660px;left:80px;width:920px;'
        'height:120px">One centred line</div>')
    _fill_or_skip(with_backdrop)
    assert check_composition_fill(with_backdrop), (
        'a full-bleed backdrop was counted as content, so the gate reports a '
        'full frame for a composition that holds one line')


def test_an_unmeasurable_composition_is_not_charged_as_a_defect(monkeypatch):
    """No browser is an ENV problem, not an ugly frame.

    Mirrors the CLI gates' env_missing rule: without this, every scene on a
    host without Chromium is marked degraded and the film looks broken for a
    reason the composition cannot fix.
    """
    from lib.motion_video import _fill

    monkeypatch.setattr(_fill, 'measure_fill', lambda *a, **k: None)
    assert _fill.check_composition_fill('<html></html>') == []


# ── False-positive bound on the REAL corpus ───────────────

def _authored_corpus() -> list[tuple[str, str]]:
    """Every authored composition on disk, as (label, html).

    The template fallback is excluded by line count: it is a fixed short file
    and is not what this gate judges (an explicitly-template film is the tier
    the user chose). Empty on a host that has run no jobs — callers skip
    rather than assert over nothing (charter: verify the scan surface).
    """
    out: list[tuple[str, str]] = []
    for path in sorted(glob.glob(os.path.join(
            _ROOT, 'data', 'motion_video', 'jobs', '*', 'scenes', '*',
            'index.html'))):
        try:
            with open(path, encoding='utf-8') as f:
                html = f.read()
        except OSError as e:
            print(f'[corpus] skipped {path}: {e}')
            continue
        if len(html.splitlines()) <= 80:
            continue
        label = '/'.join(path.split(os.sep)[-4:-1])
        out.append((label, html))
    return out


def test_the_corpus_scan_sees_the_authored_scenes():
    """Verify the SCAN SURFACE before trusting the bound built on it."""
    corpus = _authored_corpus()
    if not corpus:
        pytest.skip('no motion jobs on this host')
    assert len(corpus) >= 3, (
        f'the scan found only {len(corpus)} authored composition(s) — the '
        f'false-positive bound below would be measured over almost nothing: '
        f'{[c[0] for c in corpus]}')


def test_the_gate_does_not_reject_most_of_the_real_corpus():
    """The bound that keeps the thresholds honest.

    Measured at calibration time: the 9 authored scenes on this host span
    49.6 / 58.5 / 59.2 | 68.2 / 69.5 / 72.2 / 78.0 / 86.1 / 87.8 % with bottom
    dead-bands 25.2 / 34.0 / 33.3 | 20.3 / 6.9 / 13.9 / 2.9 / 6.9 / 6.1 %. The
    shipped thresholds sit in those gaps, rejecting the three scenes that
    prompted this work and nothing else. If a majority of real scenes trip the
    gate, the GATE is wrong rather than the compositions — degrading good
    scenes to plain template cards would be worse than the bug being fixed.
    """
    from lib.motion_video import check_composition_fill, measure_fill

    corpus = _authored_corpus()
    if not corpus:
        pytest.skip('no motion jobs on this host')
    if measure_fill(corpus[0][1]) is None:
        pytest.skip('no headless browser on this host to measure fill')

    flagged = [label for label, html in corpus if check_composition_fill(html)]
    assert len(flagged) <= len(corpus) // 2, (
        f'{len(flagged)}/{len(corpus)} real authored scenes tripped the fill '
        f'gate — at this rate it degrades good compositions: {flagged}')


# ── The WIRING, not just the helper ───────────────────────
#
# Charter: testing the helper is not testing the call site. Both assertions
# below drive real functions, so deleting the call turns them red.

def test_fill_findings_reach_the_quality_verdict():
    """A finding that only logs is not a gate."""
    from lib.motion_video import engine

    class _MV:
        @staticmethod
        def is_infra_category(category):
            from lib.motion_video import is_infra_category
            return is_infra_category(category)

        @staticmethod
        def check_composition_fill(html):
            from lib.motion_video import check_composition_fill
            return check_composition_fill(html)

        @staticmethod
        def check_text_fidelity(_html, _scene):
            return []

        @staticmethod
        def check_project(_d, **_k):
            return {'ok': True, 'errors': []}

    fill = _fill_or_skip(_UNDERFILLED)
    findings = engine._scene_gate_findings(
        _MV(), '/tmp/x', 'scene-005',
        scene={'id': 'scene-005', 'text': 'x'}, html=_UNDERFILLED, fill=fill)
    assert findings, (
        'an under-filled composition passes the engine gate untouched — the '
        'CLI gates cannot see it, so nothing would')
    verdict = engine._quality_verdict(
        degraded_narration=False,
        scene_gate_issues={'scene-005': findings},
        authoring=True, authored=8, total=8)
    assert verdict['degraded'] is True and 'scene-005' in verdict['reason']


def test_fill_survives_an_infrastructure_skip_of_the_cli_gates():
    """Fill needs no hyperframes CLI, so env_missing must not swallow it.

    The CLI gates correctly return empty on env_missing; fill is measured from
    HTML we already hold in a browser boot of its own, so it must be collected
    on the same side of that exemption as text fidelity.
    """
    from lib.motion_video import engine

    class _MV:
        @staticmethod
        def is_infra_category(category):
            from lib.motion_video import is_infra_category
            return is_infra_category(category)

        @staticmethod
        def check_composition_fill(html):
            from lib.motion_video import check_composition_fill
            return check_composition_fill(html)

        @staticmethod
        def check_text_fidelity(_html, _scene):
            return []

        @staticmethod
        def check_project(_d, **_k):
            return {'ok': False, 'category': 'env_missing', 'errors': []}

    fill = _fill_or_skip(_UNDERFILLED)
    findings = engine._scene_gate_findings(
        _MV(), '/tmp/x', 'scene-005',
        scene={'id': 'scene-005', 'text': 'x'}, html=_UNDERFILLED, fill=fill)
    assert findings, (
        'fill was dropped along with the CLI gates on env_missing; it needs '
        'no toolchain and must still be reported')


def test_the_author_can_repair_a_fill_finding_before_the_film_ships():
    """The finding must ride the author's own feedback loop.

    Reported only at the end, an under-filled frame can no longer be fixed —
    the scene just degrades. ``_full_gate`` is what ``composition_check``
    calls, so the check must live there too.
    """
    import tempfile

    from lib.motion_video._scene_author import _full_gate

    _fill_or_skip(_UNDERFILLED)
    # The composition must satisfy the CONTRACT (regex) pass, or _full_gate
    # returns early by design and never reaches the browser gates — so a
    # contract-incomplete fixture would test the early return, not the fill
    # wiring. Timeline registration is part of that contract.
    html = _UNDERFILLED.replace(
        'data-composition-id="s1"',
        'data-composition-id="s1" data-duration="4"').replace(
        '</body>',
        '<script>window.__timelines = window.__timelines || {};'
        'window.__timelines["s1"] = gsap.timeline({paused:true});</script>'
        '</body>')
    with tempfile.TemporaryDirectory() as d:
        errors = _full_gate(html, d, scene={'id': 'scene-005', 'text': 'x'},
                            advisory=True)
    assert not any('__timelines' in e for e in errors), (
        f'fixture is still contract-incomplete, so the gate returned before '
        f'measuring fill: {errors}')
    assert any('vertical span' in e or 'below the last element' in e
               for e in errors), (
        f'composition_check does not report under-fill, so the author never '
        f'learns about it and cannot repair it; got {errors}')


def test_under_fill_is_advice_not_a_reason_to_ship_the_template():
    """Under-fill must NOT be an accept/reject verdict.

    Measured: the zero-LLM template itself scores 58% span / 38% bottom dead —
    it cannot pass its own fill gate. So rejecting an under-filled AUTHORED
    composition degrades it to a template that is no better on the very axis
    that triggered the rejection, and burns an agent loop to get there. The
    complement of the test above: the same composition, judged on the
    non-advisory path, must come back clean.
    """
    import tempfile

    from lib.motion_video._scene_author import _full_gate

    _fill_or_skip(_UNDERFILLED)
    html = _UNDERFILLED.replace(
        'data-composition-id="s1"',
        'data-composition-id="s1" data-duration="4"').replace(
        '</body>',
        '<script>window.__timelines = window.__timelines || {};'
        'window.__timelines["s1"] = gsap.timeline({paused:true});</script>'
        '</body>')
    with tempfile.TemporaryDirectory() as d:
        verdict = _full_gate(html, d, scene={'id': 'scene-005', 'text': 'x'})
    assert not any('vertical span' in e or 'below the last element' in e
                   for e in verdict), (
        f'under-fill is being treated as a rejection, which sends the scene '
        f'to a template that measures WORSE on the same axis: {verdict}')


def test_the_zero_llm_template_cannot_pass_its_own_fill_gate():
    """Pins the measurement the advisory/verdict split rests on.

    If a future template redesign actually fills the frame, this turns red and
    the split can be revisited — the reasoning above stops being true.
    """
    from lib.motion_video import check_composition_fill
    from lib.motion_video._template import render_scene_html

    html = render_scene_html(
        {'id': 'scene-001', 'start': 0.0, 'end': 4.0,
         'text': '空气分子把阳光散射开来。', 'visual': ''},
        width=1080, height=1440, duration=4.0, scene_index=1, total_scenes=3)
    _fill_or_skip(html)
    assert check_composition_fill(html), (
        'the template now passes the fill gate — revisit whether under-fill '
        'should still be advisory-only rather than a rejection')


# ── The craft guide must state the truncation rule ────────

def test_the_author_is_told_to_rewrite_rather_than_truncate():
    """The measured defect: a headline cut to a dangling noun to make it fit.

    ``硫化物固态电解质取代液态`` shipped with the final noun dropped. Every gate
    passed it — a truncated string is well-formed, correctly sized and
    contrast-compliant; only a reader notices. Density pressure from the
    vertical-fill guidance makes this MORE likely, so the instruction ships
    with it. Driven through ``_build_prompt`` (what the model receives), not
    by reading the file: the guide is truncated into the prompt, so a section
    appended past the limit would be invisible while the file contains it.
    """
    from lib.motion_video._scene_author import _build_prompt

    prompt = _build_prompt(
        {'id': 'scene-001', 'text': '固态电池能量密度翻倍', 'visual': 'stat card'},
        width=1080, height=1440, duration=4.0, scene_index=1, total_scenes=3)
    low = prompt.lower()
    assert 'truncat' in low, (
        'the author is never told not to truncate copy to fit — the measured '
        'dangling-noun headline is the result')
    assert 'complete phrase' in low, (
        'the guidance names no positive remedy (rewrite shorter, complete)')
