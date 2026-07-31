"""tests/test_motion_video_gate_categories.py — the gate-category partition.

WHY (measured 2026-07-29, owner-found). ``_classify_failure`` gained an
``'infra'`` category on 2026-07-28 so that cgroup-memory noise would stop being
charged to the composition author. It was added to the CLASSIFIER only. Both
consumers — ``engine._scene_gate_findings`` and ``_scene_author._full_gate`` —
carried their own hand-written ``('env_missing', 'aborted', 'timeout',
'chrome')``, so the brand-new category was exempted NOWHERE.

Consequences, measured rather than reasoned:

  * 14 of 14 scene dirs on this host classify ``'infra'`` — every single scene
    was being judged by a category nobody handled;
  * two scenes of a 6-scene film held 7,481 B and 10,284 B of FINISHED authored
    work in ``.tofu-draft/`` (one with 2 inline SVGs + an ``@font-face``) while
    ``index.html`` carried a 2-node gradient card;
  * the film's own telemetry showed those scenes burned 54,125 and 60,841
    tokens — proof the author had worked, not that it had failed.

So these guards pin the INVARIANT, not a list of strings: every category the
classifier can return must be classified, and the two consumers must ask the
shared predicate rather than restating a tuple. A guard that hardcoded
``'infra' in EXEMPT`` would go green today and miss the NEXT category exactly
the same way.
"""

from __future__ import annotations

import ast
import inspect
import os
import re

import pytest

pytestmark = pytest.mark.unit

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel: str) -> str:
    with open(os.path.join(_ROOT, rel), encoding='utf-8') as f:
        return f.read()


# ══════════════════════════════════════════════════════════
# The partition itself
# ══════════════════════════════════════════════════════════

def test_infra_and_composition_sets_are_disjoint():
    from lib.motion_video._render import (COMPOSITION_CATEGORIES,
                                          INFRA_CATEGORIES)

    overlap = INFRA_CATEGORIES & COMPOSITION_CATEGORIES
    assert not overlap, (
        f'a category cannot be both infrastructure and a composition '
        f'verdict: {sorted(overlap)}')


def test_all_categories_is_exactly_the_union():
    from lib.motion_video._render import (ALL_CATEGORIES,
                                          COMPOSITION_CATEGORIES,
                                          INFRA_CATEGORIES)

    assert ALL_CATEGORIES == INFRA_CATEGORIES | COMPOSITION_CATEGORIES


def test_every_category_the_classifier_can_return_is_classified():
    """THE guard. Any string literal returned by the classification helpers
    must appear in the partition.

    This is what makes the next ``'infra'`` impossible: adding a category to
    ``_classify_failure`` without deciding whether it is infrastructure or a
    composition verdict turns this test red at the moment it is written.
    """
    from lib.motion_video import _render

    tree = ast.parse(_src('lib/motion_video/_render.py'))
    returned: set[str] = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        if fn.name not in ('_classify_failure',):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Return) and isinstance(node.value,
                                                           ast.Constant):
                if isinstance(node.value.value, str) and node.value.value:
                    returned.add(node.value.value)

    assert returned, 'found no category literals — the AST scan is broken'
    unclassified = returned - _render.ALL_CATEGORIES
    assert not unclassified, (
        f'category/ies {sorted(unclassified)} can be returned by '
        f'_classify_failure but appear in neither INFRA_CATEGORIES nor '
        f'COMPOSITION_CATEGORIES. Classify them — an unclassified category is '
        f'one some consumer will silently mishandle (this is exactly how '
        f"'infra' discarded finished work on 14/14 scenes).")


def test_categories_assigned_in_dict_literals_are_also_classified():
    """``_run_cli`` / ``_env_missing_result`` set categories directly."""
    from lib.motion_video import _render

    src = _src('lib/motion_video/_render.py')
    literals = set(re.findall(r"'category':\s*'([a-z_]+)'", src))
    unclassified = literals - _render.ALL_CATEGORIES
    assert not unclassified, (
        f'{sorted(unclassified)} assigned as a category but not classified')


