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

import hashlib
import json
import math
import os
import time
from concurrent.futures import (FIRST_COMPLETED, ThreadPoolExecutor,
                                as_completed, wait)

from lib.agent_core.events import Phase, build_phase
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
    if task.get('narration') or task.get('audio_plan') \
            or task.get('audio_plan_path'):
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
    from lib.json_store import write_text_atomic
    write_text_atomic(path, text)


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
    'task_id', 'user_id', 'kind', 'created_at',
    'srt_path', 'scenes_path', 'workdir', 'voice', 'speed',
    'alignment', 'narration', 'quality', 'parallel', 'width', 'height',
    'burn_in', 'burn_in_fontsdir', 'topic', 'lang', 'max_scenes', 'paper_hash',
    'scene_author', 'author_token_budget', 'creative_mode', 'director',
    'audio_plan', 'audio_plan_path', 'audio_base_dir',
    # The user-picked LLM model (paper panels). Persisted so a crash-resume
    # keeps composing with the SAME model instead of silently falling back
    # to the dispatcher default mid-film.
    'model', 'qa_model',
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
    owner_user_id = int(task.get('_userId') or task.get('user_id') or 0)
    if owner_user_id < 1:
        raise ValueError('motion manifest requires an explicit owner user_id')
    manifest_task = dict(task)
    manifest_task['user_id'] = owner_user_id
    write_manifest(task.get('workdir') or '', manifest_task,
                   fields=_MANIFEST_FIELDS,
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


def _reusable_manifest(audio_dir: str, scenes: list, *, voice=None, speed=None,
                       alignment: str = 'loose', tail_pad: float | None = None
                       ) -> dict | None:
    """Return a persisted narration manifest iff it still matches the scenes.

    The recipe's timeline stage writes ``<audio_dir>/manifest.json`` after it
    synthesized narration to MEASURE durations. Reuse it here so the engine's
    narrate phase doesn't re-run TTS (and a resumed job doesn't either) — but
    ONLY when ordered scene text/timing, synthesis settings, and bounded WAV
    content hashes still match. Older existence-only manifests fail closed and
    are regenerated once rather than risking stale speech in a new film.
    """
    from lib.json_store import read_json
    from lib.motion_video import _audio as narration_audio

    m = read_json(os.path.join(audio_dir, 'manifest.json'), default=None)
    if (not isinstance(m, dict) or not m.get('ok')
            or m.get('manifest_version')
            != narration_audio._NARRATION_MANIFEST_VERSION):
        return None
    resolved_tail_pad = (narration_audio._DEFAULT_TAIL_PAD
                         if tail_pad is None else tail_pad)
    try:
        expected_request = narration_audio._manifest_request_contract(
            voice=voice, speed=speed, alignment=alignment,
            tail_pad=resolved_tail_pad)
    except ValueError:
        return None
    if m.get('request') != expected_request:
        return None

    if (not isinstance(scenes, list)
            or any(not isinstance(scene, dict) for scene in scenes)):
        return None
    expected_scenes = [scene for scene in scenes
                       if scene.get('spoken', True) is not False]
    entries = m.get('scenes')
    if not isinstance(entries, list) or len(entries) != len(expected_scenes):
        return None
    expected_ids = [str(scene.get('id') or '') for scene in expected_scenes]
    manifest_ids = [str(entry.get('scene_id') or '')
                    for entry in entries if isinstance(entry, dict)]
    if (len(manifest_ids) != len(entries) or manifest_ids != expected_ids
            or len(set(manifest_ids)) != len(manifest_ids)):
        return None

    audio_root = os.path.realpath(audio_dir)
    retained_bytes = 0
    for scene, entry in zip(expected_scenes, entries, strict=True):
        if entry.get('text_sha256') != narration_audio._scene_text_sha256(
                scene.get('text')):
            return None
        try:
            scene_duration = (float(scene.get('end') or 0)
                              - float(scene.get('start') or 0))
            source_duration = float(entry.get('srt_duration'))
            target_duration = float(entry.get('target_duration'))
        except (TypeError, ValueError, OverflowError):
            return None
        if (not all(math.isfinite(value) for value in (
                scene_duration, source_duration, target_duration))
                or (abs(scene_duration - source_duration) > 0.002
                    and abs(scene_duration - target_duration) > 0.002)):
            return None

        wav_value = str(entry.get('wav') or '')
        if not wav_value:
            if target_duration <= 0 and not str(scene.get('text') or '').strip():
                continue
            return None
        wav_path = (wav_value if os.path.isabs(wav_value)
                    else os.path.join(audio_dir, wav_value))
        wav_path = os.path.realpath(wav_path)
        try:
            if os.path.commonpath((audio_root, wav_path)) != audio_root:
                return None
        except ValueError:
            return None
        try:
            declared_bytes = int(entry.get('wav_bytes'))
        except (TypeError, ValueError, OverflowError):
            return None
        try:
            actual_bytes = os.path.getsize(wav_path)
        except OSError:
            return None
        if (declared_bytes <= 0
                or declared_bytes > narration_audio._MAX_SCENE_AUDIO_BYTES
                or not os.path.isfile(wav_path)
                or actual_bytes != declared_bytes):
            return None
        retained_bytes += declared_bytes
        if retained_bytes > narration_audio._MAX_NARRATION_DISK_BYTES:
            return None
        digest = hashlib.sha256()
        read_bytes = 0
        try:
            with open(wav_path, 'rb') as wav_file:
                while read_bytes <= declared_bytes:
                    chunk = wav_file.read(min(
                        1024 * 1024, declared_bytes - read_bytes + 1))
                    if not chunk:
                        break
                    read_bytes += len(chunk)
                    if read_bytes > declared_bytes:
                        return None
                    digest.update(chunk)
        except OSError:
            return None
        if (read_bytes != declared_bytes
                or digest.hexdigest() != str(entry.get('wav_sha256') or '')):
            return None
        entry['wav'] = wav_path
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
                          scene_index: int = 1, total_scenes: int = 1,
                          theme=None) -> str | None:
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
                        total_scenes=total_scenes, theme=theme):
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
                       total_scenes: int, theme=None) -> str:
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
        scene_index=scene_index, total_scenes=total_scenes,
        theme=theme) else 'authored')
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
                total_scenes=total_scenes, theme=theme) else 'authored')
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


