"""Guards for the motion-video visual-quality gates.

Why this file exists (all three were REAL defects found by measurement, not
review):

1. ``_gate`` read a FLAT top-level ``findings`` key, but ``hyperframes check``
   nests its findings under ``lint`` / ``runtime`` / ``layout``. Every finding
   of the merged report was silently dropped, so a composition with a fatal
   runtime error came back ``ok=False`` with ``errors == []`` — which reads as
   "failed for no reason" to every consumer, and as SUCCESS to any consumer
   that only looks at the error list.
2. The zero-LLM template named ``PingFang SC`` / ``Noto Sans CJK SC``, neither
   of which is in the renderer's auto-resolved font list. Naming an absent face
   does not get you that face; it gets a silent fallback to whatever fontconfig
   has. On a host whose only CJK face is a SERIF, the sans stack rendered serif.
3. The engine ran only the regex gate, which is structurally blind to fonts,
   contrast, runtime errors and text overflow — i.e. to everything a viewer
   describes as "no formatting".

Discipline (charter): these assert RESULTS (what the pipeline reports for a
given composition), never constants, so re-tuning the CLI invocation or the
template's font stack cannot make them falsely green.
"""

from __future__ import annotations

import json
import os
import re
import sys

import pytest

from tests._runtime_sections import runtime_section_path
from tests.frontend.test_frontend_paper_video_artifact import (
    test_video_reader_uses_program_clock_and_preserves_playback as _assert_video_reader,
)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from lib.motion_video._render import _collect_findings, _gate  # noqa: E402
from lib.motion_video._template import render_scene_html  # noqa: E402

pytestmark = pytest.mark.unit


# ── 1. The nested-report parser ───────────────────────────

def test_findings_are_collected_from_the_nested_check_report():
    """A ``check`` payload's per-section findings must all be collected.

    This is the exact shape the installed CLI emits; reading only the flat
    ``findings`` key returned an empty list while ``ok`` was false.
    """
    payload = {
        'ok': False,
        'lint': {'ok': True, 'findings': []},
        'runtime': {'ok': False, 'findings': [
            {'code': 'page_error', 'severity': 'error',
             'message': 'NOT_DEFINED_AT_ALL is not defined'}]},
        'layout': {'ok': False, 'findings': [
            {'severity': 'error', 'message': 'text overflows its container'}]},
    }
    findings, saw = _collect_findings(payload)
    assert saw is True
    messages = [f['message'] for f in findings]
    assert 'NOT_DEFINED_AT_ALL is not defined' in messages
    assert 'text overflows its container' in messages


def test_flat_report_shape_still_parses():
    """The legacy single-subcommand shape must keep working."""
    findings, saw = _collect_findings(
        {'findings': [{'severity': 'error', 'message': 'flat style'}]})
    assert saw is True
    assert [f['message'] for f in findings] == ['flat style']


def test_unrecognisable_payload_reports_that_it_saw_no_report():
    findings, saw = _collect_findings({'ok': False})
    assert findings == []
    assert saw is False


# ── 2. A failure never reports an EMPTY reason list ───────

def _fake_run(monkeypatch, *, rc, out, err=''):
    monkeypatch.setattr('lib.motion_video._render._cli_or_env_error',
                        lambda: '/fake/hyperframes')
    monkeypatch.setattr(
        'lib.motion_video._render._run_cli',
        lambda *a, **k: {'rc': rc, 'out': out, 'err': err,
                         'elapsed': 0.1, 'category': ''})


def test_a_failing_gate_always_names_at_least_one_reason(monkeypatch):
    """``ok=False`` with ``errors=[]`` is the shape that read as success.

    The CLI can exit non-zero while its report names no error-severity
    finding. The gate must synthesize one so no consumer sees a failure with
    an empty reason list.
    """
    _fake_run(monkeypatch, rc=1, out=json.dumps({'ok': False}),
              err='boom: the renderer exploded')
    res = _gate('check', '/tmp/nowhere', timeout=5)
    assert res['ok'] is False
    assert res['errors'], 'a failure must never carry an empty error list'
    assert 'boom: the renderer exploded' in res['errors'][0]


def test_a_failing_gate_prefers_the_real_findings_over_the_synthetic_one(monkeypatch):
    """The synthetic reason must not mask a real, specific finding."""
    _fake_run(monkeypatch, rc=1, out=json.dumps({
        'ok': False,
        'runtime': {'findings': [{'severity': 'error',
                                  'message': 'foo is not defined'}]}}))
    res = _gate('check', '/tmp/nowhere', timeout=5)
    assert res['errors'] == ['foo is not defined']


