#!/usr/bin/env python3
"""tests/test_motion_video_author_resilience.py — transient faults vs defects.

Root-cause guards for the OTHER half of the shipped 0-of-8 job (job
motion_bb4245444177498d). Four of its six degraded scenes were a gate
misjudgement (guarded in test_motion_video_gate_verdict.py); the remaining
two, plus a third, had nothing to do with composition quality at all:

  * ``Claude subscription not logged in (no valid OAuth token)``
  * ``HTTPSConnectionPool(host='your-llm-gateway.example.com', port=443): Read timed out.
    (read timeout=119.2)``
  * the 60000-token budget, blown at 74040 and 67941 tokens

``author_scene`` treated ALL of those identically to "the model wrote a bad
composition": degrade to the gradient template. Two consequences, both
measured:

  1. One network blip threw away a composition the model had already written —
     the work only ever lived in memory.
  2. The engine then wrote that fallback card to ``index.html``, and
     ``engine._existing_composition`` compared only ``data-duration`` — so it
     could not tell a fallback card from an authored composition and adopted
     it on every later resume/regen. A scene degraded by a transient blip was
     pinned to the gradient card FOREVER; re-running the job could never retry
     its authoring.

Invariants pinned here:

  * a TRANSIENT fault is retried, and a scene that succeeds on a later attempt
    ends ``authored`` — not ``template``;
  * a genuine quality verdict still degrades (the retry must not launder bad
    compositions);
  * written work is persisted as a draft and resumed by a later call;
  * the draft lives OUTSIDE the renderer's project-root scan (a sibling
    ``*.html`` carrying ``data-composition-id`` makes the CLI reject the
    project with "Multiple root-level HTML files" — the first implementation
    did exactly that and broke every scene it meant to save);
  * a fallback card on disk is never adopted as a finished composition.
"""

from __future__ import annotations

import json
import os

import pytest

pytestmark = pytest.mark.unit

_AUTHORED = 'AUTHORED_MARKER'

GOOD_HTML = f'''<!doctype html>
<html><head><meta charset="UTF-8">
<script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
<style>*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{width:1080px;height:1440px;overflow:hidden;background:#000}}
#root{{position:relative;width:1080px;height:1440px;overflow:hidden}}
.bgfill{{position:absolute;inset:0;background:linear-gradient(160deg,#0b0f14,#1d3a5f)}}
.clip{{position:absolute;inset:0;display:grid;place-items:center}}
.eyebrow{{font-size:32px;color:rgba(255,255,255,.7)}}
.headline{{font-size:96px;font-weight:800;color:#fff;max-width:900px}}</style></head><body>
<div id="root" data-composition-id="main" data-start="0" data-duration="4.0"
     data-width="1080" data-height="1440">
<div class="bgfill"></div>
<section id="c1" class="clip" data-start="0" data-duration="4.0" data-track-index="1">
<div><div class="eyebrow" id="eb">CORE MECHANISM</div>
<h1 class="headline" id="hl">{_AUTHORED}</h1>
<div id="rule" style="height:2px;background:#4af;width:400px"></div></div></section></div>
<script>window.__timelines=window.__timelines||{{}};
const tl=gsap.timeline({{paused:true}});
tl.from('#eb',{{opacity:0,y:20,duration:.5}},0.1);
tl.from('#hl',{{opacity:0,y:56,duration:.7}},0.3);
tl.from('#rule',{{scaleX:0,transformOrigin:'left center',duration:.6}},0.55);
window.__timelines['main']=tl;</script></body></html>'''

#: Same document with the required root attribute removed → a real contract
#: violation, i.e. a QUALITY defect rather than an infrastructure fault.
BROKEN_HTML = GOOD_HTML.replace('data-composition-id="main"', '')

SCENE = {'id': 'scene-003', 'start': 0.0, 'end': 4.0,
         'text': '四种编辑操作:保留、替换、删除、插入。',
         'on_screen': _AUTHORED}


def _timeout_exc():
    import requests
    return requests.exceptions.ReadTimeout(
        "HTTPSConnectionPool(host='your-llm-gateway.example.com', port=443): Read timed "
        "out. (read timeout=119.20262455940247)")


def _write_call(html: str, idx: int = 0):
    return None, {'total_tokens': 4000, '_tool_calls': [{
        'id': f'c{idx}', 'type': 'function',
        'function': {'name': 'write_composition',
                     'arguments': json.dumps({'html': html})}}]}