_MOTION_VISUAL_QA_CACHE_NAME = '.tofu-motion-visual-qa.json'
_MOTION_VISUAL_QA_CACHE_VERSION = 'motion-scene-visual-qa-v1'
_MAX_MOTION_VISUAL_QA_CACHE_BYTES = 64 * 1024
_MAX_MOTION_VISUAL_QA_FINDINGS = 32


def _visual_qa_round(task: dict, sc: dict, scene_dir: str,
                     index_path: str, html: str, *, width: int, height: int,
                     duration: float, scene_index: int, total_scenes: int,
                     theme=None, author_prompt_context_provider=None) -> str:
    """One screenshot → VLM-checklist → author-repair round for a scene.

    Returns the html that should stand on disk afterwards (the repair when it
    survived the no-regression commit, else the original). NEVER raises: QA
    is an enhancement layer, and an outage here must not touch the film.

    Skips (all logged, all silent to the film): template fallbacks (already
    reported on the quality axis), missing playwright/Chromium, no
    vision-capable slot, VLM dispatch failure, unparseable replies.
    """
    try:
        from lib.motion_video._template import matches_template
        if matches_template(html, sc, width=width, height=height,
                            duration=duration, scene_index=scene_index,
                            total_scenes=total_scenes, theme=theme):
            return html
        import lib.design_sys.visual_qa as vqa
        avail = task.get('_visual_qa_available')
        if avail is None:
            avail = vqa.visual_qa_available()
            task['_visual_qa_available'] = avail
            if not avail[0]:
                logger.info('[MotionVideo] visual QA skipped for the whole '
                            'film: %s', avail[1])
        if not avail[0]:
            return html
        from lib.motion_video._scene_author import author_scene, save_draft
        shot_dir = os.path.join(scene_dir, '.tofu-draft')
        os.makedirs(shot_dir, exist_ok=True)
        shot = os.path.join(shot_dir, 'qa-frame.png')
        from lib.design_sys.temporal_qa import (
            DEFAULT_PROGRESS_POINTS,
            screenshot_timeline_contact_sheet,
        )
        progresses = sc.get('qa_progresses') or DEFAULT_PROGRESS_POINTS
        screenshot_timeline_contact_sheet(
            scene_dir, shot, width=width, height=height,
            progresses=progresses)
        qa_labels = ' / '.join(
            f'{round(float(point) * 100)}%' for point in progresses)
        constraints = '; '.join(str(item) for item in
                                (sc.get('recipe_constraints') or []))
        subject = (
            f'视频镜头配方验收接触表（左至右为 {qa_labels}）；'
            f'镜头配方={sc.get("shot_recipe") or "unspecified"}，'
            f'运动族={sc.get("motion_family") or "unspecified"}，'
            f'最低落定停留={sc.get("hold_s") or 0}s。'
            + (f'验收约束：{constraints}' if constraints else ''))
        qa_model = vqa.resolve_visual_qa_model(task.get('qa_model') or '')
        cache_path = os.path.join(
            scene_dir, _MOTION_VISUAL_QA_CACHE_NAME)
        cache = vqa.load_visual_qa_cache(
            cache_path, version=_MOTION_VISUAL_QA_CACHE_VERSION,
            max_entries=1, max_bytes=_MAX_MOTION_VISUAL_QA_CACHE_BYTES)
        try:
            input_sha256 = vqa.qa_frame_input_sha256(
                shot, theme=theme, subject=subject, model=qa_model)
        except (OSError, ValueError) as exc:
            logger.debug('[MotionVideo] %s visual QA cache identity '
                         'unavailable: %s', sc.get('id'), exc)
            input_sha256 = ''
        cached = (vqa.cached_visual_qa_result(
            cache['entries'].get('scene'), input_sha256,
            max_findings=_MAX_MOTION_VISUAL_QA_FINDINGS)
                  if input_sha256 else None)
        if cached is not None:
            res = cached
        else:
            res = vqa.qa_frame(
                shot, theme=theme, label=sc.get('id'), subject=subject,
                model=qa_model, abort_check=lambda: _aborted(task))
            if input_sha256:
                vqa.remember_visual_qa_result(
                    cache, cache_path, 'scene', input_sha256, res,
                    max_entries=1,
                    max_bytes=_MAX_MOTION_VISUAL_QA_CACHE_BYTES,
                    max_findings=_MAX_MOTION_VISUAL_QA_FINDINGS)
        if not res.get('ok'):
            _emit(task, {'type': 'scene_visual_qa', 'scene_id': sc.get('id'),
                         'ran': False,
                         'reason': res.get('reason') or res.get('skipped')})
            return html
        actionable = [f for f in res.get('findings') or []
                      if f.get('severity') in ('blocker', 'major')]
        _emit(task, {'type': 'scene_visual_qa', 'scene_id': sc.get('id'),
                     'ran': True, 'findings': len(res.get('findings') or []),
                     'actionable': len(actionable),
                     'reused': bool(res.get('reused')),
                     'qa_progresses': list(progresses),
                     'shot_recipe': sc.get('shot_recipe') or ''})
        if not actionable:
            return html
        logger.info('[MotionVideo] %s visual QA: %d actionable finding(s) — '
                    'entering an author repair pass',
                    sc.get('id'), len(actionable))
        if _aborted(task):
            return html
        save_draft(scene_dir, html)   # the repair resumes FROM this frame
        repair = author_scene(
            sc, scene_dir, width=width, height=height, duration=duration,
            scene_index=scene_index, total_scenes=total_scenes,
            token_budget=45000,
            model=task.get('model') or None,
            abort_event=task.get('abort_event'), theme=theme,
            prompt_context=(author_prompt_context_provider()
                            if author_prompt_context_provider else None),
            extra_findings=[vqa.findings_text(actionable)],
            transient_attempts=1)
        if repair.get('mode') != 'authored':
            return html
        # A QA repair may inherit a CDN example from its draft.  Seal it
        # *before* the sole guarded commit: writing the localised result after
        # the commit would bypass the no-regression guarantee.
        from lib.motion_video._runtime_assets import localise_gsap_html
        repair_html = localise_gsap_html(repair['html'], scene_dir)
        return _commit_scene_html(index_path, repair_html, sc, scene_dir,
                                  width=width, height=height,
                                  duration=duration, scene_index=scene_index,
                                  total_scenes=total_scenes, theme=theme)
    except Exception as e:
        logger.warning('[MotionVideo] %s visual QA round failed (%s) — '
                       'keeping the composition as-is', sc.get('id'), e)
        return html


