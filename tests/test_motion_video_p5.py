"""tests/test_motion_video_p5.py — Per-scene composition author (P5) suite.

Covers the per-scene authoring contract in docs/modules/production.md:
each scene gets its own bounded agent loop that authors a bespoke composition,
with the zero-LLM template as the always-available floor.

Load-bearing behaviours under test:
  * a good author run produces the AUTHORED html (not the template);
  * every failure mode degrades to the template — never raises, never fails
    the film: no composition written / gate never satisfied / LLM raises /
    abort already set;
  * the static gate is enforced on the AUTHORED output (an author cannot ship
    a composition the gate rejects);
  * the per-scene token budget (拍板 #3) stops the loop;
  * the narrow toolset really is narrow (no render/concat/mux reachable);
  * authoring is ON by default, with a per-job / per-env opt-OUT;
  * the engine's compose stage skips re-authoring a composition already on
    disk with a matching duration (resume — no re-spent agent loop).

The LLM is faked at the dispatch seam — no network.
"""

from __future__ import annotations

import json
import os
import threading
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

from lib.motion_video import _scene_author as sa
from lib.motion_video import engine as eng


def _scene(sid='scene-001', text='空气分子把阳光散射开来。'):
    return {'id': sid, 'start': 0.0, 'end': 4.0, 'text': text, 'visual': ''}


def _good_html(duration=4.0, width=1080, height=1440):
    """A composition that PASSES check_composition_html (built off the real
    skeleton so the test can't drift from the gate)."""
    from lib.motion_video._template import render_scene_html
    return render_scene_html(_scene(), width=width, height=height,
                             duration=duration, scene_index=1, total_scenes=3)


def _fake_llm(monkeypatch, script):
    """Fake dispatch_chat. ``script`` is a list of (content, tool_calls) per
    round; tool_calls is a list of (name, args_dict)."""
    calls = {'n': 0}

    def fake_dispatch_chat(messages, **kw):
        i = min(calls['n'], len(script) - 1)
        content, tcs = script[i]
        calls['n'] += 1
        tool_calls = [
            {'id': f'tc{j}', 'type': 'function',
             'function': {'name': name, 'arguments': json.dumps(args)}}
            for j, (name, args) in enumerate(tcs)
        ]
        usage = {'total_tokens': 1000}
        if tool_calls:
            usage['_tool_calls'] = tool_calls
        return content, usage

    monkeypatch.setattr('lib.llm_dispatch.api.dispatch_chat', fake_dispatch_chat)
    return calls


def test_scene_prompts_put_the_shared_contract_before_dynamic_fields(
        monkeypatch):
    """Sibling scenes expose one exact, large provider-cache prefix."""
    guides = {
        'COMPOSITION_CONTRACT.md': 'CONTRACT_SENTINEL',
        'MOTION_CRAFT.md': 'CRAFT_SENTINEL',
        'skeleton.html': 'SKELETON_SENTINEL',
    }
    monkeypatch.setattr(
        sa, '_read_guide', lambda name, limit=12000: guides[name])
    monkeypatch.setattr('lib.motion_video._craft.craft_index', lambda: '')

    prompts = [
        sa._build_prompt(
            _scene(sid, narration), width=1080, height=1440,
            duration=duration, scene_index=index, total_scenes=2)
        for sid, narration, duration, index in (
            ('alpha-scene', 'ALPHA_NARRATION', 4.0, 1),
            ('omega-scene', 'OMEGA_NARRATION', 7.0, 2),
        )
    ]
    prefixes = [prompt.split('## This scene\n', 1)[0]
                for prompt in prompts]

    assert prefixes[0] == prefixes[1]
    assert 'The shared film frame is 1080x1440 px.' in prefixes[0]
    for marker in guides.values():
        assert marker in prefixes[0]
    for dynamic in ('alpha-scene', 'omega-scene', 'ALPHA_NARRATION',
                    'OMEGA_NARRATION', '4.0 seconds', '7.0 seconds'):
        assert dynamic not in prefixes[0]
    assert prompts[0].index('SKELETON_SENTINEL') < prompts[0].index(
        'alpha-scene')