def test_infra_includes_the_ones_that_say_nothing_about_the_composition():
    """Behavioural floor: these are all "the environment stopped us"."""
    from lib.motion_video._render import is_infra_category

    for cat in ('env_missing', 'aborted', 'timeout', 'chrome', 'infra', 'io'):
        assert is_infra_category(cat), (
            f'{cat!r} describes the environment, not the frame — charging it '
            f'to the author discards work for a reason the author cannot fix')


def test_real_composition_verdicts_are_never_exempt():
    from lib.motion_video._render import is_infra_category

    assert not is_infra_category('lint'), (
        'a lint verdict names a real defect — exempting it would forgive the '
        'very failures the gate exists to catch')
    assert not is_infra_category('unknown'), (
        'an UNEXPLAINED failure must stay a composition verdict; treating it '
        'as safe-to-ignore silently forgives real defects')
    assert not is_infra_category('')
    assert not is_infra_category(None)


# ══════════════════════════════════════════════════════════
# Both consumers must USE the predicate
# ══════════════════════════════════════════════════════════

@pytest.mark.parametrize('rel,func', [
    ('lib/motion_video/engine.py', '_scene_gate_findings'),
    ('lib/motion_video/_scene_author.py', '_full_gate'),
])
def test_consumers_ask_the_predicate_not_a_hand_copied_tuple(rel, func):
    """The root cause was TWO hand-written copies of the exempt set.

    Comments are stripped first (charter #24 / tests/_source_scan.py): a
    docstring that MENTIONS the old tuple must not fail this guard, and must
    not satisfy it either.
    """
    from tests._source_scan import strip_comments

    src = _src(rel)
    tree = ast.parse(src)
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func:
            target = node
            break
    assert target is not None, f'{func} not found in {rel}'

    body = ast.get_source_segment(src, target) or ''
    # Drop the docstring: prose about the old shape is documentation, not code.
    doc = ast.get_docstring(target) or ''
    if doc:
        body = body.replace(doc, '')
    live = strip_comments(body, lang='python')

    assert 'is_infra_category' in live, (
        f'{rel}:{func} must ask is_infra_category() — the shared predicate is '
        f'the single owner of the taxonomy')
    assert "'env_missing', 'aborted'" not in live, (
        f'{rel}:{func} still carries a hand-copied exempt tuple; that is the '
        f'defect — two copies always drift, and the drift silently discarded '
        f'finished work')


def test_infra_verdict_does_not_reach_the_author_as_a_finding(monkeypatch,
                                                              tmp_path):
    """Behavioural end-to-end: an 'infra' outcome must yield NO findings.

    The pure-source guards above prove the shape; this proves the behaviour,
    so a refactor that keeps the call but inverts the condition still fails.
    """
    from lib.motion_video import _render, _scene_author

    html = ('<!doctype html><html><body><div id="root" '
            'data-composition-id="main" data-start="0" data-duration="5" '
            'data-width="1080" data-height="1440">'
            '<div style="position:absolute;inset:0"></div>'
            '<h1 id="t">x</h1></div>'
            '<script>window.__timelines={};'
            'const tl=gsap.timeline({paused:true});'
            "window.__timelines['main']=tl;</script></body></html>")

    monkeypatch.setattr(_render, 'check_project', lambda *a, **k: {
        'ok': False, 'category': 'infra',
        'errors': ['check failed (exit 1) without a machine-readable finding: '
                   '[SystemMemory] cgroup memory limit detected'],
        'fix_hints': []})
    monkeypatch.setattr(
        'lib.motion_video._fill.check_composition_fill', lambda *a, **k: [])

    findings = _scene_author._full_gate(html, str(tmp_path),
                                        scene={'id': 'scene-001'})
    assert findings == [], (
        'an infrastructure outcome must not be charged to the composition — '
        f'got {findings}')