_MAX_SCENE_AUTHOR_WORKERS = 2


def _scene_author_worker_limit() -> int:
    """Resolve one budget safe for text calls and author-triggered images."""
    from lib.production.image_policy import production_image_fanout
    from runtime_guards import resolve_resource_budget
    return min(
        resolve_resource_budget(
            'TOFU_PRODUCTION_LLM_FANOUT',
            maximum=_MAX_SCENE_AUTHOR_WORKERS),
        production_image_fanout(),
        _MAX_SCENE_AUTHOR_WORKERS,
    )


def _run_bounded_scene_authors(jobs: list[dict], *, max_workers: int,
                               run_author, accept_result,
                               abort_check) -> bool:
    """Run independent scene authors with a bounded, work-conserving window.

    Only active futures retain full HTML results. ``accept_result`` runs on
    the caller thread and is expected to commit each scene before another job
    is admitted. Once abort or a fatal result/commit error is observed, queued
    scenes are never submitted; already-running authors may finish so their
    own atomic drafts remain recoverable.

    Returns ``True`` when cancellation won the batch; otherwise raises the
    first scene-order failure or returns ``False``.
    """
    queued = iter(jobs)
    worker_limit = max(1, min(int(max_workers), len(jobs)))
    active: dict = {}
    failure: tuple[int, Exception] | None = None
    aborted = bool(abort_check())

    def _submit_one(pool) -> bool:
        nonlocal aborted
        if aborted or failure is not None:
            return False
        try:
            job = next(queued)
        except StopIteration:
            return False
        if abort_check():
            aborted = True
            return False
        future = pool.submit(run_author, job)
        active[future] = job
        return True

    if aborted or not jobs:
        return aborted
    with ThreadPoolExecutor(
            max_workers=worker_limit,
            thread_name_prefix='motion-scene-author') as pool:
        for _ in range(worker_limit):
            _submit_one(pool)
        while active:
            completed, _not_done = wait(
                active, return_when=FIRST_COMPLETED)
            ordered = sorted(completed,
                             key=lambda future: active[future]['index'])
            for future in ordered:
                job = active.pop(future)
                try:
                    result = future.result()
                    if abort_check():
                        aborted = True
                        continue
                    accept_result(job, result)
                except Exception as exc:
                    if failure is None or job['index'] < failure[0]:
                        failure = (job['index'], exc)
            if abort_check():
                aborted = True
            while (not aborted and failure is None
                   and len(active) < worker_limit and _submit_one(pool)):
                pass
    if aborted:
        return True
    if failure is not None:
        raise failure[1]
    return False