def test_prepared_scene_prompt_context_preserves_exact_prompt_and_reads_once(
        monkeypatch):
    reads = []
    guides = {
        'COMPOSITION_CONTRACT.md': 'contract',
        'MOTION_CRAFT.md': 'craft',
        'skeleton.html': 'skeleton',
    }

    def _guide(name, limit=12000):
        reads.append((name, limit))
        return guides[name]

    monkeypatch.setattr(sa, '_read_guide', _guide)
    monkeypatch.setattr('lib.motion_video._craft.craft_index', lambda: '')
    context = sa.prepare_author_prompt_context(
        width=1080, height=1440, total_scenes=2)
    first = sa._build_prompt(
        _scene('scene-a', 'same narration'), width=1080, height=1440,
        duration=4.0, scene_index=1, total_scenes=2,
        prompt_context=context)
    second = sa._build_prompt(
        _scene('scene-a', 'same narration'), width=1080, height=1440,
        duration=4.0, scene_index=1, total_scenes=2)

    assert first == second
    assert reads == [
        ('COMPOSITION_CONTRACT.md', 12000),
        ('MOTION_CRAFT.md', 12000),
        ('skeleton.html', 6000),
    ] * 2


def test_scene_prompt_context_rejects_cross_film_geometry(monkeypatch):
    monkeypatch.setattr(sa, '_read_guide', lambda *a, **k: 'guide')
    context = sa.prepare_author_prompt_context(
        width=1080, height=1440, total_scenes=2)

    with pytest.raises(ValueError, match='mismatch'):
        sa._build_prompt(
            _scene(), width=1920, height=1080, duration=4.0,
            scene_index=1, total_scenes=2, prompt_context=context)


def test_parallel_author_dependency_prewarm_deduplicates_theme_fonts(
        monkeypatch):
    craft_calls = []
    font_calls = []
    face = SimpleNamespace(
        id='shared-face',
        sources=(SimpleNamespace(weight=400), SimpleNamespace(weight=700)),
    )
    theme = SimpleNamespace(fonts={
        'display': 'shared-face', 'body': 'shared-face',
        'latin': 'shared-face',
    })
    monkeypatch.setattr(
        'lib.motion_video._craft.ensure_craft_corpus',
        lambda: craft_calls.append('craft') or True)
    monkeypatch.setattr('lib.design_sys.fonts.get_font', lambda _font_id: face)
    monkeypatch.setattr(
        'lib.design_sys.fonts.ensure_font',
        lambda font_id, weight: font_calls.append((font_id, weight))
        or f'/fonts/{font_id}-{weight}.woff2')

    assert sa.prepare_parallel_author_dependencies(
        theme=theme, needs_craft=True) is True
    assert craft_calls == ['craft']
    assert font_calls == [('shared-face', 400), ('shared-face', 700)]


def test_default_prompt_does_not_scan_the_unused_craft_catalog(monkeypatch):
    monkeypatch.setattr(
        'lib.motion_video._craft.craft_index',
        lambda: (_ for _ in ()).throw(
            AssertionError('default scene scanned the craft catalog')))

    prompt = sa._build_prompt(
        _scene(), width=1080, height=1440, duration=4.0,
        scene_index=1, total_scenes=2)

    assert 'Craft corpus (deep reference' not in prompt


def test_explicit_craft_browse_reads_and_exposes_the_catalog(monkeypatch):
    monkeypatch.setattr('lib.motion_video._craft.craft_index',
                        lambda: 'CRAFT_INDEX_SENTINEL')
    scene = _scene()
    scene['allow_craft_browse'] = True

    prompt = sa._build_prompt(
        scene, width=1080, height=1440, duration=4.0,
        scene_index=1, total_scenes=2)

    assert 'Craft corpus (deep reference' in prompt
    assert 'CRAFT_INDEX_SENTINEL' in prompt


@pytest.mark.parametrize(('allow_browse', 'expected_installs'), [
    (False, 0),
    (True, 1),
])
def test_author_installs_craft_only_for_explicit_browse(
        monkeypatch, tmp_path, allow_browse, expected_installs):
    installs = []
    monkeypatch.setattr(
        'lib.motion_video._craft.ensure_craft_corpus',
        lambda: installs.append('install') or True)
    monkeypatch.setattr('lib.motion_video._craft.craft_index',
                        lambda: 'CRAFT_INDEX_SENTINEL')
    html = _good_html()
    _fake_llm(monkeypatch, [
        ('', [('write_composition', {'html': html})]),
        ('done', []),
    ])
    scene = _scene()
    if allow_browse:
        scene['allow_craft_browse'] = True

    result = sa.author_scene(
        scene, str(tmp_path), width=1080, height=1440,
        duration=4.0, scene_index=1, total_scenes=3)

    assert result['mode'] == 'authored'
    assert len(installs) == expected_installs


# ══════════════════════════════════════════════════════════
#  Happy path
# ══════════════════════════════════════════════════════════