def test_a_clean_report_stays_green(monkeypatch):
    """The complement: a passing gate must not manufacture errors."""
    _fake_run(monkeypatch, rc=0, out=json.dumps({
        'ok': True, 'lint': {'findings': []}, 'runtime': {'findings': []},
        'layout': {'findings': []}}))
    res = _gate('check', '/tmp/nowhere', timeout=5)
    assert res['ok'] is True
    assert res['errors'] == []


# ── 3. The template names only resolvable fonts ───────────

#: Families the installed renderer resolves on its own. Anything else must be
#: accompanied by an @font-face rule or it silently degrades.
_AUTO_RESOLVED = {'inter', 'roboto', 'open sans', 'lato', 'montserrat',
                  'poppins', 'source sans 3', 'noto sans', 'noto sans jp',
                  'system-ui', 'sans-serif', 'serif', 'monospace',
                  'ui-monospace'}


def test_template_never_names_a_font_the_renderer_cannot_resolve():
    """Naming an absent face is worse than not naming one.

    It does not produce that face — it produces a silent fallback to whatever
    fontconfig offers (a SERIF, on the verified host) while looking deliberate
    in the source. Asserting on the RENDERED html (not on a constant) means a
    future edit that reintroduces such a family fails here.
    """
    html = render_scene_html(
        {'id': 'scene-001', 'start': 0.0, 'end': 4.0,
         'on_screen': '固态电池的能量密度', 'visual': 'x'},
        duration=4.0, scene_index=1, total_scenes=2)
    assert '@font-face' not in html, (
        'this guard assumes the template declares no faces of its own; '
        'if that changes, the named families below become legitimate')

    import re
    families: set[str] = set()
    for decl in re.findall(r'font-family:\s*([^;}]+)', html):
        for part in decl.split(','):
            families.add(part.strip().strip('"\'').lower())
    unresolvable = {f for f in families if f and f not in _AUTO_RESOLVED}
    assert not unresolvable, (
        f'template names font families the renderer cannot resolve: '
        f'{sorted(unresolvable)} — they degrade silently to a fontconfig '
        f'fallback. Either drop them or ship an @font-face.')


# ── 4. The engine actually CONSULTS the real gates ────────

def test_engine_records_gate_findings_on_the_quality_axis(tmp_path, monkeypatch):
    """Real scene gates propagate defects to the persisted quality axis."""
    import json
    from pathlib import Path
    from lib import motion_video as mv
    from lib.motion_video import engine
    from tests.motion_video.test_motion_video_workflow import _film

    task, _ = _film(tmp_path, monkeypatch)
    finding = 'fixture text overflows its container'
    monkeypatch.setattr(mv, 'check_project', lambda *args, **kwargs: {
        'ok': False, 'category': 'lint', 'errors': [finding]})
    engine.run_motion_task(task)
    assert task['status'] == 'done', task.get('error')
    gates = [event for event in task['events'] if event['type'] == 'scene_gate']
    assert gates and all(finding in event['findings'] for event in gates)
    assert task['artifact_quality']['degraded'] is True
    assert all(event['scene_id'] in task['artifact_quality']['reason'] for event in gates)
    manifest = json.loads((Path(task['workdir']) / 'job.json').read_text())
    assert manifest['artifact_quality'] == task['artifact_quality']


def test_a_film_with_no_defects_is_not_marked_degraded():
    """Complement: the axis must not be a constant True.

    Without this, "always degraded" satisfies every assertion above while
    making the banner meaningless — users would learn to ignore it.
    """
    from lib.motion_video import engine

    verdict = engine._quality_verdict(
        degraded_narration=False, scene_gate_issues={},
        authoring=True, authored=8, total=8)
    assert verdict['degraded'] is False
    assert verdict['reason'] == ''


def test_scene_gate_helper_treats_a_missing_toolchain_as_not_a_defect():
    """No CLI installed is an ENV problem, not an ugly frame.

    Without this, every scene on a host with no hyperframes CLI would be
    marked degraded and the film would look broken for the wrong reason.
    """
    from lib.motion_video.engine import _scene_gate_findings

    class _MV:
        @staticmethod
        def is_infra_category(category):
            from lib.motion_video import is_infra_category
            return is_infra_category(category)

        @staticmethod
        def check_project(_d, **_k):
            return {'ok': False, 'category': 'env_missing', 'errors': []}

    assert _scene_gate_findings(_MV(), '/tmp/x', 'scene-001') == []