def test_a_real_lint_verdict_still_reaches_the_author(monkeypatch, tmp_path):
    """The complement. Exempting infra must not exempt everything."""
    from lib.motion_video import _render, _scene_author

    html = ('<!doctype html><html><body><div id="root" '
            'data-composition-id="main" data-start="0" data-duration="5" '
            'data-width="1080" data-height="1440">'
            '<div style="position:absolute;inset:0"></div>'
            '<h1 id="t">x</h1></div>'
            '<script>window.__timelines={};'
            'const tl=gsap.timeline({paused:true});'
            "window.__timelines['main']=tl;</script></body></html>")

    monkeypatch.setattr(_render, 'check_project', lambda *a, **k: {
        'ok': False, 'category': 'lint',
        'errors': ['text overflows its container at t=2.0s'],
        'fix_hints': ['reduce the font size or widen the container']})
    monkeypatch.setattr(
        'lib.motion_video._fill.check_composition_fill', lambda *a, **k: [])

    findings = _scene_author._full_gate(html, str(tmp_path),
                                        scene={'id': 'scene-001'})
    assert any('overflows' in f for f in findings), (
        'a named composition defect must still reach the repair loop')


# ══════════════════════════════════════════════════════════
# Zero-spend draft rescue
# ══════════════════════════════════════════════════════════

_GOOD_DRAFT = (
    '<!doctype html><html><body><div id="root" data-composition-id="main" '
    'data-start="0" data-duration="10.238" data-width="1080" '
    'data-height="1440"><div style="position:absolute;inset:0"></div>'
    '<svg><rect width="10" height="10"/></svg><h1 id="t">已完成的稿</h1></div>'
    '<script>window.__timelines={};const tl=gsap.timeline({paused:true});'
    "window.__timelines['main']=tl;</script></body></html>")


def test_gate_passing_draft_is_adopted_without_spending_a_round(monkeypatch,
                                                                tmp_path):
    """A draft only survives because the last run did not adopt it.

    Measured: the most common reason was an infrastructure verdict discarding
    finished work. Re-entering the agent loop to re-derive a composition we
    are already holding is pure waste, so a draft that passes NOW is adopted
    for 0 rounds / 0 tokens.
    """
    from lib.motion_video import _scene_author

    _scene_author.save_draft(str(tmp_path), _GOOD_DRAFT)
    monkeypatch.setattr(_scene_author, '_full_gate', lambda *a, **k: [])

    def _boom(*a, **k):
        raise AssertionError('the agent loop must NOT run for a passing draft')

    monkeypatch.setattr(_scene_author, '_author_once', _boom)

    res = _scene_author.author_scene(
        {'id': 'scene-001', 'text': 'x'}, str(tmp_path), width=1080,
        height=1440, duration=10.238, scene_index=1, total_scenes=6)
    assert res['mode'] == 'authored'
    assert res['tokens'] == 0
    assert res['rounds'] == 0
    assert res['html'] == _GOOD_DRAFT
    assert not os.path.isfile(
        os.path.join(str(tmp_path), _scene_author.DRAFT_FILENAME)), \
        'an adopted draft must be cleared'


def test_adoption_asks_for_the_advisory_verdict_not_the_plain_one():
    """Adoption must clear the STRICTER bar.

    Measured 2026-07-29: the first rescue used the plain verdict, which omits
    the asset floor — so it adopted a graphics-less draft for 0 tokens and
    shipped a text-only frame the floor had just been written to reject.
    Adoption is the one place a composition reaches the film WITHOUT the
    author ever seeing feedback, so the looser bar is the wrong one.
    """
    import inspect

    from tests._source_scan import strip_comments
    from lib.motion_video import _scene_author

    src = inspect.getsource(_scene_author.author_scene)
    doc = inspect.getdoc(_scene_author.author_scene) or ''
    if doc:
        src = src.replace(doc, '')
    live = strip_comments(src, lang='python')
    idx = live.find('_full_gate(resumed')
    assert idx != -1, 'the adoption pre-check call was not found'
    window = live[idx:idx + 220]
    assert 'advisory=True' in window, (
        'the adoption pre-check must run the ADVISORY gate — the plain '
        'verdict omits the asset floor, which is how a graphics-less draft '
        'got adopted for 0 tokens')