def test_author_returns_authored_html(monkeypatch, tmp_path):
    html = _good_html()
    _fake_llm(monkeypatch, [
        ('', [('write_composition', {'html': html})]),
        ('done', []),
    ])
    res = sa.author_scene(_scene(), str(tmp_path), width=1080, height=1440,
                          duration=4.0, scene_index=1, total_scenes=3)
    assert res['mode'] == 'authored'
    assert res['html'] == html
    assert res['tokens'] > 0


def test_author_iterates_after_a_failing_gate(monkeypatch, tmp_path):
    """A first attempt that fails the gate can be repaired in a later round."""
    bad = '<html><body>nope</body></html>'
    good = _good_html()
    _fake_llm(monkeypatch, [
        ('', [('write_composition', {'html': bad + 'x' * 300})]),
        ('', [('write_composition', {'html': good})]),
        ('done', []),
    ])
    res = sa.author_scene(_scene(), str(tmp_path), width=1080, height=1440,
                          duration=4.0, scene_index=1, total_scenes=3)
    assert res['mode'] == 'authored'
    assert res['html'] == good


# ══════════════════════════════════════════════════════════
#  Degradation — one scene must never fail the film
# ══════════════════════════════════════════════════════════

def test_degrades_when_nothing_written(monkeypatch, tmp_path):
    _fake_llm(monkeypatch, [('I will think about it.', [])])
    res = sa.author_scene(_scene(), str(tmp_path), width=1080, height=1440,
                          duration=4.0, scene_index=1, total_scenes=3)
    assert res['mode'] == 'template'
    assert res['ok'] is True
    from lib.motion_video._gates import check_composition_html
    assert check_composition_html(res['html']) == []   # the floor is always valid


def test_degrades_when_gate_never_satisfied(monkeypatch, tmp_path):
    junk = '<html><body>' + ('x' * 400) + '</body></html>'
    _fake_llm(monkeypatch, [('', [('write_composition', {'html': junk})])])
    res = sa.author_scene(_scene(), str(tmp_path), width=1080, height=1440,
                          duration=4.0, scene_index=1, total_scenes=3)
    assert res['mode'] == 'template'
    assert 'static gate' in res['detail']


def test_degrades_when_llm_raises(monkeypatch, tmp_path):
    def boom(messages, **kw):
        raise RuntimeError('provider exploded')
    monkeypatch.setattr('lib.llm_dispatch.api.dispatch_chat', boom)
    res = sa.author_scene(_scene(), str(tmp_path), width=1080, height=1440,
                          duration=4.0, scene_index=1, total_scenes=3)
    assert res['mode'] == 'template'
    assert 'author loop error' in res['detail']


def test_degrades_when_already_aborted(monkeypatch, tmp_path):
    ev = threading.Event()
    ev.set()
    called = {'n': 0}
    monkeypatch.setattr('lib.llm_dispatch.api.dispatch_chat',
                        lambda m, **kw: called.__setitem__('n', called['n'] + 1))
    res = sa.author_scene(_scene(), str(tmp_path), width=1080, height=1440,
                          duration=4.0, scene_index=1, total_scenes=3,
                          abort_event=ev)
    assert res['mode'] == 'template'
    assert called['n'] == 0  # never even dispatched


def test_author_dispatch_uses_abort_and_production_retry_budgets(
        monkeypatch, tmp_path):
    monkeypatch.setenv('TOFU_PRODUCTION_LLM_MAX_429_ATTEMPTS', '7')
    seen = {}

    def complete(messages, **kwargs):
        seen.update(kwargs)
        return 'done', {'total_tokens': 10, '_tool_calls': []}

    monkeypatch.setattr('lib.llm_dispatch.api.dispatch_chat', complete)
    res = sa.author_scene(
        _scene(), str(tmp_path), width=1080, height=1440,
        duration=4.0, scene_index=1, total_scenes=3)

    assert res['mode'] == 'template'
    assert seen['max_retries'] == 2
    assert seen['max_429_attempts'] == 7
    assert callable(seen['abort_check'])
    assert seen['abort_check']() is False


def test_abort_during_author_dispatch_is_not_retried(monkeypatch, tmp_path):
    from lib.llm_errors import AbortedError

    abort_event = threading.Event()
    calls = []

    def interrupted(messages, **kwargs):
        calls.append('dispatch')
        assert kwargs['abort_check']() is False
        abort_event.set()
        raise AbortedError('stopped')

    monkeypatch.setattr('lib.llm_dispatch.api.dispatch_chat', interrupted)
    res = sa.author_scene(
        _scene(), str(tmp_path), width=1080, height=1440,
        duration=4.0, scene_index=1, total_scenes=3,
        abort_event=abort_event, transient_attempts=3)

    assert res['mode'] == 'template'
    assert 'aborted' in res['detail']
    assert calls == ['dispatch']