@pytest.mark.parametrize('category',
                         ['env_missing', 'aborted', 'timeout', 'chrome'])
def test_infrastructure_failures_are_not_charged_to_the_composition(category):
    """A browser crash is not an ugly frame — measured, not hypothesized.

    In the first real authored run, scene-002's composition was rejected for
    'check failed (exit 1) ... [SystemMemory...]' and degraded to the plain
    template; re-checking the SAME file afterwards passed clean. Chrome had hit
    memory pressure on that attempt. Charging that to the author both loses a
    good scene and, once every scene hits it, would report the whole film
    degraded for a reason the composition cannot fix.
    """
    from lib.motion_video.engine import _scene_gate_findings

    class _MV:
        @staticmethod
        def is_infra_category(category):
            from lib.motion_video import is_infra_category
            return is_infra_category(category)

        @staticmethod
        def check_project(_d, **_k):
            return {'ok': False, 'category': category,
                    'errors': ['check failed (exit 1): [SystemMemory] oom']}

    assert _scene_gate_findings(_MV(), '/tmp/x', 'scene-001') == [], (
        f'{category!r} is an infrastructure outcome; blaming the composition '
        f'for it degrades scenes that are actually fine')


def test_scene_gate_helper_surfaces_real_findings():
    """The complement — a genuine defect must come back."""
    from lib.motion_video.engine import _scene_gate_findings

    class _MV:
        @staticmethod
        def is_infra_category(category):
            from lib.motion_video import is_infra_category
            return is_infra_category(category)

        @staticmethod
        def check_project(_d, **_k):
            return {'ok': False, 'category': 'unknown',
                    'errors': ['text overflows its container']}

    assert _scene_gate_findings(_MV(), '/tmp/x', 'scene-001') == [
        'text overflows its container']


def test_scene_gate_helper_never_raises():
    """A gate crash must not take down a job."""
    from lib.motion_video.engine import _scene_gate_findings

    class _MV:
        @staticmethod
        def is_infra_category(category):
            from lib.motion_video import is_infra_category
            return is_infra_category(category)

        @staticmethod
        def check_project(_d, **_k):
            raise RuntimeError('chrome died')

    assert _scene_gate_findings(_MV(), '/tmp/x', 'scene-001') == []


# ── 5. The author is briefed with real craft, not just rules ──

def test_author_prompt_carries_the_vendored_craft_guide():
    """The craft knowledge must be IN-TREE, not an optional user install.

    A default-path quality feature that depends on a user having installed a
    skill package degrades silently to today's output when they have not.
    """
    from lib.motion_video._scene_author import _build_prompt

    prompt = _build_prompt(
        {'id': 'scene-001', 'text': '固态电池能量密度翻倍', 'visual': 'stat card'},
        width=1080, height=1440, duration=4.0, scene_index=1, total_scenes=3)
    # Archetype vocabulary + hierarchy + stagger are what separate an authored
    # scene from the one-centred-line fallback.
    for token in ('archetype', 'hierarchy', 'stagger'):
        assert token in prompt.lower(), f'craft guide missing {token!r}'
    # The host font constraint must be stated, or the author reintroduces the
    # silent serif fallback this batch just removed.
    assert 'auto-resolve' in prompt.lower()
    # The legitimate asset route must be stated. This used to assert INLINE
    # (SVG) because inline markup was the ONLY legal form an asset could
    # take — the author had no filesystem-write tool, so a bitmap or a real
    # background was structurally impossible. That restriction is what made
    # every frame "a gradient plus text", so the assertion now pins the route
    # that replaced it: generate_asset returns a scene-relative path, and a
    # reference that escapes the scene directory is rejected before rendering.
    low = prompt.lower()
    assert 'generate_asset' in prompt, (
        'the author is never told it can create real imagery, so every frame '
        'stays a gradient plus text')
    assert 'assets/' in prompt, 'no example of the scene-relative asset path'
    assert '../' in prompt or 'absolute path' in low, (
        'the author is not warned which references the gate rejects')