@pytest.fixture
def author(monkeypatch, tmp_path):
    """author_scene with the CLI gates skipped and a scripted dispatcher.

    ``script`` steps: 'good' | 'broken' | 'timeout' | 'oauth' | 'quality' |
    'stop'. The LAST step repeats once exhausted.
    """
    monkeypatch.setenv('TOFU_HYPERFRAMES_BIN', '/nonexistent-cli')
    # Backoff must not make the suite slow.
    monkeypatch.setattr('lib.motion_video._scene_author._TRANSIENT_BACKOFF_S',
                        0.0)

    def run(script, *, scene_dir=None, attempts=3, token_budget=90000):
        seen = {'n': 0}

        def dispatch(messages, **kw):
            i = seen['n']
            seen['n'] += 1
            step = script[min(i, len(script) - 1)]
            if step == 'timeout':
                raise _timeout_exc()
            if step == 'oauth':
                raise RuntimeError('Claude subscription not logged in '
                                   '(no valid OAuth token)')
            if step == 'rate':
                raise RuntimeError('API HTTP 429: too many requests')
            if step == 'quality':
                raise ValueError('model produced structurally invalid output')
            if step == 'good':
                return _write_call(GOOD_HTML, i)
            if step == 'broken':
                return _write_call(BROKEN_HTML, i)
            return 'done', {'total_tokens': 500, '_tool_calls': []}

        monkeypatch.setattr('lib.llm_dispatch.api.dispatch_chat', dispatch)
        from lib.motion_video._scene_author import author_scene
        d = str(scene_dir or tmp_path)
        os.makedirs(d, exist_ok=True)
        res = author_scene(
            SCENE, d, width=1080, height=1440, duration=4.0,
            scene_index=3, total_scenes=8,
            transient_attempts=attempts, token_budget=token_budget)
        res['_calls'] = seen['n']
        res['_dir'] = d
        return res
    return run


# ══════════════════════════════════════════════════════════
#  transient faults are retried, not degraded
# ══════════════════════════════════════════════════════════

def test_one_read_timeout_still_yields_an_authored_scene(author):
    """THE regression: a single network blip must not cost the scene.

    Pre-fix this returned mode='template' after ONE ReadTimeout, throwing away
    a composition the model had already written.
    """
    res = author(['timeout', 'good', 'stop'])
    assert res['mode'] == 'authored', res.get('detail')
    assert _AUTHORED in res['html']
    assert res['_calls'] > 1, 'no retry happened'


def test_expired_oauth_token_is_retried(author):
    """The OAuth failure is a plain RuntimeError from the dispatcher, so an
    isinstance-based classifier would miss it — it must be matched on text."""
    res = author(['oauth', 'good', 'stop'])
    assert res['mode'] == 'authored', res.get('detail')


def test_rate_limit_is_retried(author):
    res = author(['rate', 'good', 'stop'])
    assert res['mode'] == 'authored', res.get('detail')


def test_transient_faults_are_bounded(author):
    """Retries are capped — a permanently broken gateway must still finish."""
    res = author(['timeout'], attempts=2)
    assert res['mode'] == 'template'
    assert 'persisted across 2 attempt' in res['detail'], res['detail']


# ══════════════════════════════════════════════════════════
#  a real quality verdict still degrades (no laundering)
# ══════════════════════════════════════════════════════════

def test_contract_violation_still_degrades(author):
    """The retry must NOT turn a bad composition into an accepted one."""
    res = author(['broken', 'stop'])
    assert res['mode'] == 'template', res.get('detail')
    assert 'static gate' in res['detail']


def test_non_transient_exception_is_not_retried(author):
    """A ValueError from the model's own output is a quality verdict: one
    attempt, then degrade — retrying it would just burn tokens."""
    res = author(['quality'], attempts=3)
    assert res['mode'] == 'template'
    calls = res['_calls']
    assert calls == 1, f'a quality failure must not be retried (got {calls})'


def test_no_composition_written_degrades(author):
    res = author(['stop'])
    assert res['mode'] == 'template'
    assert 'no composition' in res['detail']


# ══════════════════════════════════════════════════════════
#  written work survives — and is resumed
# ══════════════════════════════════════════════════════════

def test_draft_is_persisted_when_every_attempt_fails(author, tmp_path):
    from lib.motion_video._scene_author import DRAFT_FILENAME
    d = tmp_path / 'sc-draft'
    res = author(['good', 'timeout'], scene_dir=d, attempts=2)
    assert res['mode'] == 'template'
    draft = d / DRAFT_FILENAME
    assert draft.is_file(), 'the written composition must survive on disk'
    assert _AUTHORED in draft.read_text(encoding='utf-8')


def test_a_later_call_resumes_the_draft(author, tmp_path):
    """Second invocation must NOT start from a blank page."""
    d = tmp_path / 'sc-resume'
    first = author(['good', 'timeout'], scene_dir=d, attempts=2)
    assert first['mode'] == 'template'
    # The model writes NOTHING this time — success can only come from the draft.
    second = author(['stop'], scene_dir=d, attempts=1)
    assert second['mode'] == 'authored', second.get('detail')
    assert _AUTHORED in second['html']


def test_draft_is_cleared_once_authored(author, tmp_path):
    from lib.motion_video._scene_author import DRAFT_FILENAME
    d = tmp_path / 'sc-clear'
    res = author(['good', 'stop'], scene_dir=d)
    assert res['mode'] == 'authored', res.get('detail')
    assert not (d / DRAFT_FILENAME).is_file(), (
        'a passing composition leaves no draft behind')