def test_neuter_gate_check_proves_degradation_is_loadbearing(monkeypatch, tmp_path):
    """NEUTER: make the final gate always pass → junk html would ship as
    'authored'. Proves the post-loop gate is what forces degradation."""
    junk = '<html><body>' + ('x' * 400) + '</body></html>'
    _fake_llm(monkeypatch, [('', [('write_composition', {'html': junk})])])
    monkeypatch.setattr(sa, 'author_scene', sa.author_scene)  # keep symbol
    monkeypatch.setattr('lib.motion_video._gates.check_composition_html',
                        lambda html: [])
    res = sa.author_scene(_scene(), str(tmp_path), width=1080, height=1440,
                          duration=4.0, scene_index=1, total_scenes=3)
    assert res['mode'] == 'authored'   # junk shipped — gate was load-bearing


# ══════════════════════════════════════════════════════════
#  Cost caps (拍板 #3)
# ══════════════════════════════════════════════════════════

def test_token_budget_stops_the_loop(monkeypatch, tmp_path):
    """Each round burns 1000 tokens; a 1500 budget must stop after round 2."""
    _fake_llm(monkeypatch, [('', [('composition_check', {})])] * 10)
    res = sa.author_scene(_scene(), str(tmp_path), width=1080, height=1440,
                          duration=4.0, scene_index=1, total_scenes=3,
                          token_budget=1500)
    assert res['mode'] == 'template'      # never wrote anything
    assert res['tokens'] <= 3000          # stopped early, not 8 rounds


def test_tools_continue_past_former_round_cap(monkeypatch, tmp_path):
    script = [('', [('composition_check', {})])] * 8 + [('done', [])]
    calls = _fake_llm(monkeypatch, script)
    sa.author_scene(_scene(), str(tmp_path), width=1080, height=1440,
                    duration=4.0, scene_index=1, total_scenes=3,
                    token_budget=10 ** 9)
    assert calls['n'] == 9


def test_removed_max_rounds_argument_is_rejected(tmp_path):
    with pytest.raises(TypeError):
        sa.author_scene(_scene(), str(tmp_path), width=1080, height=1440,
                        duration=4.0, scene_index=1, total_scenes=3,
                        max_rounds=3)


# ══════════════════════════════════════════════════════════
#  Narrow toolset
# ══════════════════════════════════════════════════════════

def test_toolset_is_narrow_and_has_no_render_path():
    names = {t['function']['name'] for t in sa.SCENE_AUTHOR_TOOLS}
    # The AUTHORING core. Pinned as a subset rather than an exact set: the
    # toolset is allowed to grow a read-only knowledge channel (craft_reference
    # was added for epic pt_db5602172ac44b11 item ③) without this guard having
    # to be edited, but it may never LOSE one of these or gain a side-effecting
    # one — which is what the banned list below actually enforces.
    assert {'write_composition', 'composition_check',
            'web_search', 'generate_asset', 'fetch_url'} <= names
    # Everything in the set must be authoring, search, or read-only reference.
    assert names <= {'write_composition', 'composition_check', 'web_search',
                     'generate_asset', 'fetch_url', 'craft_reference'}
    # No render / concat / mux / arbitrary write_file reachable from a scene
    # author. `generate_asset` is deliberately IN the set: it writes only into
    # the content-addressed asset library and returns a scene-relative path,
    # which is the sanctioned way for a composition to carry real imagery —
    # unlike a general filesystem write, it cannot place a file anywhere else.
    for banned in ('motion_video_render', 'motion_video_concat',
                   'motion_video_mux', 'write_file', 'run_command'):
        assert banned not in names


def test_unknown_tool_is_rejected_not_crashing(monkeypatch, tmp_path):
    _fake_llm(monkeypatch, [
        ('', [('run_command', {'command': 'rm -rf /'})]),
        ('', [('write_composition', {'html': _good_html()})]),
        ('ok', []),
    ])
    res = sa.author_scene(_scene(), str(tmp_path), width=1080, height=1440,
                          duration=4.0, scene_index=1, total_scenes=3)
    assert res['mode'] == 'authored'  # the bogus call was answered, not fatal


# ══════════════════════════════════════════════════════════
#  Opt-in gating
# ══════════════════════════════════════════════════════════