def test_all_scenes_falling_back_is_reported_as_degraded():
    """One scene degrading is by design; every scene degrading is a failure.

    When authoring was requested and nothing was authored, the user receives
    exactly the plain card deck that prompted this work — it must not settle
    on the quality axis as a clean success.
    """
    import inspect

    from lib.motion_video import engine

    verdict = engine._quality_verdict(
        degraded_narration=False, scene_gate_issues={},
        authoring=True, authored=0, total=8)
    assert verdict['degraded'] is True, (
        'a film whose scenes ALL fell back to the template still reports a '
        'clean success on the quality axis')
    assert '8' in verdict['reason'] and 'template' in verdict['reason']
    # …and it must be keyed on "authoring was REQUESTED": a film that never
    # asked for bespoke compositions is not degraded by having none.
    not_asked = engine._quality_verdict(
        degraded_narration=False, scene_gate_issues={},
        authoring=False, authored=0, total=8)
    assert not_asked['degraded'] is False, (
        'an explicitly-template film must not be reported as degraded — that '
        'is the tier the user chose')


def test_a_partly_authored_film_is_not_marked_degraded_for_that():
    """Complement: the per-scene degrade must stay local.

    Without this, "always report degraded" would satisfy the test above and
    make the quality axis meaningless.
    """
    from lib.motion_video import engine

    for authored in (1, 4, 8):
        verdict = engine._quality_verdict(
            degraded_narration=False, scene_gate_issues={},
            authoring=True, authored=authored, total=8)
        assert verdict['degraded'] is False, (
            f'{authored}/8 authored must NOT trip the wholesale flag')


# ── 8. EVERY entry point gets the good default ────────────
#
# The measured defect this section exists for: flipping `produce_video`'s
# default to 'authored' fixed the TOOL path only. The two paths a human
# actually reaches — the paper Video-studio button and a bare
# `POST /api/v1/motion/videos` — never set `scene_author` at all, so they fell
# through to `scene_author_enabled`'s own default, which was still OFF. The
# quality flip was invisible to every real user.
#
# Real HTTP, paper and Chat entry configurations are exercised in
# test_motion_video_admission.py. The static guard below prevents copying the
# scene author's shared default into an individual caller.


def _source_python_files(root: str) -> list[str]:
    """Return Python files under the current construction-owner roots.

    ``rg --files`` keeps this scan fast on the FUSE worktree and, unlike
    ``git ls-files``, also works in the detached proof trees used by release
    gates.  The latter deliberately have no ``.git`` directory.
    """
    import subprocess

    discovered = subprocess.run(
        ['rg', '--files', 'lib', 'routes'], capture_output=True, text=True,
        cwd=root, timeout=120, check=True).stdout.split()
    normalized = [os.path.relpath(rel, root) if os.path.isabs(rel) else rel
                  for rel in discovered]
    return [rel for rel in normalized
            if rel.endswith('.py') and os.path.isfile(os.path.join(root, rel))]


def test_no_entry_point_hand_copies_the_default(monkeypatch):
    """The default must have exactly ONE home.

    A site that writes ``task['scene_author'] = True`` unconditionally would
    satisfy the test above while re-creating the copy that caused this bug —
    the next entry point would again be the one nobody remembered.
    """
    import ast
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    offenders = []
    for rel in _source_python_files(root):
        with open(os.path.join(root, rel), encoding='utf-8') as f:
            src = f.read()
        if "'scene_author'" not in src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError as e:
            print(f'[scan] unparseable {rel}: {e}')
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for tgt in node.targets:
                if (isinstance(tgt, ast.Subscript)
                        and isinstance(tgt.slice, ast.Constant)
                        and tgt.slice.value == 'scene_author'
                        and isinstance(node.value, ast.Constant)
                        and node.value.value is True):
                    offenders.append(f'{rel}:{node.lineno}')
    assert not offenders, (
        f'these sites hard-code the authored default instead of letting '
        f'scene_author_enabled own it: {offenders}')


def test_the_env_var_is_an_emergency_kill_switch_in_both_directions(monkeypatch):
    """Cost control must stay reachable fleet-wide without a code change.

    Authoring spends one agent loop per scene; if it has to be switched off in
    a hurry, the operator needs a lever that does not require editing every
    caller. Both directions are asserted so the variable cannot decay into a
    one-way switch that silently ignores '0'.
    """
    from lib.motion_video._scene_author import scene_author_enabled

    monkeypatch.setenv('TOFU_MOTION_SCENE_AUTHOR', '0')
    assert scene_author_enabled({'task_id': 't'}) is False
    # An explicit per-job choice still wins over the fleet default.
    assert scene_author_enabled({'task_id': 't', 'scene_author': True}) is True
    monkeypatch.setenv('TOFU_MOTION_SCENE_AUTHOR', '1')
    assert scene_author_enabled({'task_id': 't'}) is True