def test_graphics_less_draft_is_not_adopted_and_seeds_the_repair(monkeypatch,
                                                                 tmp_path):
    """Behavioural complement: a bare draft must reach the repair loop.

    This drives the REAL gate (only the browser/CLI layers are stubbed), so a
    refactor that keeps `advisory=True` but stops feeding the floor into it
    still fails here.
    """
    from lib.motion_video import _render, _scene_author

    bare = _GOOD_DRAFT.replace('<svg><rect width="10" height="10"/></svg>', '')
    assert '<svg' not in bare
    _scene_author.save_draft(str(tmp_path), bare)

    # Neutralise the infrastructure-dependent layers; the floor is pure Python.
    monkeypatch.setattr(_render, 'check_project',
                        lambda *a, **k: {'ok': True, 'category': '',
                                         'errors': [], 'fix_hints': []})
    monkeypatch.setattr(
        'lib.motion_video._fill.check_composition_fill', lambda *a, **k: [])

    seen = {}

    def _fake_once(scene, scene_dir, **kw):
        seen['seed'] = kw.get('seed_html')
        return {'outcome': 'quality', 'html': '', 'rounds': 1, 'tokens': 7,
                'detail': 'stub'}

    monkeypatch.setattr(_scene_author, '_author_once', _fake_once)
    res = _scene_author.author_scene(
        {'id': 'scene-001', 'text': 'x'}, str(tmp_path), width=1080,
        height=1440, duration=10.238, scene_index=1, total_scenes=6)

    assert seen.get('seed') == bare, (
        'a graphics-less draft must NOT be adopted — it must seed the repair '
        'loop so the author gets a chance to add the imagery')
    assert res['mode'] == 'template'


def test_advisory_gate_carries_the_asset_floor(monkeypatch, tmp_path):
    """The floor must live in the author's own gate, not only in the engine.

    An engine-only floor is a gate every future author path has to remember to
    re-attach — and the very next path (draft adoption) forgot it.
    """
    from lib.motion_video import _render, _scene_author

    bare = _GOOD_DRAFT.replace('<svg><rect width="10" height="10"/></svg>', '')
    monkeypatch.setattr(_render, 'check_project',
                        lambda *a, **k: {'ok': True, 'category': '',
                                         'errors': [], 'fix_hints': []})
    monkeypatch.setattr(
        'lib.motion_video._fill.check_composition_fill', lambda *a, **k: [])

    advisory = _scene_author._full_gate(bare, str(tmp_path),
                                        scene={'id': 'scene-001'},
                                        advisory=True)
    assert any('no real graphic' in f.lower() for f in advisory), (
        'advisory=True must report the asset floor')

    plain = _scene_author._full_gate(bare, str(tmp_path),
                                     scene={'id': 'scene-001'})
    assert not any('no real graphic' in f.lower() for f in plain), (
        'the ACCEPT/REJECT verdict must NOT reject on the floor — that would '
        'degrade the scene to a template with zero graphics, making the very '
        'metric worse')


def test_failing_draft_still_enters_the_loop_as_a_seed(monkeypatch, tmp_path):
    """Adoption is for drafts that PASS. A failing one is still repair fodder."""
    from lib.motion_video import _scene_author

    _scene_author.save_draft(str(tmp_path), _GOOD_DRAFT)
    monkeypatch.setattr(_scene_author, '_full_gate',
                        lambda *a, **k: ['still broken'])
    seen = {}

    def _fake_once(scene, scene_dir, **kw):
        seen['seed'] = kw.get('seed_html')
        return {'outcome': 'quality', 'html': '', 'rounds': 1, 'tokens': 10,
                'detail': 'stub'}

    monkeypatch.setattr(_scene_author, '_author_once', _fake_once)
    res = _scene_author.author_scene(
        {'id': 'scene-001', 'text': 'x'}, str(tmp_path), width=1080,
        height=1440, duration=10.238, scene_index=1, total_scenes=6)
    assert seen['seed'] == _GOOD_DRAFT, 'a failing draft must seed the repair'
    assert res['mode'] == 'template'