def test_scene_author_on_by_default(monkeypatch):
    """Authoring is the DEFAULT deliverable (owner 2026-07-27).

    This assertion was inverted on purpose: it used to pin "off by default",
    which is the behaviour that made every film a deck of plain template
    cards. The template is the fallback, not the default — see
    tests/test_motion_video_visual_quality.py for the entry-point coverage
    that proves EVERY spawn site inherits this.
    """
    monkeypatch.delenv('TOFU_MOTION_SCENE_AUTHOR', raising=False)
    assert sa.scene_author_enabled({}) is True
    assert sa.scene_author_enabled(None) is True


def test_scene_author_env_kill_switch_forces_it_off(monkeypatch):
    """Cost control: one agent loop per scene must stay switchable off."""
    monkeypatch.setenv('TOFU_MOTION_SCENE_AUTHOR', '0')
    assert sa.scene_author_enabled({}) is False
    # An explicit per-job choice still outranks the fleet default.
    assert sa.scene_author_enabled({'scene_author': True}) is True


def test_scene_author_per_job_choice_wins_over_the_env(monkeypatch):
    monkeypatch.delenv('TOFU_MOTION_SCENE_AUTHOR', raising=False)
    assert sa.scene_author_enabled({'scene_author': True}) is True
    # Per-job False wins even when the env switches it on globally.
    monkeypatch.setenv('TOFU_MOTION_SCENE_AUTHOR', '1')
    assert sa.scene_author_enabled({}) is True
    assert sa.scene_author_enabled({'scene_author': False}) is False


# ══════════════════════════════════════════════════════════
#  Engine compose-stage resume (no re-authoring)
# ══════════════════════════════════════════════════════════

def _authored_html(duration=4.0):
    """A composition that passes the gate WITHOUT the template's fallback mark.

    The resume tests below must not use :func:`_good_html`: that returns the
    zero-LLM TEMPLATE, and ``_existing_composition`` now deliberately refuses
    to adopt a fallback card (a scene degraded by a transient blip used to be
    pinned to the gradient forever, because the resume path compared only
    ``data-duration``). Using the template here would test the opposite of the
    intended behaviour — and, in the duration-mismatch case, would pass for
    the wrong reason.
    """
    return f'''<!doctype html>
<html><head><meta charset="UTF-8">
<script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
<style>*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{width:1080px;height:1440px;overflow:hidden;background:#000}}
#root{{position:relative;width:1080px;height:1440px;overflow:hidden}}
.bgfill{{position:absolute;inset:0;background:linear-gradient(160deg,#0b0f14,#1d3a5f)}}
.clip{{position:absolute;inset:0;display:grid;place-items:center}}
.headline{{font-size:96px;font-weight:800;color:#fff;max-width:900px}}</style></head><body>
<div id="root" data-composition-id="main" data-start="0" data-duration="{duration}"
     data-width="1080" data-height="1440">
<div class="bgfill"></div>
<section id="c1" class="clip" data-start="0" data-duration="{duration}" data-track-index="1">
<h1 class="headline" id="hl">Authored scene</h1></section></div>
<script>window.__timelines=window.__timelines||{{}};
const tl=gsap.timeline({{paused:true}});
tl.from('#hl',{{opacity:0,y:56,duration:.7}},0.2);
window.__timelines['main']=tl;</script></body></html>'''


def test_existing_composition_reused_when_duration_matches(tmp_path):
    html = _authored_html(duration=4.0)
    p = tmp_path / 'index.html'
    p.write_text(html, encoding='utf-8')
    assert eng._existing_composition(str(p), 4.0) == html


def test_existing_composition_discarded_when_duration_changed(tmp_path):
    p = tmp_path / 'index.html'
    p.write_text(_authored_html(duration=4.0), encoding='utf-8')
    assert eng._existing_composition(str(p), 7.5) is None


def test_existing_composition_refuses_a_degraded_fallback_card(tmp_path):
    """A template card on disk must be RE-AUTHORED, not adopted.

    Pins the fix for the permanent lock-in: pre-fix, one transient network
    fault wrote the gradient card to index.html and every later resume/regen
    adopted it, so re-running the job could never retry that scene's
    authoring. Full coverage (incl. NEUTER) lives in
    tests/test_motion_video_author_resilience.py.
    """
    p = tmp_path / 'index.html'
    p.write_text(_good_html(duration=4.0), encoding='utf-8')   # the template
    assert eng._existing_composition(str(p), 4.0) is None


def test_existing_composition_absent_file(tmp_path):
    assert eng._existing_composition(str(tmp_path / 'nope.html'), 4.0) is None