# ── 9. A degraded film must LOOK degraded in the panel ────


def test_paper_video_lookup_carries_the_quality_axis():
    """The re-attach path must not launder a degraded film into a clean one.

    ``/api/v1/paper/video/lookup`` is what the Video-studio panel calls on tab
    open — and after a restart it is the ONLY response the panel ever sees
    (runtime.poll 404s on a task it no longer holds). Omitting
    ``artifact_quality`` there means a film whose every scene fell back to the
    template renders identically to a good one.
    """
    import inspect

    from routes.paper_pkg import _podcast as paper_video_routes

    src = inspect.getsource(paper_video_routes.lookup_video_abstract)
    assert 'artifact_quality' in src, (
        'the paper video lookup drops the product-quality axis — a degraded '
        'film re-attaches looking like a clean success')
    disk = inspect.getsource(paper_video_routes._lookup_paper_video_on_disk)
    assert 'artifact_quality' in disk, (
        'the post-restart disk fallback drops the quality axis; that path has '
        'no later poll to correct it')


def test_the_panel_renders_a_degrade_notice_when_the_axis_says_degraded():
    _assert_video_reader()


def test_the_composition_choice_is_its_own_control_not_the_render_preset():
    """draft/standard/high is a RENDER preset — it must not imply looks.

    A user picking 'High (slower, finer)' and receiving the plain template
    card is a precise, credible false promise: the control claims to govern
    quality while the thing they object to is chosen elsewhere.
    """
    js = runtime_section_path('paper/video.js')
    control_src = open(js, encoding='utf-8').read()
    runtime = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        'frontend', 'src', 'features', 'paper', 'video-runtime.ts')
    runtime_src = open(runtime, encoding='utf-8').read()
    assert 'videoVisualSel' in control_src, (
        'the panel offers no composition control, so the render preset is '
        'still the only thing that looks like a quality choice')
    assert 'videoVisualSel' in runtime_src and 'scene_author' in runtime_src, (
        'the composition choice is never sent to the backend')
    assert "state.visual !== 'template'" in runtime_src


# ── 10. The frame must be RIGHT, not merely well-formed ───
#
# The measured defect: an authored scene rendered the eyebrow 「极极致耐用测试」
# while its beat said 「耐用性」. lint + validate + inspect ALL passed — they
# check fonts, contrast, runtime errors and overflow, i.e. whether a frame is
# WELL-FORMED. None of them has an opinion on whether it is RIGHT. That is the
# same class of failure this whole effort is about: shipping green while
# looking wrong to a human.


def _real_compositions() -> list[tuple[str, str, str, dict]]:
    """Every authored composition on disk paired with its own beat.

    Deliberately the REAL corpus, not synthetic fixtures: a fidelity gate is
    a false-positive problem, and only genuine authored output can show
    whether it fires on good writing. Returns (job, scene_id, html, scene);
    empty when no job has been run on this host, which callers must skip on
    rather than assert over nothing (charter: verify the scan surface).
    """
    import glob

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    jobs = os.path.join(root, 'data', 'motion_video', 'jobs')
    out: list[tuple[str, str, str, dict]] = []
    for html_path in sorted(glob.glob(
            os.path.join(jobs, '*', 'scenes', '*', 'index.html'))):
        scene_dir = os.path.dirname(html_path)
        scene_id = os.path.basename(scene_dir)
        job_dir = os.path.dirname(os.path.dirname(scene_dir))
        storyboard = os.path.join(job_dir, 'scenes.json')
        if not os.path.isfile(storyboard):
            continue
        try:
            with open(storyboard, encoding='utf-8') as f:
                scenes = json.load(f)
            with open(html_path, encoding='utf-8') as f:
                html = f.read()
        except (OSError, ValueError) as e:
            print(f'[corpus] skipped {html_path}: {e}')
            continue
        scene = next((s for s in scenes if str(s.get('id')) == scene_id), None)
        if scene:
            out.append((os.path.basename(job_dir), scene_id, html, scene))
    return out