def run_motion_task(task: dict) -> None:
    """Worker entry — drives the full pipeline for one motion task."""
    from lib import motion_video as mv
    from lib.motion_video.runtime import _motion_runtime

    task_id = task['task_id']
    workdir = task['workdir']
    owner_user_id = int(task.get('_userId') or task.get('user_id') or 0)
    if owner_user_id < 1:
        raise ValueError('motion task requires an explicit owner user_id')
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
            _emit(task, build_phase(Phase.RESEARCH, topic=topic))
            with heartbeat(task, lambda t, ev: _emit(t, ev), 'research'):
                tl = build_scenes_from_topic(
                    topic, workdir, lang=task.get('lang') or 'zh',
                    max_scenes=int(task.get('max_scenes') or 8),
                    creative_mode=task.get('creative_mode') or 'director',
                    narration=bool(task.get('narration')),
                    voice=task.get('voice') or '', speed=task.get('speed'),
                    alignment=task.get('alignment') or 'loose',
                    model=task.get('model') or None,
                    owner_user_id=owner_user_id,
                    abort_event=task.get('abort_event'),
                    emit=lambda ev: _emit(task, {'type': 'recipe', **ev}))
            scenes_path = tl['scenes_path']
            task['scenes_path'] = scenes_path
            task['creative_mode'] = tl.get('creative_mode') or 'director'
            task['director'] = tl.get('director') or {}
            _emit(task, build_phase(Phase.SCRIPT_DONE,
                                    scenes=tl['scenes'],
                                    timed_from_audio=tl['timed_from_audio'],
                                    creative_mode=tl.get('creative_mode'),
                                    director=tl.get('director') or {}))
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
            _emit(task, build_phase(Phase.PARSE,
                                    cues=len(entries),
                                    span_s=[round(s, 3) for s in span]))
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
        from lib.motion_video._creative_plan import normalise_film_plan
        normalise_film_plan(scenes)
        from lib.motion_video._shot_recipes import (
            shot_contract_errors,
            shot_plan_findings,
        )
        shot_errors = shot_contract_errors(scenes)
        if shot_errors:
            raise ValueError('normalized shot plan failed its own contract: '
                             + ' | '.join(shot_errors[:6]))
        for scene in scenes:
            scene['plan_findings'] = []
        plan_findings = shot_plan_findings(scenes)
        scenes_by_id = {str(scene.get('id') or ''): scene for scene in scenes}
        for finding in plan_findings:
            target = scenes_by_id.get(str(finding.get('scene_id') or ''))
            if target is not None:
                target['plan_findings'].append(finding)
        if plan_findings:
            logger.warning('[MotionVideo] shot plan has %d advisory finding(s): '
                           '%.400s', len(plan_findings),
                           ' | '.join(str(item.get('issue') or '')
                                      for item in plan_findings))
            _emit(task, {'type': 'shot_plan_findings',
                         'findings': plan_findings})
        _write(os.path.join(workdir, 'scenes.json'),
               json.dumps(scenes, ensure_ascii=False, indent=1))
        _phase_started(task, phases, 'storyboard')
        _emit(task, build_phase(Phase.STORYBOARD, scenes=len(scenes)))

        # ── 3. narration (optional) ──
        narration = bool(task.get('narration'))
        degraded_narration = False
        manifest: dict = {}
        if narration:
            audio_dir = os.path.join(workdir, 'audio')
            # Reuse a manifest already produced by the recipe timeline stage
            # (topic jobs synthesize TTS up-front to measure durations) so we
            # never double-synthesize, and a resumed job skips narration too.
            manifest = _reusable_manifest(
                audio_dir, scenes, voice=task.get('voice') or None,
                speed=task.get('speed'),
                alignment=task.get('alignment') or 'loose') or {}
            if manifest.get('ok'):
                _emit(task, build_phase(
                    Phase.NARRATE, degraded=False, reused=True,
                    scenes=[{'scene_id': e['scene_id'],
                             'audio_s': e['audio_duration'],
                             'target_s': e['target_duration'],
                             'overflow_s': e['overflow']}
                            for e in manifest['scenes']]))
            else:
                _phase_started(task, phases, 'narrate')
                try:
                    with heartbeat(task, lambda t, ev: _emit(t, ev), 'narrate'):
                        manifest = mv.synthesize_scene_narrations(
                            [s for s in scenes if s.get('spoken', True)],
                            audio_dir, voice=task.get('voice') or None,
                            speed=task.get('speed'),
                            alignment=task.get('alignment') or 'loose',
                            owner_user_id=owner_user_id,
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
                    _emit(task, build_phase(
                        Phase.NARRATE, degraded=True,
                        detail=manifest.get('detail', '')))
                    logger.warning('[MotionVideo] narration degraded: %s',
                                   manifest.get('detail'))
                else:
                    from lib.json_store import write_json_atomic
                    write_json_atomic(
                        os.path.join(audio_dir, 'manifest.json'), manifest)
                    _emit(task, build_phase(
                        Phase.NARRATE, degraded=False,
                        scenes=[
                            {'scene_id': e['scene_id'],
                             'audio_s': e['audio_duration'],
                             'target_s': e['target_duration'],
                             'overflow_s': e['overflow']}
                            for e in manifest['scenes']]))
        if _aborted(task):
            _motion_runtime.finish(task_id)
            return

        target_by_id = {e['scene_id']: e['target_duration']
                        for e in manifest.get('scenes', [])} if manifest.get('ok') else {}

        # ── Program timeline: spoken/content time is immutable. Overlap edits
        # receive visual handles so transitions never shorten the narration or
        # move subtitle clocks.
        from lib.motion_video._timeline import (
            normalise_timeline_contract,
            timeline_contract_errors,
        )
        timeline_info = normalise_timeline_contract(
            scenes, durations=target_by_id, fps=fps)
        timeline_errors = timeline_contract_errors(scenes)
        if timeline_errors:
            raise ValueError('timeline contract failed: '
                             + ' | '.join(timeline_errors[:6]))
        _write(os.path.join(workdir, 'scenes.json'),
               json.dumps(scenes, ensure_ascii=False, indent=1))
        _emit(task, {'type': 'timeline_contract', **timeline_info})

        # Audio is validated and staged before expensive composition/render.
        # A missing license or remote runtime URL is therefore an early,
        # actionable contract failure, not a late ffmpeg surprise.
        from lib.motion_video._audio_cues import (
            audio_plan_errors,
            audio_plan_summary,
            load_audio_plan,
            write_audio_attribution,
        )
        audio_plan = load_audio_plan(
            task, scenes, workdir, timeline_info['duration_s'])
        if audio_plan:
            audio_errors = audio_plan_errors(audio_plan)
            if audio_errors:
                raise ValueError('audio plan failed: '
                                 + ' | '.join(audio_errors[:6]))
            from lib.json_store import write_json_atomic
            write_json_atomic(os.path.join(workdir, 'audio_plan.json'),
                              audio_plan)
            write_audio_attribution(
                audio_plan, os.path.join(workdir, 'audio_attribution.txt'))
            _emit(task, {'type': 'audio_plan',
                         **audio_plan_summary(audio_plan)})

        # ── 4. compose (per-scene author when enabled, else zero-LLM template) ──
        from lib.motion_video._scene_author import (
            author_scene, prepare_author_prompt_context,
            prepare_parallel_author_dependencies,
            scene_author_enabled,
        )
        from lib.motion_video._template import (is_template_composition,
                                                matches_template,
                                                render_scene_html)
        from lib.motion_video._runtime_assets import (ensure_gsap,
                                                      localise_gsap_html)
        _phase_started(task, phases, 'compose')
        authoring = scene_author_enabled(task)
        # Film-level theme (design-system P1): ONE palette + font pairing +
        # scenario bible for the whole film, replacing the old per-scene
        # colour roulette. Default ON; any failure keeps the legacy path.
        theme = None
        try:
            from lib.design_sys.themes import (classify_scenario,
                                               default_theme_id, get_theme)
            _tid = (task.get('theme') or '').strip()
            if not _tid:
                _tid = default_theme_id(classify_scenario(topic))
            theme = get_theme(_tid)
            if theme is not None:
                logger.info('[MotionVideo] film theme: %s (%s)',
                            theme.id, theme.label)
        except Exception as e:
            logger.warning('[MotionVideo] theme resolution failed, '
                           'legacy path: %s', e)
            theme = None
        scene_dirs: list[str] = []
        authored = 0
        scene_gate_issues: dict[str, list[str]] = {}
        scene_records: list[dict] = []
        total = len(scenes)
        author_prompt_context = None

        def _get_author_prompt_context():
            nonlocal author_prompt_context
            if author_prompt_context is None:
                author_prompt_context = prepare_author_prompt_context(
                    width=width, height=height, total_scenes=total)
            return author_prompt_context

        # Prepare scene-local directories/assets and identify resume hits on
        # the caller thread. Only missing compositions enter the bounded model
        # window; completed HTML is committed immediately by the caller, so
        # full results are retained only by active futures (never all scenes).
        prepared_scenes: list[dict] = []
        author_jobs: list[dict] = []
        media_credit_providers: set[str] = set()
        for i, sc in enumerate(scenes, 1):
            if _aborted(task):
                _motion_runtime.finish(task_id)
                return
            if (sc.get('spoken') is False and media_credit_providers
                    and 'Pexels' in media_credit_providers
                    and 'pexels.com' not in str(sc.get('text') or '').lower()):
                sc['text'] = (str(sc.get('text') or '').rstrip()
                              + ' · Media: Pexels.com')
            content_dur = float(sc['content_duration_s'])
            dur = float(sc['render_duration_s'])
            scene_dir = os.path.join(workdir, 'scenes', sc['id'])
            os.makedirs(scene_dir, exist_ok=True)
            # Browser rendering must never depend on its proxy/CDN access.
            # Stage the pinned runtime before authoring (so its asset gate can
            # verify the reference) and localise old/resumed compositions too.
            if not ensure_gsap(scene_dir):
                raise RuntimeError(
                    f'could not stage the pinned GSAP runtime for {sc["id"]}')
            asset_preflight = {'resolved': [], 'findings': []}
            if authoring:
                try:
                    from lib.motion_video._asset_preflight import prepare_scene_assets
                    asset_preflight = prepare_scene_assets(sc, scene_dir)
                    media_credit_providers.update(
                        str(record.get('provider') or '')
                        for record in (asset_preflight.get('resolved') or [])
                        if record.get('provider'))
                except Exception as e:
                    logger.warning('[MotionVideo] %s asset preflight crashed: %s',
                                   sc.get('id'), e, exc_info=True)
                    asset_preflight['findings'] = [
                        f'asset preflight crashed: {e}']
            index_path = os.path.join(scene_dir, 'index.html')
            # Resume: a composition already on disk for this scene is kept —
            # never re-author (that would re-spend an agent loop per restart).
            existing = _existing_composition(
                index_path, dur, sc, width=width, height=height,
                scene_index=i, total_scenes=total, theme=theme)
            prepared = {
                'index': i,
                'scene': sc,
                'content_duration': content_dur,
                'duration': dur,
                'scene_dir': scene_dir,
                'index_path': index_path,
                'asset_preflight': asset_preflight,
                'has_existing': existing is not None,
                'author_result': None,
            }
            prepared_scenes.append(prepared)
            if existing is None and authoring:
                author_jobs.append(prepared)

        if author_jobs:
            abort_check = lambda: _aborted(task)

            def _accept_author_result(job: dict, res: dict) -> None:
                sc = job['scene']
                rounds = res.get('rounds', 0)
                tokens = res.get('tokens', 0)
                job['author_result'] = {
                    'mode': res['mode'],
                    'rounds': rounds,
                    'tokens': tokens,
                    'craft_reads': list(res.get('craft_reads') or []),
                    'detail': res.get('detail', ''),
                }
                _emit(task, {
                    'type': 'scene_authored', 'scene_id': sc['id'],
                    'mode': res['mode'], 'rounds': rounds, 'tokens': tokens,
                    'detail': res.get('detail', '')[:200],
                    'done': job['index'], 'total': total,
                })
                html = localise_gsap_html(res['html'], job['scene_dir'])
                errs = mv.check_composition_html(html)
                if errs:
                    raise ValueError(
                        f"template composition failed its own gate for "
                        f"{sc['id']}: {' | '.join(errs)}")
                _commit_scene_html(
                    job['index_path'], html, sc, job['scene_dir'],
                    width=width, height=height, duration=job['duration'],
                    scene_index=job['index'], total_scenes=total,
                    theme=theme)

            with heartbeat(task, lambda t, ev: _emit(t, ev), 'compose'):
                worker_limit = _scene_author_worker_limit()
                if worker_limit > 1:
                    needs_craft = any(
                        bool(job['scene'].get('allow_craft_browse'))
                        for job in author_jobs)
                    if not prepare_parallel_author_dependencies(
                            theme=theme, needs_craft=needs_craft):
                        logger.info(
                            '[MotionVideo] shared author dependencies are not '
                            'fully durable; using one scene-author worker')
                        worker_limit = 1
                if abort_check():
                    batch_aborted = True
                else:
                    batch_prompt_context = _get_author_prompt_context()
                    author_token_budget = int(
                        task.get('author_token_budget') or 0) or None

                    def _run_author(job: dict) -> dict:
                        return author_scene(
                            job['scene'], job['scene_dir'], width=width,
                            height=height, duration=job['duration'],
                            scene_index=job['index'], total_scenes=total,
                            token_budget=author_token_budget,
                            model=task.get('model') or None,
                            abort_event=task.get('abort_event'), theme=theme,
                            prompt_context=batch_prompt_context)

                    batch_aborted = _run_bounded_scene_authors(
                        author_jobs, max_workers=worker_limit,
                        run_author=_run_author,
                        accept_result=_accept_author_result,
                        abort_check=abort_check)
            if batch_aborted or _aborted(task):
                _motion_runtime.finish(task_id)
                return

        for prepared in prepared_scenes:
            i = prepared['index']
            sc = prepared['scene']
            content_dur = prepared['content_duration']
            dur = prepared['duration']
            scene_dir = prepared['scene_dir']
            index_path = prepared['index_path']
            asset_preflight = prepared['asset_preflight']
            author_result = prepared['author_result']
            author_rounds = author_tokens = 0
            author_craft_reads: list = []
            precommitted = author_result is not None
            if precommitted:
                with open(index_path, encoding='utf-8') as f:
                    html = f.read()
                author_rounds = author_result['rounds']
                author_tokens = author_result['tokens']
                author_craft_reads = author_result['craft_reads']
                if author_result['mode'] == 'authored':
                    authored += 1
            elif prepared['has_existing']:
                with open(index_path, encoding='utf-8') as f:
                    html = f.read()
                # A reused authored composition is STILL authored — the work
                # was done in an earlier process. Leaving the counter at 0
                # reports a fully-resumed film as "every scene fell back",
                # which both the verdict and the panel then repeat. Measured:
                # a rescue run with all six compositions intact printed
                # authored 0/6 while shipping six authored frames.
                if not is_template_composition(html):
                    authored += 1
            elif authoring:
                raise RuntimeError(
                    f'scene author left {sc["id"]!r} without a result')
            else:
                html = render_scene_html(sc, width=width, height=height,
                                         duration=dur, scene_index=i,
                                         total_scenes=total, theme=theme)
            if not precommitted:
                html = localise_gsap_html(html, scene_dir)
                errs = mv.check_composition_html(html)
                if errs:
                    raise ValueError(
                        f"template composition failed its own gate for "
                        f"{sc['id']}: {' | '.join(errs)}")
                # ── NO-REGRESSION COMMIT ──
                # A re-run may only ever RAISE a scene's grade. New author
                # results were already committed by the bounded batch callback;
                # resume/template paths reach the same guard here.
                html = _commit_scene_html(
                    index_path, html, sc, scene_dir, width=width,
                    height=height, duration=dur, scene_index=i,
                    total_scenes=total, theme=theme)
            # ── Visual QA round (design-system P2): a VLM looks at the
            # settled frame with the designer checklist; actionable findings
            # go back to the author for ONE repair round. Never blocks the
            # film: unavailable infra / no vision model → silent skip.
            if authoring and not _aborted(task):
                html = _visual_qa_round(
                    task, sc, scene_dir, index_path, html,
                    width=width, height=height, duration=dur,
                    scene_index=i, total_scenes=total, theme=theme,
                    author_prompt_context_provider=(
                        _get_author_prompt_context))
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
            gate_findings = list(asset_preflight.get('findings') or [])
            gate_findings += _scene_gate_findings(
                mv, scene_dir, sc['id'], abort_event=task.get('abort_event'),
                scene=sc, html=html, fill=fill)
            # The asset floor. Judged on the composition that is about to
            # render, and only for AUTHORED scenes — a template fallback owes
            # its degrade to the quality axis already, and failing it here too
            # would report one defect twice.
            scene_mode = ('template' if matches_template(
                html, sc, width=width, height=height, duration=dur,
                scene_index=i, total_scenes=total, theme=theme) else 'authored')
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
            sc['_content_duration'] = content_dur
            scene_dirs.append(scene_dir)
            _emit(task, {'type': 'progress', 'phase': 'compose',
                         'done': i, 'total': total, 'unit': 'scene',
                         'scene_id': sc['id']})
            if _aborted(task):
                _motion_runtime.finish(task_id)
                return
        _emit(task, build_phase(Phase.COMPOSE, scenes=total,
                                authored=authored,
                                templated=total - authored,
                                gate_failed_scenes=sorted(scene_gate_issues)))
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
            res = mv.concat_mp4s(
                ordered, silent_final,
                transitions=timeline_info['transitions'],
                abort_event=task.get('abort_event'))
        if not res.get('ok'):
            raise RuntimeError('concat failed: ' + res.get('detail', ''))
        _emit(task, build_phase(Phase.CONCAT,
                                duration_s=res.get('duration'),
                                mode=res.get('mode')))

        # ── 7. sidecar SRT (loose-adjusted timeline when narrated) ──
        sidecar = os.path.join(workdir, 'final.srt')
        cursor = scenes[0]['start']
        lines: list[str] = []
        for i, sc in enumerate(scenes, 1):
            dur = sc['_content_duration']
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
            _emit(task, build_phase(Phase.BURN_IN,
                                    duration_s=br.get('duration'),
                                    auto=bool(degraded_narration)))

        # ── 8. mux (optional) ──
        final_path = os.path.join(workdir, 'final.mp4')
        narration_wav = ''
        if narration:
            _phase_started(task, phases, 'mux')
            wavs = [e['wav'] for e in manifest['scenes'] if e.get('wav')]
            narration_wav = os.path.join(workdir, 'audio', 'narration.wav')
            with heartbeat(task, lambda t, ev: _emit(t, ev), 'mux'):
                cn = mv.concat_narrations(wavs, narration_wav)
                if not cn.get('ok'):
                    raise RuntimeError('narration concat failed: '
                                       + cn.get('detail', ''))
                if audio_plan:
                    mx = mv.mix_audio_timeline(
                        video_final, narration_wav, audio_plan, final_path,
                        abort_event=task.get('abort_event'))
                else:
                    mx = mv.mux_audio_video(
                        video_final, narration_wav, final_path,
                        abort_event=task.get('abort_event'))
            if not mx.get('ok'):
                raise RuntimeError('mux failed: ' + mx.get('detail', ''))
            _emit(task, build_phase(Phase.MUX,
                                    duration_s=mx.get('duration')))
        elif audio_plan:
            _phase_started(task, phases, 'mux')
            with heartbeat(task, lambda t, ev: _emit(t, ev), 'mux'):
                mx = mv.mix_audio_timeline(
                    video_final, '', audio_plan, final_path,
                    abort_event=task.get('abort_event'))
            if not mx.get('ok'):
                raise RuntimeError('audio mix failed: '
                                   + mx.get('detail', ''))
            _emit(task, build_phase(Phase.MUX,
                                    duration_s=mx.get('duration')))
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
        try:
            from lib.motion_video._asset_preflight import (
                collect_media_attribution,
            )
            media_attribution = collect_media_attribution(workdir)
        except Exception as e:
            logger.warning('[MotionVideo] media attribution failed: %s', e,
                           exc_info=True)
            media_attribution = {
                'records': 0, 'json_path': '', 'text_path': ''}
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
            'creative_mode': task.get('creative_mode') or 'standard',
            'director': task.get('director') or {},
            'gate_failed_scenes': sorted(scene_gate_issues),
            'scene_quality': scene_records,
            'quality_summary': quality_summary,
            'timeline': timeline_info,
            'audio': audio_plan_summary(audio_plan),
            'audio_plan_path': (os.path.join(workdir, 'audio_plan.json')
                                if audio_plan else ''),
            'audio_attribution_path': (
                os.path.join(workdir, 'audio_attribution.txt')
                if audio_plan else ''),
            'media_attribution': media_attribution,
            'media_attribution_path': media_attribution.get('text_path') or '',
        }
        task['result'] = result
        # Computed BEFORE the manifest write, not after: job.json is the ONLY
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
        user_id = int(m['user_id'])
        if user_id < 1:
            raise ValueError('motion manifest has no valid owner')
        task = _new_motion_task(
            task_id, srt_path=m.get('srt_path') or '', workdir=workdir,
            voice=m.get('voice') or '', speed=m.get('speed'),
            alignment=m.get('alignment') or 'loose',
            narration=bool(m.get('narration', True)),
            quality=m.get('quality') or 'standard',
            parallel=int(m.get('parallel') or 2),
            width=int(m.get('width') or 1080),
            height=int(m.get('height') or 1440),
            scenes_path=m.get('scenes_path') or '', user_id=user_id)
        for k in ('burn_in', 'burn_in_fontsdir', 'topic', 'lang',
                  'max_scenes', 'paper_hash', 'kind', 'scene_author',
                  'author_token_budget', 'creative_mode', 'director', 'model',
                  'qa_model', 'audio_plan',
                  'audio_plan_path', 'audio_base_dir'):
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
        _emit(task, build_phase(Phase.REGEN,
                                scene_id=scene_id,
                                regen_of=task.get('regen_of')))
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
        from lib.motion_video._timeline import transition_plan
        transitions = (transition_plan(scenes)
                       if all(sc.get('timeline_contract_version')
                              for sc in scenes) else None)
        with heartbeat(task, lambda t, ev: _emit(t, ev), 'regen'):
            res = mv.concat_mp4s(
                ordered, silent_final, transitions=transitions,
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
        audio_plan = None
        audio_plan_path = os.path.join(workdir, 'audio_plan.json')
        if os.path.isfile(audio_plan_path):
            from lib.json_store import read_json
            audio_plan = read_json(audio_plan_path, default=None)
        narration_wav = (os.path.join(workdir, 'audio', 'narration.wav')
                         if task.get('narration') else '')
        if audio_plan:
            with heartbeat(task, lambda t, ev: _emit(t, ev), 'regen'):
                mx = mv.mix_audio_timeline(
                    video_final, narration_wav, audio_plan, final_path,
                    abort_event=task.get('abort_event'))
            if not mx.get('ok'):
                raise RuntimeError('re-mix failed: ' + mx.get('detail', ''))
        elif task.get('narration'):
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
