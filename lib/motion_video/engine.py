"""lib/motion_video/engine.py — Headless motion-video pipeline worker.

Fully automatic SRT → narrated MG video, driven by a TaskRuntime task
(see :mod:`lib.motion_video.runtime`). Phases (each emits an event):

    parse → storyboard → narrate? → compose → render → concat → sidecar → mux?

Composition authoring uses the zero-LLM template
(:mod:`._template`) — the chat-agent path (P1 tools) stays the creative
one; this engine is the deterministic, API-driveable floor. Scene renders
run through a bounded thread pool (the P3 parallel item): each render is
a subprocess-heavy HyperFrames call, so a small pool (default 2) already
halves wall time without melting the host.

All heavy dependencies are called through the ``lib.motion_video`` facade
so tests can monkeypatch them (same seam contract as lib.tts).
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from lib.log import get_logger
from lib.production.heartbeat import heartbeat

logger = get_logger(__name__)

__all__ = ['run_motion_task', 'run_scene_regen_task', 'run_topic_motion_task',
           'write_job_manifest', 'resume_interrupted_jobs']


def _emit(task: dict, event: dict) -> None:
    from lib.motion_video.runtime import _append_motion_event
    _append_motion_event(task, event)


def _plan_phases(task: dict) -> list:
    """The projected phase list for this job (P-UX2 stepper vocabulary).

    Computed up-front from the task config; a mid-run degrade (no TTS slot)
    keeps the narrate phase (marked degraded) and simply skips mux — the
    frontend marks skipped steps done as later phase_started events arrive.
    """
    phases: list = []
    topic = (task.get('topic') or '').strip()
    scenes_path = task.get('scenes_path') or ''
    srt_path = task.get('srt_path') or ''
    if topic and not (scenes_path and os.path.isfile(scenes_path)) \
            and not (srt_path and os.path.isfile(srt_path)):
        phases.append('research')
    if srt_path and os.path.isfile(srt_path):
        phases.append('parse')
    phases.append('storyboard')
    if task.get('narration'):
        phases.append('narrate')
    phases += ['compose', 'render', 'concat']
    if task.get('burn_in'):
        phases.append('burn_in')
    if task.get('narration'):
        phases.append('mux')
    return phases


def _phase_started(task: dict, phases: list, phase: str) -> None:
    try:
        idx = phases.index(phase) + 1
    except ValueError as _e:
        logger.debug('phase started: unparseable (%s)', _e)
        idx = 0
    _emit(task, {'type': 'phase_started', 'phase': phase,
                 'phase_index': idx, 'phase_total': len(phases),
                 'phases': list(phases), 'started_at': time.time()})


def _aborted(task: dict) -> bool:
    ev = task.get('abort_event')
    return bool(ev is not None and ev.is_set())


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
    os.replace(tmp, path)


def _scene_gate_findings(mv, scene_dir: str, scene_id: str, *,
                         abort_event=None, scene: dict | None = None,
                         html: str = '',
                         fill: dict | None = None) -> list[str]:
    """Run the REAL HyperFrames gates on one composed scene dir.

    ``check_composition_html`` is a regex pass over the contract fields and the
    determinism ban-list; it is structurally blind to the defects a viewer
    actually sees — a font the renderer cannot resolve (silently swapped for
    whatever fontconfig has), a WCAG contrast failure, a runtime console error,
    or text spilling its container. ``hyperframes check`` catches exactly
    those, costs no render, and each finding carries a fix hint.

    ADVISORY by design: a finding means the frame is UGLY, not unrenderable, so
    the caller records it on the quality axis rather than aborting a film that
    would still play. ``env_missing`` / ``aborted`` / ``timeout`` / ``chrome``
    are INFRASTRUCTURE outcomes, not composition defects, and return empty --
    measured: a scene whose composition re-checks clean was rejected once
    because Chrome hit memory pressure on that attempt, which charged an
    infra flake to the author and degraded a good scene to the plain template.

    ``fill`` is a measurement the caller already took (see
    :func:`lib.motion_video._fill.measure_fill`); its findings are derived
    purely, so the film boots ONE browser per scene rather than two and the
    telemetry can never disagree with the verdict.

    Never raises -- a gate crash must not take down a job.
    """
    # Text fidelity is judged on the HTML we already hold, so unlike the CLI
    # gates it is unaffected by an env_missing / chrome outcome -- a corrupted
    # headline is still corrupted when Chrome is out of memory. Collected
    # FIRST for that reason, then merged with whatever the CLI reported.
    fidelity: list[str] = []
    if html and scene is not None:
        try:
            fidelity = list(mv.check_text_fidelity(html, scene))
        except Exception as e:
            logger.warning('[MotionVideo] scene %s fidelity gate crashed: %s',
                           scene_id, e, exc_info=True)
        if fidelity:
            logger.warning('[MotionVideo] scene %s text fidelity: %.300s',
                           scene_id, ' | '.join(fidelity))
    # Vertical fill rides the measurement the caller already took, so it is
    # NOT swallowed by an env_missing / chrome outcome from the CLI gates. An
    # unavailable measurement is None and yields no findings, on the same
    # infrastructure-vs-defect rule.
    if html:
        try:
            from lib.motion_video._fill import findings_for_fill
            fidelity += list(findings_for_fill(fill))
        except Exception as e:
            logger.warning('[MotionVideo] scene %s fill gate crashed: %s',
                           scene_id, e, exc_info=True)
    try:
        res = mv.check_project(scene_dir, abort_event=abort_event)
    except Exception as e:
        logger.warning('[MotionVideo] scene %s gate crashed: %s', scene_id, e,
                       exc_info=True)
        return fidelity
    if res.get('ok'):
        return fidelity
    if mv.is_infra_category(res.get('category')):
        logger.info('[MotionVideo] scene %s real gates skipped (%s)',
                    scene_id, res.get('category'))
        return fidelity
    findings = fidelity + [str(e) for e in res.get('errors', [])]
    logger.warning('[MotionVideo] scene %s failed %d real-gate check(s): %.400s',
                   scene_id, len(findings), ' | '.join(findings))
    return findings


def _render_one(mv, scene_dir: str, mp4_path: str, *, quality: str,
                width: int, height: int, fps: int, expect_dur: float,
                abort_event) -> dict:
    """Render + probe-verify one scene. Returns a per-scene result dict."""
    res = mv.render_project(scene_dir, mp4_path, quality=quality,
                            abort_event=abort_event)
    if not res.get('ok'):
        return {'ok': False, 'category': res.get('category', 'unknown'),
                'detail': res.get('detail', '')}
    probe = mv.probe_video(mp4_path)
    errors = mv.verify_spec(probe, width=width, height=height, fps=fps,
                            duration=expect_dur)
    if errors:
        return {'ok': False, 'category': 'io',
                'detail': 'spec mismatch: ' + '; '.join(errors)}
    return {'ok': True, 'elapsed': res.get('elapsed')}


#: Task fields persisted to job.json so an interrupted job can be re-spawned
#: verbatim after a server restart (crash-resume is a correctness contract).
#: ``created_at`` is the job's TRUE start: ``resume_running_jobs`` re-creates
#: the task via ``create_task()``, which mints a fresh one, so without the
#: persisted value a resumed job's elapsed clock silently restarts at the
#: restart instant.
_MANIFEST_FIELDS = (
    'task_id', 'kind', 'created_at',
    'srt_path', 'scenes_path', 'workdir', 'voice', 'speed',
    'alignment', 'narration', 'quality', 'parallel', 'width', 'height',
    'burn_in', 'burn_in_fontsdir', 'topic', 'lang', 'max_scenes', 'paper_hash',
    'scene_author', 'author_rounds', 'author_token_budget',
    # The user-picked LLM model (paper panels). Persisted so a crash-resume
    # keeps composing with the SAME model instead of silently falling back
    # to the dispatcher default mid-film.
    'model',
    # Product-quality axis. Persisted because the paper panel's post-restart
    # re-attach reads job.json and NOTHING else — without it a degraded film
    # is laundered into a clean success by the next process restart.
    'artifact_quality', 'authored_scenes', 'total_scenes',
    # Per-scene quality NUMBERS (span / bottom_dead / paint nodes / graphics /
    # font faces) + their film-level roll-up. Persisted for the same reason the
    # verdict is, plus one of its own: a verdict without the numbers behind it
    # cannot be DIFFED across runs, so "did this film get better?" was only
    # answerable by re-measuring every scene by hand in a browser.
    'scene_quality', 'quality_summary',
)


def write_job_manifest(task: dict, *, kind: str, state: str) -> None:
    """Persist the job's params + lifecycle state to ``<workdir>/job.json``.

    This is the disk anchor :func:`resume_interrupted_jobs` scans on startup:
    a job whose manifest says ``running`` when the process died is re-spawned
    (the stage-graph checkpoint + per-scene mp4 skip make the re-run resume
    rather than restart).
    """
    from lib.production.jobs import write_manifest
    write_manifest(task.get('workdir') or '', task, fields=_MANIFEST_FIELDS,
                   kind=kind, state=state, log_label='MotionVideo')


def _drop_page_cache(workdir: str) -> None:
    """fadvise-DONTNEED this job's media outputs (scene mp4s, finals, wavs).

    Render intermediates are write-once GB-scale payloads; left in the page
    cache they charge the shared cgroup long after the job finished (OOM
    context in lib/cgroup_guard). Best-effort — never raises.
    """
    try:
        import glob as _glob
        from lib.cgroup_guard import drop_files_cache
        paths = []
        for pat in ('*.mp4', 'scenes/*/*.mp4', 'audio/*.wav', '*.srt'):
            paths.extend(_glob.glob(os.path.join(workdir, pat)))
        stats = drop_files_cache(paths, min_bytes=1 << 20)
        if stats['files']:
            logger.info('[MotionVideo] page-cache relief for %s: %d files/%.1fMB',
                        workdir, stats['files'], stats['bytes'] / 1e6)
    except Exception as e:
        logger.debug('[MotionVideo] page-cache relief failed for %s: %s',
                     workdir, e)


def task_id_of(task: dict) -> str:
    return task.get('task_id') or '?'


def _reusable_manifest(audio_dir: str, scenes: list) -> dict | None:
    """Return a persisted narration manifest iff it still matches the scenes.

    The recipe's timeline stage writes ``<audio_dir>/manifest.json`` after it
    synthesized narration to MEASURE durations. Reuse it here so the engine's
    narrate phase doesn't re-run TTS (and a resumed job doesn't either) — but
    ONLY when every scene id is covered and its wav still exists on disk.
    """
    from lib.json_store import read_json
    m = read_json(os.path.join(audio_dir, 'manifest.json'), default=None)
    if not isinstance(m, dict) or not m.get('ok'):
        return None
    by_id = {e.get('scene_id'): e for e in m.get('scenes') or []}
    for sc in scenes:
        if sc.get('spoken', True) is False:
            continue  # silent scenes (e.g. the sources card) need no wav
        entry = by_id.get(sc.get('id'))
        if not entry or not entry.get('wav') or not os.path.isfile(entry['wav']):
            return None
    return m


def _composition_contract_findings(html: str, scene_dir: str) -> list[str]:
    """Contract violations that make an on-disk composition STALE, not merely
    old — the things a re-run must not silently inherit.

    ``_existing_composition`` used to ask only two questions: is this the
    fallback card, and does the duration still match. Both are about the
    composition's IDENTITY, neither about whether it still meets the quality
    contract this pipeline enforces TODAY. So a composition authored before a
    contract existed was adopted verbatim for ever, and the telemetry recorded
    it as ``authored`` with no complaint.

    Measured 2026-07-29 on the target film: five of six scenes were authored on
    07-28, before the CJK font channel existed. They ship no ``@font-face``, so
    their Chinese is drawn by whatever face the render host happens to own —
    while the one scene re-authored today ships its own. One film, two
    typographies, and ``job.json`` called it 6/6 clean.

    Kept deliberately NARROW: only defects that (a) a re-author would actually
    fix and (b) are invisible to the duration/marker checks. Cosmetic drift is
    not a reason to re-spend an agent loop.
    """
    findings: list[str] = []
    try:
        from lib.motion_video._fonts import cjk_fallback_findings
        findings += list(cjk_fallback_findings(html, scene_dir))
    except Exception as e:
        logger.warning('[MotionVideo] contract check (fonts) crashed: %s', e,
                       exc_info=True)
    return findings


def _existing_composition(index_path: str, duration: float,
                          scene: dict | None = None, *,
                          width: int = 1080, height: int = 1440,
                          scene_index: int = 1, total_scenes: int = 1) -> str | None:
    """Return an on-disk composition iff it matches this scene's duration.

    Resume path for the compose stage: a scene authored before a crash must
    NOT be re-authored (that would re-spend an agent loop per restart). The
    duration check guards against a stale composition from a run whose
    timeline changed — that one is discarded and re-made.

    **A degraded FALLBACK card is never reused.** Measured 2026-07-28: this
    function compared only ``data-duration``, so it could not tell an authored
    composition from the zero-LLM template. A scene degraded by a transient
    network blip therefore had the gradient card written to ``index.html``, and
    every later resume/regen adopted it — pinning that scene to the fallback
    FOREVER, so re-running the job could never retry its authoring.

    Detection is by :func:`lib.motion_video._template.matches_template`, NOT by
    the marker alone. Measured 2026-07-29: the marker was introduced that day,
    so every fallback card written before it is marker-LESS — including
    scene-004 of the film that started this whole effort, whose 2,398-byte
    gradient card was therefore adopted as a finished authored composition on
    every re-run. A marker-only test cannot see the exact population it was
    introduced to rescue.
    """
    if not os.path.isfile(index_path):
        return None
    try:
        with open(index_path, encoding='utf-8') as f:
            html = f.read()
    except OSError as e:
        logger.debug('[MotionVideo] cannot read %s: %s', index_path, e)
        return None
    from lib.motion_video._template import matches_template
    if matches_template(html, scene or {}, width=width, height=height,
                        duration=duration, scene_index=scene_index,
                        total_scenes=total_scenes):
        logger.info('[MotionVideo] %s holds a degraded fallback card — '
                    're-authoring instead of adopting it', index_path)
        return None
    import re as _re
    m = _re.search(r'data-duration="([0-9.]+)"', html)
    if not m:
        return None
    try:
        if abs(float(m.group(1)) - float(duration)) > 0.01:
            return None
    except ValueError as _e:
        logger.debug('existing composition: unparseable (%s)', _e)
        return None
    # Identity is settled; now the CONTRACT. A composition authored before a
    # quality contract existed is stale even though it is neither a fallback
    # card nor mistimed — adopting it ships a film whose scenes disagree with
    # each other (measured: 5 of 6 scenes with no CJK face beside one that has
    # it, reported as 6/6 clean).
    stale = _composition_contract_findings(html, os.path.dirname(index_path))
    if stale:
        logger.info('[MotionVideo] %s predates the current quality contract '
                    '(%s) — re-authoring instead of adopting it',
                    index_path, stale[0][:120])
        return None
    return html


def _scene_already_rendered(mv, mp4_path: str, *, width: int, height: int,
                            fps: int, expect_dur: float) -> bool:
    """True when a scene's mp4 already exists on disk and passes verify_spec.

    Lets a re-spawned job skip scenes that were fully rendered before the
    crash — the owner's 'already-rendered shots are not re-rendered' contract.
    """
    if not os.path.isfile(mp4_path) or os.path.getsize(mp4_path) == 0:
        return False
    try:
        probe = mv.probe_video(mp4_path)
        return not mv.verify_spec(probe, width=width, height=height, fps=fps,
                                  duration=expect_dur)
    except Exception as e:
        logger.debug('[MotionVideo] resume probe of %s failed: %s', mp4_path, e)
        return False


def _commit_scene_html(index_path: str, html: str, scene: dict,
                       scene_dir: str, *, width: int, height: int,
                       duration: float, scene_index: int,
                       total_scenes: int) -> str:
    """Write ``html`` to ``index_path`` unless doing so would LOSE quality.

    Returns the HTML that is now on disk — the new one when it was committed,
    the PRESERVED one when the new composition was a regression. The caller
    must use the return value for its gates and telemetry, or it would report
    a composition that is not the one the renderer will read.

    The comparison is a single ordered grade (see
    :func:`lib.motion_video._quality.scene_grade`), so "worse" means exactly
    one thing everywhere rather than being re-derived per caller.

    A rejected composition is NOT thrown away: it is kept as the scene's draft,
    so the next attempt continues repairing it instead of starting from a blank
    page — the same contract the author's own transient-fault path uses.
    """
    from lib.motion_video._quality import is_regression, scene_grade
    from lib.motion_video._template import matches_template

    new_mode = ('template' if matches_template(
        html, scene, width=width, height=height, duration=duration,
        scene_index=scene_index, total_scenes=total_scenes) else 'authored')
    new_grade = scene_grade(html, scene_dir, mode=new_mode)

    old_html = ''
    if os.path.isfile(index_path):
        try:
            with open(index_path, encoding='utf-8') as f:
                old_html = f.read()
        except OSError as e:
            logger.debug('[MotionVideo] cannot read %s for grading: %s',
                         index_path, e)
    if old_html:
        # A stale composition (wrong duration) is not a candidate to preserve —
        # it would render at the wrong length, which is worse than any grade.
        import re as _re
        m = _re.search(r'data-duration="([0-9.]+)"', old_html)
        fresh = False
        try:
            fresh = bool(m) and abs(float(m.group(1)) - float(duration)) <= 0.01
        except ValueError as _e:
            logger.debug('[MotionVideo] old duration unparseable: %s', _e)
        if fresh:
            old_mode = ('template' if matches_template(
                old_html, scene, width=width, height=height,
                duration=duration, scene_index=scene_index,
                total_scenes=total_scenes) else 'authored')
            old_grade = scene_grade(old_html, scene_dir, mode=old_mode)
            if is_regression(old_grade, new_grade):
                logger.warning(
                    '[MotionVideo] %s REFUSING to overwrite a %s composition '
                    'with a %s one — keeping the known-good file and saving '
                    'the new attempt as a draft',
                    scene.get('id'), old_grade, new_grade)
                try:
                    from lib.motion_video._scene_author import save_draft
                    save_draft(scene_dir, html)
                except Exception as e:
                    logger.warning('[MotionVideo] could not keep the rejected '
                                   'composition as a draft: %s', e)
                return old_html
    _write(index_path, html)
    return html


def run_motion_task(task: dict) -> None:
    """Worker entry — drives the full pipeline for one motion task."""
    from lib import motion_video as mv
    from lib.motion_video.runtime import _motion_runtime

    task_id = task['task_id']
    workdir = task['workdir']
    width, height = task['width'], task['height']
    fps = 30
    phases = _plan_phases(task)
    try:
        os.makedirs(workdir, exist_ok=True)
        write_job_manifest(task, kind=task.get('kind') or 'scenes',
                           state='running')

        # ── 0. topic front-half (research → script → timeline) ──
        # When the job carries a bare TOPIC (no SRT / scenes), run the recipe
        # to synthesize scenes.json first. The recipe is itself checkpointed
        # (pipeline_state.json), so a crash mid-research resumes there.
        scenes_path = task.get('scenes_path') or ''
        topic = (task.get('topic') or '').strip()
        if topic and not (scenes_path and os.path.isfile(scenes_path)) \
                and not (task.get('srt_path') and os.path.isfile(task['srt_path'])):
            from lib.motion_video._recipe import build_scenes_from_topic
            _phase_started(task, phases, 'research')
            _emit(task, {'type': 'phase', 'phase': 'research', 'topic': topic})
            with heartbeat(task, lambda t, ev: _emit(t, ev), 'research'):
                tl = build_scenes_from_topic(
                    topic, workdir, lang=task.get('lang') or 'zh',
                    max_scenes=int(task.get('max_scenes') or 8),
                    narration=bool(task.get('narration')),
                    voice=task.get('voice') or '', speed=task.get('speed'),
                    alignment=task.get('alignment') or 'loose',
                    abort_event=task.get('abort_event'),
                    emit=lambda ev: _emit(task, {'type': 'recipe', **ev}))
            scenes_path = tl['scenes_path']
            task['scenes_path'] = scenes_path
            _emit(task, {'type': 'phase', 'phase': 'script_done',
                         'scenes': tl['scenes'],
                         'timed_from_audio': tl['timed_from_audio']})
        if _aborted(task):
            _motion_runtime.finish(task_id)
            return

        # ── 1. parse (optional when scenes are supplied directly) ──
        entries = []
        span = (0.0, 0.0)
        scenes_path = task.get('scenes_path') or ''
        if task.get('srt_path') and os.path.isfile(task['srt_path']):
            _phase_started(task, phases, 'parse')
            with open(task['srt_path'], encoding='utf-8') as f:
                entries = mv.parse_srt(f.read())
            if not entries:
                raise ValueError('SRT parsed to zero cues')
            span = mv.total_span(entries)
            _emit(task, {'type': 'phase', 'phase': 'parse',
                         'cues': len(entries),
                         'span_s': [round(s, 3) for s in span]})
        elif not (scenes_path and os.path.isfile(scenes_path)):
            raise ValueError('neither srt_path nor scenes_path available')
        if _aborted(task):
            _motion_runtime.finish(task_id)
            return

        # ── 2. storyboard (agent-supplied scenes.json wins; else zero-LLM) ──
        scenes = None
        if scenes_path and os.path.isfile(scenes_path):
            with open(scenes_path, encoding='utf-8') as f:
                scenes = json.load(f)
            if not entries:
                # Scenes-only input: the storyboard is the source of truth —
                # validate internal consistency (contiguity/overlap/sum)
                # against its OWN span.
                if scenes:
                    span = (float(scenes[0]['start']), float(scenes[-1]['end']))
            errors = mv.check_storyboard(scenes, span)
            if errors:
                raise ValueError('scenes.json failed the storyboard gate: '
                                 + ' | '.join(errors[:4]))
        if scenes is None:
            from lib.motion_video._storyboard import build_storyboard
            scenes = build_storyboard(entries)
        _write(os.path.join(workdir, 'scenes.json'),
               json.dumps(scenes, ensure_ascii=False, indent=1))
        _phase_started(task, phases, 'storyboard')
        _emit(task, {'type': 'phase', 'phase': 'storyboard',
                     'scenes': len(scenes)})

        # ── 3. narration (optional) ──
        narration = bool(task.get('narration'))
        degraded_narration = False
        manifest: dict = {}
        if narration:
            audio_dir = os.path.join(workdir, 'audio')
            # Reuse a manifest already produced by the recipe timeline stage
            # (topic jobs synthesize TTS up-front to measure durations) so we
            # never double-synthesize, and a resumed job skips narration too.
            manifest = _reusable_manifest(audio_dir, scenes) or {}
            if manifest.get('ok'):
                _emit(task, {'type': 'phase', 'phase': 'narrate',
                             'degraded': False, 'reused': True,
                             'scenes': [{'scene_id': e['scene_id'],
                                         'audio_s': e['audio_duration'],
                                         'target_s': e['target_duration'],
                                         'overflow_s': e['overflow']}
                                        for e in manifest['scenes']]})
            else:
                _phase_started(task, phases, 'narrate')
                try:
                    with heartbeat(task, lambda t, ev: _emit(t, ev), 'narrate'):
                        manifest = mv.synthesize_scene_narrations(
                            [s for s in scenes if s.get('spoken', True)],
                            audio_dir, voice=task.get('voice') or None,
                            speed=task.get('speed'),
                            alignment=task.get('alignment') or 'loose',
                            abort_event=task.get('abort_event'),
                            on_scene_done=lambda i, n, sid: _emit(task, {
                                'type': 'progress', 'phase': 'narrate',
                                'done': i, 'total': n, 'unit': 'scene',
                                'scene_id': sid}))
                except mv.NarrationAborted:
                    _motion_runtime.finish(task_id)
                    return
                if not manifest.get('ok'):
                    narration = False
                    degraded_narration = True
                    _emit(task, {'type': 'phase', 'phase': 'narrate',
                                 'degraded': True,
                                 'detail': manifest.get('detail', '')})
                    logger.warning('[MotionVideo] narration degraded: %s',
                                   manifest.get('detail'))
                else:
                    _emit(task, {'type': 'phase', 'phase': 'narrate',
                                 'degraded': False,
                                 'scenes': [
                                     {'scene_id': e['scene_id'],
                                      'audio_s': e['audio_duration'],
                                      'target_s': e['target_duration'],
                                      'overflow_s': e['overflow']}
                                     for e in manifest['scenes']]})
        if _aborted(task):
            _motion_runtime.finish(task_id)
            return

        target_by_id = {e['scene_id']: e['target_duration']
                        for e in manifest.get('scenes', [])} if manifest.get('ok') else {}

        # ── 4. compose (per-scene author when enabled, else zero-LLM template) ──
        from lib.motion_video._scene_author import (author_scene,
                                                    scene_author_enabled)
        from lib.motion_video._template import (is_template_composition,
                                                matches_template,
                                                render_scene_html)
        _phase_started(task, phases, 'compose')
        authoring = scene_author_enabled(task)
        scene_dirs: list[str] = []
        authored = 0
        scene_gate_issues: dict[str, list[str]] = {}
        scene_records: list[dict] = []
        total = len(scenes)
        for i, sc in enumerate(scenes, 1):
            dur = target_by_id.get(sc['id'], round(sc['end'] - sc['start'], 3))
            scene_dir = os.path.join(workdir, 'scenes', sc['id'])
            os.makedirs(scene_dir, exist_ok=True)
            index_path = os.path.join(scene_dir, 'index.html')
            author_rounds = author_tokens = 0
            author_craft_reads: list = []
            # Resume: a composition already on disk for this scene is kept —
            # never re-author (that would re-spend an agent loop per restart).
            existing = _existing_composition(
                index_path, dur, sc, width=width, height=height,
                scene_index=i, total_scenes=total)
            if existing is not None:
                html = existing
                # A reused authored composition is STILL authored — the work
                # was done in an earlier process. Leaving the counter at 0
                # reports a fully-resumed film as "every scene fell back",
                # which both the verdict and the panel then repeat. Measured:
                # a rescue run with all six compositions intact printed
                # authored 0/6 while shipping six authored frames.
                if not is_template_composition(html):
                    authored += 1
            elif authoring:
                with heartbeat(task, lambda t, ev: _emit(t, ev), 'compose'):
                    res = author_scene(sc, scene_dir, width=width, height=height,
                                       duration=dur, scene_index=i,
                                       total_scenes=total,
                                       max_rounds=int(task.get('author_rounds') or 0)
                                       or None,
                                       token_budget=int(task.get('author_token_budget')
                                                        or 0) or None,
                                       model=task.get('model') or None,
                                       abort_event=task.get('abort_event'))
                html = res['html']
                author_rounds = res.get('rounds', 0)
                author_tokens = res.get('tokens', 0)
                author_craft_reads = list(res.get('craft_reads') or [])
                if res['mode'] == 'authored':
                    authored += 1
                _emit(task, {'type': 'scene_authored', 'scene_id': sc['id'],
                             'mode': res['mode'], 'rounds': author_rounds,
                             'tokens': author_tokens,
                             'detail': res.get('detail', '')[:200],
                             'done': i, 'total': total})
            else:
                html = render_scene_html(sc, width=width, height=height,
                                         duration=dur, scene_index=i,
                                         total_scenes=total)
            errs = mv.check_composition_html(html)
            if errs:
                raise ValueError(f"template composition failed its own gate "
                                 f"for {sc['id']}: {' | '.join(errs)}")
            # ── NO-REGRESSION COMMIT ──
            # A re-run may only ever RAISE a scene's grade. Writing straight to
            # index.html made every re-run of an already-good film a bet on the
            # gateway: one exhausted credit or a run of 120s timeouts degraded
            # the scene, the fallback card overwrote finished work, and the next
            # concat baked it into final.mp4. Measured near-miss on the target
            # film — four authored scenes (span 87.5-93.8%, 17 graphics) were
            # already back in the author loop under HTTP 402 when the run was
            # stopped by hand. Re-running must be SAFE, not a gamble.
            html = _commit_scene_html(index_path, html, sc, scene_dir,
                                      width=width, height=height,
                                      duration=dur, scene_index=i,
                                      total_scenes=total)
            # ONE fill measurement per scene, shared by the gate verdict and
            # the persisted telemetry. Measuring twice would double the browser
            # boots AND allow the two to disagree about one composition.
            fill = None
            try:
                fill = mv.measure_fill(html)
            except Exception as e:
                logger.warning('[MotionVideo] scene %s fill measure crashed: %s',
                               sc['id'], e, exc_info=True)
            # The REAL gates (fonts/contract + runtime errors + WCAG contrast +
            # text overflow) — the regex gate above cannot see any of those,
            # and they are exactly the defects that read as "no formatting".
            # Advisory: a finding means the frame is ugly, not unrenderable, so
            # it is reported and counted on the quality axis instead of killing
            # a film that would still play.
            gate_findings = _scene_gate_findings(
                mv, scene_dir, sc['id'], abort_event=task.get('abort_event'),
                scene=sc, html=html, fill=fill)
            # The asset floor. Judged on the composition that is about to
            # render, and only for AUTHORED scenes — a template fallback owes
            # its degrade to the quality axis already, and failing it here too
            # would report one defect twice.
            scene_mode = ('template' if matches_template(
                html, sc, width=width, height=height, duration=dur,
                scene_index=i, total_scenes=total) else 'authored')
            try:
                from lib.motion_video._quality import (asset_floor_findings,
                                                       scene_telemetry)
                gate_findings += list(asset_floor_findings(
                    sc, html, scene_dir, mode=scene_mode))
            except Exception as e:
                logger.warning('[MotionVideo] scene %s asset floor crashed: %s',
                               sc['id'], e, exc_info=True)
            gate_findings += _composition_contract_findings(html, scene_dir)
            if gate_findings:
                scene_gate_issues[sc['id']] = gate_findings
                _emit(task, {'type': 'scene_gate', 'scene_id': sc['id'],
                             'ok': False, 'findings': gate_findings[:6]})
            try:
                rec = scene_telemetry(sc, html, scene_dir, mode=scene_mode,
                                      fill=fill, rounds=author_rounds,
                                      tokens=author_tokens,
                                      gate_findings=gate_findings,
                                      craft_reads=author_craft_reads)
                scene_records.append(rec)
                _emit(task, {'type': 'scene_quality', **rec})
            except Exception as e:
                logger.warning('[MotionVideo] scene %s telemetry crashed: %s',
                               sc['id'], e, exc_info=True)
            sc['_duration'] = dur
            scene_dirs.append(scene_dir)
            _emit(task, {'type': 'progress', 'phase': 'compose',
                         'done': i, 'total': total, 'unit': 'scene',
                         'scene_id': sc['id']})
            if _aborted(task):
                _motion_runtime.finish(task_id)
                return
        _emit(task, {'type': 'phase', 'phase': 'compose', 'scenes': total,
                     'authored': authored,
                     'templated': total - authored,
                     'gate_failed_scenes': sorted(scene_gate_issues)})
        if _aborted(task):
            _motion_runtime.finish(task_id)
            return

        # ── 5. render (bounded parallel) ──
        _phase_started(task, phases, 'render')
        parallel = max(1, int(task.get('parallel') or 2))
        quality = task.get('quality') or 'standard'
        mp4s: dict[str, str] = {}
        failures: list[dict] = []
        with heartbeat(task, lambda t, ev: _emit(t, ev), 'render'), \
                ThreadPoolExecutor(max_workers=parallel,
                                   thread_name_prefix='mv-render') as pool:
            futures = {}
            for sc, scene_dir in zip(scenes, scene_dirs):
                if _aborted(task):
                    break
                mp4_path = os.path.join(scene_dir, f"{sc['id']}.mp4")
                if _scene_already_rendered(mv, mp4_path, width=width,
                                           height=height, fps=fps,
                                           expect_dur=sc['_duration']):
                    mp4s[sc['id']] = mp4_path
                    _emit(task, {'type': 'scene_done', 'scene_id': sc['id'],
                                 'ok': True, 'resumed': True,
                                 'done': len(mp4s), 'total': total})
                    continue
                fut = pool.submit(
                    _render_one, mv, scene_dir, mp4_path,
                    quality=quality, width=width, height=height, fps=fps,
                    expect_dur=sc['_duration'],
                    abort_event=task.get('abort_event'))
                futures[fut] = (sc, mp4_path)
            for fut in as_completed(futures):
                sc, mp4_path = futures[fut]
                try:
                    r = fut.result()
                except Exception as e:
                    logger.error('[MotionVideo] scene %s render crashed: %s',
                                 sc['id'], e, exc_info=True)
                    r = {'ok': False, 'category': 'unknown', 'detail': str(e)}
                if r.get('ok'):
                    mp4s[sc['id']] = mp4_path
                    _emit(task, {'type': 'scene_done', 'scene_id': sc['id'],
                                 'ok': True, 'elapsed': r.get('elapsed'),
                                 'done': len(mp4s), 'total': total})
                else:
                    failures.append({'scene_id': sc['id'],
                                     'category': r.get('category'),
                                     'detail': (r.get('detail') or '')[:300]})
                    _emit(task, {'type': 'scene_done', 'scene_id': sc['id'],
                                 'ok': False,
                                 'category': r.get('category')})
        if _aborted(task):
            _motion_runtime.finish(task_id)
            return
        if failures:
            first = failures[0]
            raise RuntimeError(
                f"scene {first['scene_id']} render failed "
                f"({first['category']}): {first['detail']}")

        # ── 6. concat (silent) ──
        _phase_started(task, phases, 'concat')
        ordered = [mp4s[sc['id']] for sc in scenes]
        silent_final = os.path.join(workdir, 'final_silent.mp4')
        with heartbeat(task, lambda t, ev: _emit(t, ev), 'concat'):
            res = mv.concat_mp4s(ordered, silent_final,
                                 abort_event=task.get('abort_event'))
        if not res.get('ok'):
            raise RuntimeError('concat failed: ' + res.get('detail', ''))
        _emit(task, {'type': 'phase', 'phase': 'concat',
                     'duration_s': res.get('duration'), 'mode': res.get('mode')})

        # ── 7. sidecar SRT (loose-adjusted timeline when narrated) ──
        sidecar = os.path.join(workdir, 'final.srt')
        cursor = scenes[0]['start']
        lines: list[str] = []
        for i, sc in enumerate(scenes, 1):
            dur = sc['_duration']
            lines.append(str(i))
            lines.append(f"{mv.format_timestamp(cursor)} --> "
                         f"{mv.format_timestamp(cursor + dur)}")
            lines.append(sc.get('text') or '')
            lines.append('')
            cursor += dur
        _write(sidecar, '\n'.join(lines))

        # ── 7b. optional subtitle burn-in (re-encode) ──
        # When narration was REQUESTED but degraded to silent, the text is
        # the video's only information carrier (owner 2026-07-26) — burn the
        # sidecar subtitles in automatically. An explicit narration=False
        # never reaches here, so a deliberate silent run stays unburned.
        burn_in_eff = bool(task.get('burn_in')) or degraded_narration
        if degraded_narration and 'burn_in' not in phases:
            phases.insert(phases.index('mux') if 'mux' in phases
                          else len(phases), 'burn_in')
        video_final = silent_final
        if burn_in_eff:
            _phase_started(task, phases, 'burn_in')
            burned = os.path.join(workdir, 'final_burned.mp4')
            with heartbeat(task, lambda t, ev: _emit(t, ev), 'burn_in'):
                br = mv.burn_in_subtitles(
                    silent_final, sidecar, burned,
                    fontsdir=task.get('burn_in_fontsdir') or '',
                    abort_event=task.get('abort_event'))
            if not br.get('ok'):
                raise RuntimeError('burn-in failed: ' + br.get('detail', ''))
            video_final = burned
            _emit(task, {'type': 'phase', 'phase': 'burn_in',
                         'duration_s': br.get('duration'),
                         'auto': bool(degraded_narration)})

        # ── 8. mux (optional) ──
        final_path = os.path.join(workdir, 'final.mp4')
        if narration:
            _phase_started(task, phases, 'mux')
            wavs = [e['wav'] for e in manifest['scenes'] if e.get('wav')]
            narration_wav = os.path.join(workdir, 'audio', 'narration.wav')
            with heartbeat(task, lambda t, ev: _emit(t, ev), 'mux'):
                cn = mv.concat_narrations(wavs, narration_wav)
                if not cn.get('ok'):
                    raise RuntimeError('narration concat failed: '
                                       + cn.get('detail', ''))
                mx = mv.mux_audio_video(video_final, narration_wav, final_path,
                                        abort_event=task.get('abort_event'))
            if not mx.get('ok'):
                raise RuntimeError('mux failed: ' + mx.get('detail', ''))
            _emit(task, {'type': 'phase', 'phase': 'mux',
                         'duration_s': mx.get('duration')})
        else:
            os.replace(video_final, final_path)

        probe = mv.probe_video(final_path)
        try:
            from lib.motion_video._quality import film_quality_summary
            quality_summary = film_quality_summary(scene_records)
        except Exception as e:
            logger.warning('[MotionVideo] quality summary crashed: %s', e,
                           exc_info=True)
            quality_summary = {}
        result = {
            'final_path': final_path,
            'srt_path': sidecar,
            'duration': round(float((probe or {}).get('duration') or 0), 3),
            'scenes': total,
            'narrated': narration,
            'burn_in': burn_in_eff,
            'burn_in_auto': bool(degraded_narration),
            'workdir': workdir,
            'mode': 'engine',
            'gate_failed_scenes': sorted(scene_gate_issues),
            'scene_quality': scene_records,
            'quality_summary': quality_summary,
        }
        task['result'] = result
        # ★ Computed BEFORE the manifest write, not after: job.json is the ONLY
        #   thing the paper panel can read once the process restarts (the task
        #   is gone from the runtime and poll() 404s), so a verdict reached
        #   after the write would survive exactly until the next restart and
        #   then silently become a clean success.
        _quality = _quality_verdict(
            degraded_narration=degraded_narration,
            scene_gate_issues=scene_gate_issues,
            authoring=bool(authoring), authored=authored, total=total,
            scene_records=scene_records)
        # Same shape TaskRuntime.finish() puts on the task, so the disk
        # fallback and the live poll hand the panel one field, not two.
        task['artifact_quality'] = _quality
        task['authored_scenes'] = authored
        task['total_scenes'] = total
        task['scene_quality'] = scene_records
        task['quality_summary'] = quality_summary
        write_job_manifest(task, kind=task.get('kind') or 'scenes',
                           state='done')
        _drop_page_cache(workdir)
        _emit(task, {'type': 'final', 'final_path': final_path,
                     'duration': result['duration'], 'narrated': narration,
                     'quality_summary': quality_summary})
        _motion_runtime.finish(
            task_id, result=result,
            degraded=_quality['degraded'],
            degraded_reason=_quality['reason'])
        logger.info('[MotionVideo] task %s done: %s (%.2fs, %d scenes, '
                    'narrated=%s)', task_id, final_path, result['duration'],
                    total, narration)

    except Exception as e:
        logger.error('[MotionVideo] task %s failed: %s', task_id, e, exc_info=True)
        try:
            write_job_manifest(task, kind=task.get('kind') or 'scenes',
                               state='error')
        except Exception as _me:
            logger.debug('[MotionVideo] manifest error-state write failed: %s', _me)
        _motion_runtime.finish(task_id, error=e,
                               error_context='motion-video:engine')


def _quality_verdict(*, degraded_narration: bool, scene_gate_issues: dict,
                     authoring: bool, authored: int, total: int,
                     scene_records: list | None = None) -> dict:
    """The film's PRODUCT-quality verdict: ``{'degraded': bool, 'reason': str}``.

    Separate from ``status`` on purpose (lib/agent_core/task_runtime.py): all
    four inputs below describe a film that PLAYS, so the lifecycle is a
    legitimate 'done' — the question this answers is whether it is the film
    that was asked for.

    Four ways a playable film is still a failed artifact:

    * **silent-degraded narration** — narration was REQUESTED but no TTS slot
      existed, so durations are char-estimated rather than measured from real
      audio. That is how "8 shots all pinned at the 15.0s ceiling" ships green.
    * **scenes that failed the REAL gates** — those frames carry an
      unresolvable font, failing contrast, or text outside its container:
      exactly what a viewer calls "no formatting".
    * **wholesale fallback** — ONE scene degrading to the template is the
      designed local degrade, but when authoring was requested and EVERY scene
      fell back, the user received the plain card deck that prompted this work.
    * **an image-less authored scene** — ANY authored scene that shipped
      without a single real graphic. Measured 2026-07-29: with imagery merely
      PERMITTED by the prompt and required by nothing, the water-line was one
      background image per scene and some scenes with none. This is judged
      PER SCENE, not per film: the first version only fired when EVERY authored
      scene was bare, so a film with one text-only frame among five rich ones
      passed silently — which is precisely the "materials are scarce" defect
      a viewer notices. A text-only beat is still allowed, but it has to be
      DECLARED (``text_only_reason`` / the reserved ``sources`` marker) rather
      than left as an accident.

    A pure function rather than an inline block so it can be driven with real
    inputs: while it lived inside ``run_motion_task`` the only way to check it
    was to grep the function's source for a literal, which stops matching the
    moment the expression is reformatted — a guard that reports formatting,
    not behaviour.
    """
    reasons = []
    if degraded_narration:
        reasons.append(
            'narration requested but no TTS slot was available — shipped '
            'the silent video with burned-in subtitles; scene durations '
            'are char-estimated, not measured from audio')
    if scene_gate_issues:
        first = next(iter(scene_gate_issues.values()))[:2]
        reasons.append(
            f'{len(scene_gate_issues)} of {total} scene(s) failed the '
            f'renderer quality gates '
            f'({", ".join(sorted(scene_gate_issues))}): ' + '; '.join(first))
    all_fell_back = bool(authoring and total and authored == 0)
    if all_fell_back:
        reasons.append(
            f'boutique quality was requested but all {total} scene(s) fell '
            f'back to the plain template card — the film shipped, but not '
            f'at the quality asked for')
    # The asset floor at FILM level, judged PER SCENE. Only authored scenes
    # that are not declared text-only are eligible: a template fallback is
    # already reported above, and repeating it here would bury the distinct
    # signal that the authored scenes themselves carry no imagery.
    bare = [rec.get('scene_id') or '?' for rec in (scene_records or [])
            if rec.get('mode') == 'authored'
            and not rec.get('text_only_exempt')
            and int(rec.get('graphics') or 0) == 0]
    if bare:
        reasons.append(
            f'{len(bare)} authored scene(s) shipped with NO real graphic — no '
            f'image/video asset and no inline SVG that draws anything '
            f'({", ".join(sorted(bare))}). Imagery is available and was not '
            f'used; a text-only beat must declare a text_only_reason')
    return {
        'degraded': bool(degraded_narration or scene_gate_issues
                         or all_fell_back or bare),
        'reason': ' | '.join(reasons),
    }


def run_topic_motion_task(task: dict) -> None:
    """Worker entry alias — a topic-driven job is just a motion task whose
    front half is the recipe. Kept as a named symbol so callers/log lines and
    the resume scanner can distinguish topic jobs from scenes/SRT jobs."""
    task.setdefault('kind', 'topic')
    run_motion_task(task)


def resume_interrupted_jobs() -> int:
    """Re-spawn motion jobs left ``running`` on disk by a crashed process.

    Scans ``<motion_root>/jobs/*/job.json``; any manifest in the ``running``
    state whose task is not live in the runtime is re-spawned with its
    persisted params. The stage-graph checkpoint (pipeline_state.json) and the
    per-scene mp4 skip make the re-run resume rather than restart — the
    owner's crash-resume correctness contract. Returns the count re-spawned.

    Best-effort and idempotent: called once at startup. Never raises.
    """
    from lib.motion_video._env import motion_root
    from lib.motion_video.runtime import _motion_runtime, _new_motion_task
    from lib.production.jobs import resume_running_jobs

    def _respawn(task_id: str, workdir: str, m: dict) -> None:
        task = _new_motion_task(
            task_id, srt_path=m.get('srt_path') or '', workdir=workdir,
            voice=m.get('voice') or '', speed=m.get('speed'),
            alignment=m.get('alignment') or 'loose',
            narration=bool(m.get('narration', True)),
            quality=m.get('quality') or 'standard',
            parallel=int(m.get('parallel') or 2),
            width=int(m.get('width') or 1080),
            height=int(m.get('height') or 1440),
            scenes_path=m.get('scenes_path') or '')
        for k in ('burn_in', 'burn_in_fontsdir', 'topic', 'lang',
                  'max_scenes', 'paper_hash', 'kind', 'scene_author',
                  'author_rounds', 'author_token_budget', 'model'):
            if m.get(k) is not None:
                task[k] = m[k]
        # Restore the ORIGINAL start so the resumed job's elapsed clock
        # continues instead of restarting at the process-restart instant.
        if m.get('created_at') is not None:
            task['created_at'] = m['created_at']
        _motion_runtime.spawn(task_id, run_motion_task, task)

    return resume_running_jobs(
        os.path.join(motion_root(), 'jobs'),
        is_live=lambda tid: _motion_runtime.get(tid) is not None,
        respawn=_respawn, log_label='MotionVideo')


def run_scene_regen_task(task: dict) -> None:
    """Worker entry — re-render ONE scene of a finished job, then re-assemble.

    Task shape: ``workdir`` (the finished job's dir), ``scene_id``,
    ``regen_of`` (the original task id, echoed into the result), plus the
    usual width/height/quality/narration/burn_in fields. The scene's
    EXISTING composition (index.html — agent- or template-authored) is
    re-rendered as-is; durations, narration and storyboard are untouched,
    so re-concat / re-burn / re-mux produce a drop-in replacement
    ``final.mp4`` at the original job's stable URL.
    """
    from lib import motion_video as mv
    from lib.motion_video.runtime import _motion_runtime

    task_id = task['task_id']
    workdir = task['workdir']
    scene_id = task['scene_id']
    try:
        scenes_file = os.path.join(workdir, 'scenes.json')
        with open(scenes_file, encoding='utf-8') as f:
            scenes = json.load(f)
        target = next((sc for sc in scenes if sc.get('id') == scene_id), None)
        if target is None:
            raise ValueError(f'scene {scene_id!r} not in scenes.json')
        scene_dir = os.path.join(workdir, 'scenes', scene_id)
        index_html = os.path.join(scene_dir, 'index.html')
        if not os.path.isfile(index_html):
            raise ValueError(f'scene {scene_id!r} has no composition to re-render')
        import re as _re
        m = _re.search(r'data-duration="([0-9.]+)"',
                       open(index_html, encoding='utf-8').read())
        expect_dur = float(m.group(1)) if m else (
            float(target['end']) - float(target['start']))
        _emit(task, {'type': 'phase', 'phase': 'regen',
                     'scene_id': scene_id, 'regen_of': task.get('regen_of')})
        if _aborted(task):
            _motion_runtime.finish(task_id)
            return

        mp4_path = os.path.join(scene_dir, f'{scene_id}.mp4')
        with heartbeat(task, lambda t, ev: _emit(t, ev), 'regen'):
            r = _render_one(mv, scene_dir, mp4_path,
                            quality=task.get('quality') or 'standard',
                            width=task['width'], height=task['height'], fps=30,
                            expect_dur=expect_dur,
                            abort_event=task.get('abort_event'))
        if not r.get('ok'):
            raise RuntimeError(f"scene {scene_id} re-render failed "
                               f"({r.get('category')}): {r.get('detail', '')[:300]}")
        _emit(task, {'type': 'scene_done', 'scene_id': scene_id, 'ok': True,
                     'elapsed': r.get('elapsed')})
        if _aborted(task):
            _motion_runtime.finish(task_id)
            return

        # Re-assemble with the unchanged siblings.
        ordered = []
        for sc in scenes:
            p = os.path.join(workdir, 'scenes', sc['id'], f"{sc['id']}.mp4")
            if not os.path.isfile(p):
                raise ValueError(f"sibling scene {sc['id']!r} mp4 missing — "
                                 'cannot re-assemble')
            ordered.append(p)
        silent_final = os.path.join(workdir, 'final_silent.mp4')
        with heartbeat(task, lambda t, ev: _emit(t, ev), 'regen'):
            res = mv.concat_mp4s(ordered, silent_final,
                                 abort_event=task.get('abort_event'))
        if not res.get('ok'):
            raise RuntimeError('re-concat failed: ' + res.get('detail', ''))

        video_final = silent_final
        if task.get('burn_in'):
            sidecar = os.path.join(workdir, 'final.srt')
            burned = os.path.join(workdir, 'final_burned.mp4')
            with heartbeat(task, lambda t, ev: _emit(t, ev), 'regen'):
                br = mv.burn_in_subtitles(
                    silent_final, sidecar, burned,
                    fontsdir=task.get('burn_in_fontsdir') or '',
                    abort_event=task.get('abort_event'))
            if not br.get('ok'):
                raise RuntimeError('re-burn failed: ' + br.get('detail', ''))
            video_final = burned

        final_path = os.path.join(workdir, 'final.mp4')
        if task.get('narration'):
            narration_wav = os.path.join(workdir, 'audio', 'narration.wav')
            with heartbeat(task, lambda t, ev: _emit(t, ev), 'regen'):
                mx = mv.mux_audio_video(video_final, narration_wav, final_path,
                                        abort_event=task.get('abort_event'))
            if not mx.get('ok'):
                raise RuntimeError('re-mux failed: ' + mx.get('detail', ''))
        else:
            os.replace(video_final, final_path)

        probe = mv.probe_video(final_path)
        result = {'final_path': final_path,
                  'regen_of': task.get('regen_of'),
                  'scene_id': scene_id,
                  'duration': round(float((probe or {}).get('duration') or 0), 3)}
        _drop_page_cache(workdir)
        _emit(task, {'type': 'final', 'final_path': final_path,
                     'scene_id': scene_id, 'regen_of': task.get('regen_of')})
        _motion_runtime.finish(task_id, result=result)
        logger.info('[MotionVideo] regen %s of job %s done', scene_id,
                    task.get('regen_of'))
    except Exception as e:
        logger.error('[MotionVideo] regen task %s failed: %s', task_id, e,
                     exc_info=True)
        _motion_runtime.finish(task_id, error=e,
                               error_context='motion-video:regen')