def test_the_fidelity_gate_flags_the_measured_corruption():
    """The one real defect in the corpus must be caught.

    Uses the shipped 「极极致耐用测试」 frame rather than a synthetic string, so
    the guard is anchored to output the pipeline actually produced.
    """
    from lib.motion_video import check_text_fidelity

    corpus = _real_compositions()
    if not corpus:
        pytest.skip('no motion jobs on this host to measure against')
    hits = {f'{job}/{sid}': check_text_fidelity(html, scene)
            for job, sid, html, scene in corpus
            if check_text_fidelity(html, scene)}
    assert hits, (
        'the fidelity gate fires on nothing in the whole corpus — including '
        'the frame that shipped 极极致耐用测试 against a beat saying 耐用性')
    assert any('极极' in e for errs in hits.values() for e in errs), (
        f'the known corruption is no longer detected; gate fired on {hits}')


def test_the_fidelity_gate_does_not_punish_good_writing():
    """The false-positive bound — measured before the rule was tightened.

    This is the assertion that keeps the gate honest. A naive "any doubled CJK
    character" rule fires on **14** of the corpus's scenes, 13 of them ordinary
    reduplicated words (恰恰 / 源源 / 准准 / 证证 / 偷偷) that the beat itself
    contains. Degrading 13 good scenes to plain template cards would be worse
    than the bug being fixed, so the shipped rule requires the doubled pair to
    be ABSENT from the source. Anything above a couple of hits means the rule
    has drifted back toward flagging legitimate prose.
    """
    from lib.motion_video import check_text_fidelity

    corpus = _real_compositions()
    if not corpus:
        pytest.skip('no motion jobs on this host to measure against')
    flagged = [f'{job}/{sid}: {check_text_fidelity(html, scene)}'
               for job, sid, html, scene in corpus
               if check_text_fidelity(html, scene)]
    assert len(flagged) <= 2, (
        f'{len(flagged)}/{len(corpus)} real scenes tripped the fidelity gate '
        f'— at this rate it degrades good compositions, so the GATE is wrong, '
        f'not the compositions: {flagged}')


def test_a_reduplicated_word_the_beat_supports_is_not_a_corruption():
    """Legitimate Chinese reduplication must pass.

    The precise mechanism the false-positive bound relies on, asserted
    directly so a refactor cannot drop the source cross-check and stay green
    on a corpus that happens to be clean.
    """
    from lib.motion_video import check_text_fidelity

    html = ('<html><body><div data-composition-id="s1">'
            '<h1>源源不断的算力</h1></div></body></html>')
    supported = {'id': 'scene-001', 'text': '算力源源不断地涌入这个行业'}
    assert check_text_fidelity(html, supported) == [], (
        'a reduplicated word the narration itself uses was reported as '
        'corruption — this degrades correctly-written scenes')

    unsupported = {'id': 'scene-001', 'text': '算力涌入这个行业'}
    assert check_text_fidelity(html, unsupported), (
        'the complement failed: with no support in the beat, the doubled '
        'characters must be reported')


def test_script_and_style_bodies_are_not_judged_as_on_frame_text():
    """Only what a VIEWER reads counts.

    Without this, a CSS rule or a JS identifier can trip the gate — text that
    never reaches the frame, so the resulting degrade would be unexplainable
    to anyone looking at the video.
    """
    from lib.motion_video import visible_text

    html = ('<html><head><style>.aa{color:red}</style></head><body>'
            '<script>const 变变 = 1;</script><p>正常标题</p></body></html>')
    strings = visible_text(html)
    assert '正常标题' in strings
    assert not any('变变' in s for s in strings), (
        'script bodies are being read as on-frame text')


def test_fidelity_findings_reach_the_quality_verdict():
    """A finding that only logs is not a gate.

    Charter: testing the helper is not testing the wiring. This drives the
    engine's own collector so removing the fidelity call from
    ``_scene_gate_findings`` fails here, not just in review.
    """
    from lib.motion_video import engine

    class _MV:
        @staticmethod
        def check_text_fidelity(html, scene):
            from lib.motion_video import check_text_fidelity
            return check_text_fidelity(html, scene)

        @staticmethod
        def is_infra_category(category):
            from lib.motion_video import is_infra_category
            return is_infra_category(category)

        @staticmethod
        def check_project(_d, **_k):
            return {'ok': True, 'errors': []}

    html = ('<html><body><div data-composition-id="s1">'
            '<h1>极极致耐用测试</h1></div></body></html>')
    findings = engine._scene_gate_findings(
        _MV(), '/tmp/x', 'scene-003',
        scene={'id': 'scene-003', 'text': '这一代产品的耐用性大幅提升'},
        html=html)
    assert findings, (
        'a corrupted headline passes the engine gate untouched — the CLI '
        'gates cannot see it, so nothing would')
    verdict = engine._quality_verdict(
        degraded_narration=False,
        scene_gate_issues={'scene-003': findings},
        authoring=True, authored=8, total=8)
    assert verdict['degraded'] is True and 'scene-003' in verdict['reason']