def test_stale_draft_is_discarded(author, tmp_path):
    """A draft whose duration no longer matches the timeline must not be
    resumed, or the scene renders at the wrong length."""
    from lib.motion_video._scene_author import DRAFT_FILENAME, load_draft
    d = tmp_path / 'sc-stale'
    (d / os.path.dirname(DRAFT_FILENAME)).mkdir(parents=True, exist_ok=True)
    (d / DRAFT_FILENAME).write_text(GOOD_HTML, encoding='utf-8')  # 4.0s
    assert load_draft(str(d), 4.0)
    assert load_draft(str(d), 9.5) == '', 'a stale draft must be discarded'


def test_draft_lives_outside_the_renderer_project_scan():
    """The draft must NOT sit beside index.html.

    The renderer scans the project root and rejects the project with
    "Multiple root-level HTML files with data-composition-id" as soon as a
    second composition-looking file is there. The first implementation put the
    draft in the scene dir and every resumed scene failed its gate — the fix
    broke exactly what it was meant to save.
    """
    from lib.motion_video._scene_author import DRAFT_FILENAME
    assert os.path.dirname(DRAFT_FILENAME), (
        'the draft must live in a subdirectory, not the project root')
    assert DRAFT_FILENAME.startswith('.'), (
        'the draft dir should be dot-prefixed so tooling ignores it')


# ══════════════════════════════════════════════════════════
#  the fallback card must never be adopted as finished work
# ══════════════════════════════════════════════════════════

def test_template_composition_is_self_identifying():
    from lib.motion_video._template import (TEMPLATE_MARKER,
                                            is_template_composition,
                                            render_scene_html)
    html = render_scene_html(SCENE, width=1080, height=1440, duration=4.0,
                             scene_index=3, total_scenes=8)
    assert TEMPLATE_MARKER in html
    assert is_template_composition(html) is True
    assert is_template_composition(GOOD_HTML) is False


def test_resume_never_adopts_a_degraded_fallback_card(tmp_path):
    """A fallback card on disk must be re-authored, not reused.

    This is the lock-in half of the defect: pre-fix, _existing_composition
    compared only data-duration, so a scene degraded by one network blip was
    pinned to the gradient card for every future resume and regen.
    """
    from lib.motion_video._template import render_scene_html
    from lib.motion_video.engine import _existing_composition

    idx = tmp_path / 'index.html'
    idx.write_text(render_scene_html(SCENE, width=1080, height=1440,
                                     duration=4.0, scene_index=3,
                                     total_scenes=8), encoding='utf-8')
    assert _existing_composition(str(idx), 4.0) is None, (
        'the resume path must refuse to adopt the fallback card')

    # …while a real authored composition IS still resumed (the whole point of
    # the resume path — it must not become a no-op).
    idx.write_text(GOOD_HTML, encoding='utf-8')
    got = _existing_composition(str(idx), 4.0)
    assert got is not None and _AUTHORED in got


# ══════════════════════════════════════════════════════════
#  classification + NEUTER
# ══════════════════════════════════════════════════════════

@pytest.mark.parametrize('exc,expected', [
    (None, True),                                     # ReadTimeout
    (RuntimeError('Claude subscription not logged in '
                  '(no valid OAuth token)'), True),
    (RuntimeError('API HTTP 429: rate limit exceeded'), True),
    (RuntimeError('API HTTP 503: service unavailable'), True),
    (ValueError('no JSON object in reply'), False),
    (KeyError('html'), False),
    (TypeError('bad argument'), False),
])
def test_transient_classification(exc, expected):
    from lib.motion_video._scene_author import is_transient_fault
    e = _timeout_exc() if exc is None else exc
    assert is_transient_fault(e) is expected, f'{type(e).__name__}: {e}'


def test_NEUTER_without_classification_a_blip_destroys_the_scene(
        author, monkeypatch):
    """NEUTER: declare every fault non-transient → the SAME single timeout
    degrades the scene again, exactly as the shipped job did.

    Teeth of the whole batch: if this stops failing, the retry policy is gone.
    """
    monkeypatch.setattr(
        'lib.motion_video._scene_author.is_transient_fault',
        lambda exc: False)
    res = author(['timeout', 'good', 'stop'])
    assert res['mode'] == 'template', (
        'NEUTER did not bite: with classification amputated a transient blip '
        'must fall through to the template, which is the bug we fixed')


def test_NEUTER_without_the_marker_the_fallback_is_locked_in(
        tmp_path, monkeypatch):
    """NEUTER: make the template unidentifiable → _existing_composition adopts
    the gradient card again, restoring the permanent lock-in."""
    from lib.motion_video._template import render_scene_html
    from lib.motion_video.engine import _existing_composition

    monkeypatch.setattr('lib.motion_video._template.is_template_composition',
                        lambda html: False)
    idx = tmp_path / 'index.html'
    idx.write_text(render_scene_html(SCENE, width=1080, height=1440,
                                     duration=4.0, scene_index=3,
                                     total_scenes=8), encoding='utf-8')
    assert _existing_composition(str(idx), 4.0) is not None, (
        'NEUTER did not bite: without the template marker the resume path '
        'must adopt the fallback card, which is the lock-in we fixed')