def test_stale_duration_draft_is_never_adopted(monkeypatch, tmp_path):
    """A draft from a run whose timeline changed would render at the wrong
    length — the duration check must gate adoption too."""
    from lib.motion_video import _scene_author

    _scene_author.save_draft(str(tmp_path), _GOOD_DRAFT)   # duration 10.238
    monkeypatch.setattr(_scene_author, '_full_gate', lambda *a, **k: [])
    calls = {'n': 0}

    def _fake_once(scene, scene_dir, **kw):
        calls['n'] += 1
        assert not kw.get('seed_html'), 'a stale draft must not seed either'
        return {'outcome': 'quality', 'html': '', 'rounds': 1, 'tokens': 5,
                'detail': 'stub'}

    monkeypatch.setattr(_scene_author, '_author_once', _fake_once)
    _scene_author.author_scene(
        {'id': 'scene-001', 'text': 'x'}, str(tmp_path), width=1080,
        height=1440, duration=7.5, scene_index=1, total_scenes=6)
    assert calls['n'] == 1, 'a stale draft must fall through to the loop'



# ══════════════════════════════════════════════════════════
# The authored counter must not read a resumed film as "all fell back"
# ══════════════════════════════════════════════════════════

def test_pre_marker_fallback_card_is_still_recognised_as_a_template():
    """The marker was added 2026-07-29; every fallback card before it is
    marker-LESS — including scene-004 of the film that started this effort.

    A marker-only test cannot see the exact population the marker was
    introduced to rescue: that 2,398-byte gradient card was adopted as a
    finished authored composition on every re-run, pinning the scene forever.
    """
    from lib.motion_video._template import (is_template_composition,
                                            matches_template,
                                            render_scene_html)

    scene = {'id': 'scene-004', 'start': 30.476, 'end': 40.714,
             'text': '旁白', 'on_screen': 'LCS对齐:零成本自动生成编辑监督',
             'visual': '两行文字上下对齐连线'}
    card = render_scene_html(scene, duration=10.238, scene_index=4,
                             total_scenes=6)
    # Simulate a pre-marker card by stripping the marker the template stamps.
    from lib.motion_video._template import TEMPLATE_MARKER
    legacy = card.replace(TEMPLATE_MARKER, '')
    assert not is_template_composition(legacy), \
        'precondition: the legacy card carries no marker'
    assert matches_template(legacy, scene, duration=10.238, scene_index=4,
                            total_scenes=6), (
        'a marker-less fallback card must still be recognised, or the resume '
        'path adopts it as authored and pins the scene to the gradient')


def test_matches_template_does_not_flag_a_genuine_composition():
    """The complement: over-eager detection would re-author good scenes."""
    from lib.motion_video._template import matches_template

    scene = {'id': 'scene-005', 'start': 0.0, 'end': 5.0, 'text': 'x',
             'on_screen': '标题'}
    authored = (
        '<!doctype html><html><body><div id="root" data-composition-id="main" '
        'data-start="0" data-duration="5" data-width="1080" '
        'data-height="1440"><div style="position:absolute;inset:0"></div>'
        '<svg><rect width="10" height="10"/></svg>'
        '<h1 id="t">标题</h1></div><script>window.__timelines={};'
        'const tl=gsap.timeline({paused:true});'
        "window.__timelines['main']=tl;</script></body></html>")
    assert not matches_template(authored, scene, duration=5.0,
                                scene_index=5, total_scenes=6)


def test_resumed_authored_composition_counts_as_authored():
    """Measured: a rescue run with all six compositions intact printed
    authored 0/6 while shipping six authored frames, because the counter only
    incremented on the fresh-authoring path. A fully-resumed film then read
    as "every scene fell back" in both the verdict and the panel.
    """
    import inspect

    from lib.motion_video import engine
    from tests._source_scan import python_block

    src = inspect.getsource(engine.run_motion_task)
    # The resume branch must inspect whether the reused composition is the
    # fallback card before it can count it.
    #
    # Indent-matched, not a fixed 600-byte window (charter #24 / pt_b95c6d39).
    # ``run_motion_task`` is ~16.7 KB, so the old window covered under 4% of it
    # and stopped well short of this branch's end. This is a POSITIVE assertion,
    # which is the dangerous polarity for a truncating window: it passes today
    # only because the token happens to sit early in the branch, and would go
    # quietly green if the check moved a few lines down.
    branch = python_block(src, 'if existing is not None:')
    assert 'is_template_composition' in branch, (
        'the resume branch must ask whether the reused composition is the '
        'fallback card before counting it as authored — an authored resume '
        'is authored work, a template resume is still a degrade')