def test_fidelity_survives_an_infrastructure_skip():
    """A corrupted headline is still corrupted when Chrome is unavailable.

    The CLI gates return empty on ``env_missing`` (correctly — that is an
    infra outcome). Fidelity is computed from HTML we already hold, so it must
    NOT be swallowed by the same exemption.
    """
    from lib.motion_video import engine

    class _MV:
        @staticmethod
        def check_text_fidelity(html, scene):
            from lib.motion_video import check_text_fidelity
            return check_text_fidelity(html, scene)

        @staticmethod
        def is_infra_category(category):
            from lib.motion_video import is_infra_category
            return is_infra_category(category)

        @staticmethod
        def check_project(_d, **_k):
            return {'ok': False, 'category': 'env_missing', 'errors': []}

    findings = engine._scene_gate_findings(
        _MV(), '/tmp/x', 'scene-003',
        scene={'id': 'scene-003', 'text': '这一代产品的耐用性大幅提升'},
        html=('<html><body><div data-composition-id="s1">'
              '<h1>极极致耐用测试</h1></div></body></html>'))
    assert findings, (
        'text fidelity was dropped along with the CLI gates on env_missing; '
        'it needs no toolchain and must still be reported')


def test_the_author_can_repair_a_fidelity_finding_before_the_film_ships():
    """The finding must ride the author's own feedback loop.

    Reported only at the end, a corrupted headline can no longer be fixed —
    the scene just degrades. ``_full_gate`` is what the author's
    ``composition_check`` tool calls, so the check must live there too.
    """
    from lib.motion_video._scene_author import _full_gate

    html = ('<html><body><div data-composition-id="s1" data-duration="4" '
            'data-width="1080" data-height="1440"><h1>极极致耐用测试</h1>'
            '</div></body></html>')
    errors = _full_gate(html, '/tmp/nowhere-scene',
                        scene={'id': 'scene-003',
                               'text': '这一代产品的耐用性大幅提升'})
    assert any('极极' in e for e in errors), (
        'composition_check does not report text corruption, so the author '
        'never learns about it and cannot repair it')


# ── 11. Vertical composition guidance ─────────────────────
#
# Measured on this host's authored scenes BEFORE the guidance existed: mean
# vertical span 65.0% of frame height, mean bottom dead-band 19.5%, worst two
# at 34.0% and 33.3%. The guide covered duration and edge margins but said
# nothing about filling the frame's tall axis, so 35-40% dead space at the
# bottom was a missing instruction rather than a per-scene accident.


def test_the_craft_guide_instructs_the_author_on_vertical_composition():
    """Asserts the instruction reaches the AUTHOR, not just the file.

    Driven through ``_build_prompt`` (what the model actually receives) rather
    than by reading the markdown — the guide is truncated into the prompt, so
    a section appended past the limit would be invisible while the file
    happily contains it.
    """
    from lib.motion_video._scene_author import _build_prompt

    prompt = _build_prompt(
        {'id': 'scene-001', 'text': '固态电池能量密度翻倍', 'visual': 'stat card'},
        width=1080, height=1440, duration=4.0, scene_index=1, total_scenes=3)
    low = prompt.lower()
    assert 'vertical' in low, (
        'the author is never told to fill the frame vertically — 35-40% dead '
        'space at the bottom is the result')
    assert 'dead' in low or 'space-between' in low, (
        'the guidance names no concrete remedy for the dead band')
    # A target the author can aim at, not just a prohibition.
    assert re.search(r'8[05]\s*-\s*9[05]%', prompt), (
        'no measurable vertical-span target for the author to hit')
def test_craft_guide_is_vendored_in_tree():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        'lib', 'motion_video', 'guide', 'MOTION_CRAFT.md')
    assert os.path.isfile(path), (
        'the craft guide must live in-tree beside the composition contract')
    body = open(path, encoding='utf-8').read()
    assert len(body) > 3000
