"""Fixed film workflow, from storyboard admission through validated publication.

Entry points: ``film_workflow`` declares nodes; ``run_film_workflow`` enters the
shared production executor. Engine helpers own media adapters and scene gates;
TaskRuntime/job.json remain the sole job lifecycle owner. Nodes pass finite JSON
artifacts, with binary media referenced by path, never copied into checkpoints.

Nodes re-enter domain verification on recovery. Existing storyboard, human,
TTS, composition and render checkpoints own expensive-work reuse; a stage-done
receipt alone cannot prove mutable media remains valid. No second model loop,
worker pool, task state machine or automatic publication retry is introduced.
"""
from __future__ import annotations

from concurrent.futures import as_completed
from copy import deepcopy
import json
import os
from pathlib import Path

from lib import motion_video as mv
from lib.agent_core.events import Phase, build_phase
from lib.motion_video import engine
from lib.motion_video.media_publication import (
    assemble_candidate, final_media_findings, inspect_candidate, publish_candidate,
)
from lib.production.concurrency import production_executor
from lib.production.heartbeat import heartbeat
from lib.production.stages import Stage, StageAborted
from lib.production.workflows import AgentToolWorkflow, WorkflowNode, run_workflow

logger = engine.logger


def _files_gate(*fields):
    def validate(ctx, artifact):
        return [f'{field}: output file is missing or empty' for field in fields
                if not Path(artifact[field]).is_file()
                or Path(artifact[field]).stat().st_size <= 0]
    return validate


def _verify_film(ctx: dict, artifact: dict) -> list:
    task = ctx['task']
    result, probe = artifact['result'], artifact['media']['probe']
    needs_audio = bool(result['narrated'] or ctx['artifacts']['timeline']['audio_plan'])
    return final_media_findings(
        probe, width=task['width'], height=task['height'], fps=ctx['fps'],
        duration=ctx['artifacts']['timeline']['timeline']['duration_s'],
        needs_audio=needs_audio, verify_spec=mv.verify_spec)


def _run_publish(ctx: dict) -> dict:
    verified = ctx['artifacts']['verify']
    final_path = os.path.join(ctx['workdir'], 'final.mp4')
    media = publish_candidate(verified['media'], final_path,
                              abort_check=lambda: engine._aborted(ctx['task']))
    return {'result': {**verified['result'], 'final_path': final_path},
            'quality': verified['quality'], 'media': media}


def _object(**properties):
    return {'type': 'object', 'properties': properties,
            'required': list(properties), 'additionalProperties': False}


_PATH = {'type': 'string', 'minLength': 1}
_BOOL = {'type': 'boolean'}
_COUNT = {'type': 'integer', 'minimum': 0}
_RECORD = {'type': 'object'}
_RECORDS = {'type': 'array', 'items': _RECORD}
_SCENES = {'type': 'array', 'minItems': 1, 'items': {
    'type': 'object', 'required': ['id', 'start', 'end'],
    'properties': {'id': _PATH, 'start': {'type': 'number'},
                   'end': {'type': 'number'}}}}
_DELIVERY = _object(
    result={'type': 'object', 'required': ['final_path', 'srt_path', 'duration',
                                         'scenes', 'narrated', 'quality_summary'],
            'properties': {'final_path': _PATH, 'srt_path': _PATH,
                           'duration': {'type': 'number', 'exclusiveMinimum': 0},
                           'scenes': {'type': 'integer', 'minimum': 1},
                           'narrated': _BOOL, 'quality_summary': _RECORD}},
    quality=_object(degraded=_BOOL, reason={'type': 'string'}),
    media=_object(path=_PATH, bytes={'type': 'integer', 'minimum': 1},
                  sha256={'type': 'string', 'pattern': '^[a-f0-9]{64}$'},
                  probe={'type': ['object', 'null']}))


def film_workflow(task: dict) -> AgentToolWorkflow:
    """Declare all film boundaries; expensive reuse stays in its domain owner.

    Non-resumable means re-enter the gate/cache-aware callback, not regenerate
    valid model or media work. These receipts must not bypass a changed file,
    a human decision, fresh cancellation or the final publication check.
    """
    from lib.motion_video._scene_author import scene_author_enabled
    nodes = [
        ('storyboard', _run_storyboard, 'model' if task.get('topic') else 'deterministic',
         _object(scenes=_SCENES, span={'type': 'array', 'minItems': 2, 'maxItems': 2,
                                     'items': {'type': 'number'}},
                 topic={'type': 'string'}), None),
        ('review', _run_review, 'human' if task.get('interactive_storyboard') else 'deterministic',
         _object(scenes=_SCENES), None),
        ('narration', _run_narration, 'model' if task.get('narration') else 'deterministic',
         _object(enabled=_BOOL, degraded=_BOOL, manifest=_RECORD), None),
        ('timeline', _run_timeline, 'deterministic',
         _object(scenes=_SCENES, timeline=_RECORD, audio_plan={'type': ['object', 'null']}), None),
        ('compose', _run_compose, 'model' if scene_author_enabled(task) else 'deterministic',
         _object(scenes=_SCENES, scene_dirs={'type': 'array', 'items': _PATH},
                 authored=_COUNT, authoring=_BOOL, scene_gate_issues=_RECORD,
                 scene_records=_RECORDS), None),
        ('render', _run_render, 'deterministic',
         _object(mp4s={'type': 'object', 'minProperties': 1, 'additionalProperties': _PATH}), None),
        ('concat', _run_concat, 'deterministic',
         _object(video_path=_PATH), _files_gate('video_path')),
        ('subtitles', _run_subtitles, 'deterministic',
         _object(video_path=_PATH, srt_path=_PATH, burn_in=_BOOL),
         _files_gate('video_path', 'srt_path')),
        ('mux', _run_mux, 'deterministic',
         _object(candidate_path=_PATH), _files_gate('candidate_path')),
        ('verify', _run_verify, 'deterministic', _DELIVERY, _verify_film),
        ('publish', _run_publish, 'deterministic', _DELIVERY, None),
    ]
    return AgentToolWorkflow('motion-video-film', '1', tuple(
        WorkflowNode(Stage(name, callback, gate=gate, retry=0,
                           resumable=False, checkpoint_version='1'), execution, schema)
        for name, callback, execution, schema, gate in nodes))


def run_film_workflow(task: dict, *, owner_user_id: int) -> dict:
    ctx = {'task': task, 'workdir': task['workdir'], 'owner_user_id': owner_user_id,
           'fps': 30, 'phases': engine._plan_phases(task)}
    return run_workflow(
        film_workflow(task), ctx,
        state_path=os.path.join(task['workdir'], 'film_state.json'),
        emit=lambda event: engine._emit(task, event),
        abort_check=lambda: engine._aborted(task),
        abort_exceptions=(mv.NarrationAborted,))

def _run_storyboard(ctx: dict) -> dict:
    task = ctx['task']
    workdir = ctx['workdir']
    phases = ctx['phases']
    owner_user_id = ctx['owner_user_id']
    # ── 0. topic front-half (research → script → timeline) ──
    # When the job carries a bare TOPIC (no SRT / scenes), run the recipe
    # to synthesize scenes.json first. The recipe is itself checkpointed
    # (pipeline_state.json), so a crash mid-research resumes there.
    scenes_path = task.get('scenes_path') or ''
    topic = (task.get('topic') or '').strip()
    if topic and not (scenes_path and os.path.isfile(scenes_path)) \
            and not (task.get('srt_path') and os.path.isfile(task['srt_path'])):
        from lib.motion_video._recipe import build_scenes_from_topic
        engine._phase_started(task, phases, 'research')
        engine._emit(task, build_phase(Phase.RESEARCH, topic=topic))
        with heartbeat(task, lambda t, ev: engine._emit(t, ev), 'research'):
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
                emit=lambda ev: engine._emit(task, {'type': 'recipe', **ev}))
        scenes_path = tl['scenes_path']
        task['scenes_path'] = scenes_path
        task['creative_mode'] = tl.get('creative_mode') or 'director'
        task['director'] = tl.get('director') or {}
        engine._emit(task, build_phase(Phase.SCRIPT_DONE,
                                scenes=tl['scenes'],
                                timed_from_audio=tl['timed_from_audio'],
                                creative_mode=tl.get('creative_mode'),
                                director=tl.get('director') or {}))
    if engine._aborted(task):
        raise StageAborted('motion-video cancelled')

    # ── 1. parse (optional when scenes are supplied directly) ──
    entries = []
    span = (0.0, 0.0)
    scenes_path = task.get('scenes_path') or ''
    if task.get('srt_path') and os.path.isfile(task['srt_path']):
        engine._phase_started(task, phases, 'parse')
        with open(task['srt_path'], encoding='utf-8') as f:
            entries = mv.parse_srt(f.read())
        if not entries:
            raise ValueError('SRT parsed to zero cues')
        span = mv.total_span(entries)
        engine._emit(task, build_phase(Phase.PARSE,
                                cues=len(entries),
                                span_s=[round(s, 3) for s in span]))
    elif not (scenes_path and os.path.isfile(scenes_path)):
        raise ValueError('neither srt_path nor scenes_path available')
    if engine._aborted(task):
        raise StageAborted('motion-video cancelled')

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
        engine._emit(task, {'type': 'shot_plan_findings',
                     'findings': plan_findings})
    engine._write(os.path.join(workdir, 'scenes.json'),
           json.dumps(scenes, ensure_ascii=False, indent=1))
    engine._phase_started(task, phases, 'storyboard')
    engine._emit(task, build_phase(Phase.STORYBOARD, scenes=len(scenes)))
    return {'scenes': scenes, 'span': list(span), 'topic': topic}


def _run_review(ctx: dict) -> dict:
    task = ctx['task']
    workdir = ctx['workdir']
    storyboard = ctx['artifacts']['storyboard']
    scenes = deepcopy(storyboard['scenes'])
    span = storyboard['span']
    # ── 2b. interactive storyboard review (C4; chat-initiated jobs) ──
    scenes = engine._interactive_storyboard_gate(task, workdir, scenes, span)
    if scenes is None:
        raise StageAborted('motion-video cancelled')
    if scenes is not None:
        engine._write(os.path.join(workdir, 'scenes.json'),
               json.dumps(scenes, ensure_ascii=False, indent=1))
    return {'scenes': scenes}


def _run_narration(ctx: dict) -> dict:
    task = ctx['task']
    workdir = ctx['workdir']
    phases = ctx['phases']
    owner_user_id = ctx['owner_user_id']
    scenes = ctx['artifacts']['review']['scenes']
    # ── 3. narration (optional) ──
    narration = bool(task.get('narration'))
    degraded_narration = False
    manifest: dict = {}
    if narration:
        audio_dir = os.path.join(workdir, 'audio')
        # Reuse a manifest already produced by the recipe timeline stage
        # (topic jobs synthesize TTS up-front to measure durations) so we
        # never double-synthesize, and a resumed job skips narration too.
        manifest = engine._reusable_manifest(
            audio_dir, scenes, voice=task.get('voice') or None,
            speed=task.get('speed'),
            alignment=task.get('alignment') or 'loose') or {}
        if manifest.get('ok'):
            engine._emit(task, build_phase(
                Phase.NARRATE, degraded=False, reused=True,
                scenes=[{'scene_id': e['scene_id'],
                         'audio_s': e['audio_duration'],
                         'target_s': e['target_duration'],
                         'overflow_s': e['overflow']}
                        for e in manifest['scenes']]))
        else:
            engine._phase_started(task, phases, 'narrate')
            try:
                with heartbeat(task, lambda t, ev: engine._emit(t, ev), 'narrate'):
                    manifest = mv.synthesize_scene_narrations(
                        [s for s in scenes if s.get('spoken', True)],
                        audio_dir, voice=task.get('voice') or None,
                        speed=task.get('speed'),
                        alignment=task.get('alignment') or 'loose',
                        owner_user_id=owner_user_id,
                        abort_event=task.get('abort_event'),
                        on_scene_done=lambda i, n, sid: engine._emit(task, {
                            'type': 'progress', 'phase': 'narrate',
                            'done': i, 'total': n, 'unit': 'scene',
                            'scene_id': sid}))
            except mv.NarrationAborted:
                raise StageAborted('motion-video cancelled')
            if not manifest.get('ok'):
                narration = False
                degraded_narration = True
                engine._emit(task, build_phase(
                    Phase.NARRATE, degraded=True,
                    detail=manifest.get('detail', '')))
                logger.warning('[MotionVideo] narration degraded: %s',
                               manifest.get('detail'))
            else:
                from lib.json_store import write_json_atomic
                write_json_atomic(
                    os.path.join(audio_dir, 'manifest.json'), manifest)
                engine._emit(task, build_phase(
                    Phase.NARRATE, degraded=False,
                    scenes=[
                        {'scene_id': e['scene_id'],
                         'audio_s': e['audio_duration'],
                         'target_s': e['target_duration'],
                         'overflow_s': e['overflow']}
                        for e in manifest['scenes']]))
    if engine._aborted(task):
        raise StageAborted('motion-video cancelled')
    return {'enabled': narration, 'degraded': degraded_narration, 'manifest': manifest}


def _run_timeline(ctx: dict) -> dict:
    task = ctx['task']
    workdir = ctx['workdir']
    fps = ctx['fps']
    scenes = deepcopy(ctx['artifacts']['review']['scenes'])
    manifest = ctx['artifacts']['narration']['manifest']
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
    engine._write(os.path.join(workdir, 'scenes.json'),
           json.dumps(scenes, ensure_ascii=False, indent=1))
    engine._emit(task, {'type': 'timeline_contract', **timeline_info})

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
        engine._emit(task, {'type': 'audio_plan',
                     **audio_plan_summary(audio_plan)})
    return {'scenes': scenes, 'timeline': timeline_info, 'audio_plan': audio_plan}


def _run_compose(ctx: dict) -> dict:
    task = ctx['task']
    workdir = ctx['workdir']
    phases = ctx['phases']
    width, height = task['width'], task['height']
    topic = ctx['artifacts']['storyboard']['topic']
    scenes = deepcopy(ctx['artifacts']['timeline']['scenes'])
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
    engine._phase_started(task, phases, 'compose')
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
    # the caller thread. Only missing compositions start author work on demand;
    # completed HTML is committed immediately by the caller, so
    # full results are retained only by active futures (never all scenes).
    prepared_scenes: list[dict] = []
    author_jobs: list[dict] = []
    media_credit_providers: set[str] = set()
    for i, sc in enumerate(scenes, 1):
        if engine._aborted(task):
            raise StageAborted('motion-video cancelled')
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
        existing = engine._existing_composition(
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
        abort_check = lambda: engine._aborted(task)

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
            engine._emit(task, {
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
            engine._commit_scene_html(
                job['index_path'], html, sc, job['scene_dir'],
                width=width, height=height, duration=job['duration'],
                scene_index=job['index'], total_scenes=total,
                theme=theme)

        with heartbeat(task, lambda t, ev: engine._emit(t, ev), 'compose'):
            needs_craft = any(bool(job['scene'].get('allow_craft_browse'))
                              for job in author_jobs)
            if not prepare_parallel_author_dependencies(theme=theme, needs_craft=needs_craft):
                logger.info('[MotionVideo] optional author dependencies unavailable; '
                            'scene gates retain their existing fallback findings')
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

                batch_aborted = engine._run_scene_authors(
                    author_jobs,
                    run_author=_run_author,
                    accept_result=_accept_author_result,
                    abort_check=abort_check)
        if batch_aborted or engine._aborted(task):
            raise StageAborted('motion-video cancelled')

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
            # results were already committed by the joined author callback;
            # resume/template paths reach the same guard here.
            html = engine._commit_scene_html(
                index_path, html, sc, scene_dir, width=width,
                height=height, duration=dur, scene_index=i,
                total_scenes=total, theme=theme)
        # ── Visual QA round (design-system P2): a VLM looks at the
        # settled frame with the designer checklist; actionable findings
        # go back to the author for ONE repair round. Never blocks the
        # film: unavailable infra / no vision model → silent skip.
        if authoring and not engine._aborted(task):
            html = engine._visual_qa_round(
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
        gate_findings += engine._scene_gate_findings(
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
        gate_findings += engine._composition_contract_findings(html, scene_dir)
        if gate_findings:
            scene_gate_issues[sc['id']] = gate_findings
            engine._emit(task, {'type': 'scene_gate', 'scene_id': sc['id'],
                         'ok': False, 'findings': gate_findings[:6]})
        try:
            rec = scene_telemetry(sc, html, scene_dir, mode=scene_mode,
                                  fill=fill, rounds=author_rounds,
                                  tokens=author_tokens,
                                  gate_findings=gate_findings,
                                  craft_reads=author_craft_reads)
            scene_records.append(rec)
            engine._emit(task, {'type': 'scene_quality', **rec})
        except Exception as e:
            logger.warning('[MotionVideo] scene %s telemetry crashed: %s',
                           sc['id'], e, exc_info=True)
        sc['_duration'] = dur
        sc['_content_duration'] = content_dur
        scene_dirs.append(scene_dir)
        engine._emit(task, {'type': 'progress', 'phase': 'compose',
                     'done': i, 'total': total, 'unit': 'scene',
                     'scene_id': sc['id']})
        if engine._aborted(task):
            raise StageAborted('motion-video cancelled')
    engine._emit(task, build_phase(Phase.COMPOSE, scenes=total,
                            authored=authored,
                            templated=total - authored,
                            gate_failed_scenes=sorted(scene_gate_issues)))
    if engine._aborted(task):
        raise StageAborted('motion-video cancelled')
    return {'scenes': scenes, 'scene_dirs': scene_dirs, 'authored': authored,
            'authoring': authoring, 'scene_gate_issues': scene_gate_issues,
            'scene_records': scene_records}


def _run_render(ctx: dict) -> dict:
    task = ctx['task']
    phases = ctx['phases']
    width, height = task['width'], task['height']
    fps = ctx['fps']
    composition = ctx['artifacts']['compose']
    scenes = composition['scenes']
    total = len(scenes)
    scene_dirs = composition['scene_dirs']
    # ── 5. render (on-demand parallel work, joined before proceeding) ──
    engine._phase_started(task, phases, 'render')
    parallel = max(1, int(task.get('parallel') or 2))
    quality = task.get('quality') or 'standard'
    mp4s: dict[str, str] = {}
    failures: list[dict] = []
    with heartbeat(task, lambda t, ev: engine._emit(t, ev), 'render'), \
            production_executor(name='mv-render', idle_reuse_hint=parallel) as pool:
        futures = {}
        for sc, scene_dir in zip(scenes, scene_dirs):
            if engine._aborted(task):
                break
            mp4_path = os.path.join(scene_dir, f"{sc['id']}.mp4")
            if engine._scene_already_rendered(mv, mp4_path, width=width,
                                       height=height, fps=fps,
                                       expect_dur=sc['_duration']):
                mp4s[sc['id']] = mp4_path
                engine._emit(task, {'type': 'scene_done', 'scene_id': sc['id'],
                             'ok': True, 'resumed': True,
                             'done': len(mp4s), 'total': total})
                continue
            fut = pool.submit(
                engine._render_one, mv, scene_dir, mp4_path,
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
                engine._emit(task, {'type': 'scene_done', 'scene_id': sc['id'],
                             'ok': True, 'elapsed': r.get('elapsed'),
                             'done': len(mp4s), 'total': total})
            else:
                failures.append({'scene_id': sc['id'],
                                 'category': r.get('category'),
                                 'detail': (r.get('detail') or '')[:300]})
                engine._emit(task, {'type': 'scene_done', 'scene_id': sc['id'],
                             'ok': False,
                             'category': r.get('category')})
    if engine._aborted(task):
        raise StageAborted('motion-video cancelled')
    if failures:
        first = failures[0]
        raise RuntimeError(
            f"scene {first['scene_id']} render failed "
            f"({first['category']}): {first['detail']}")
    return {'mp4s': mp4s}


def _run_concat(ctx: dict) -> dict:
    task = ctx['task']
    workdir = ctx['workdir']
    phases = ctx['phases']
    composition = ctx['artifacts']['compose']
    scenes = composition['scenes']
    mp4s = ctx['artifacts']['render']['mp4s']
    timeline_info = ctx['artifacts']['timeline']['timeline']
    # ── 6. concat (silent) ──
    engine._phase_started(task, phases, 'concat')
    ordered = [mp4s[sc['id']] for sc in scenes]
    silent_final = os.path.join(workdir, 'final_silent.mp4')
    with heartbeat(task, lambda t, ev: engine._emit(t, ev), 'concat'):
        res = mv.concat_mp4s(
            ordered, silent_final,
            transitions=timeline_info['transitions'],
            abort_event=task.get('abort_event'))
    if not res.get('ok'):
        raise RuntimeError('concat failed: ' + res.get('detail', ''))
    engine._emit(task, build_phase(Phase.CONCAT,
                            duration_s=res.get('duration'),
                            mode=res.get('mode')))
    return {'video_path': silent_final}


def _run_subtitles(ctx: dict) -> dict:
    task = ctx['task']
    workdir = ctx['workdir']
    phases = ctx['phases']
    composition = ctx['artifacts']['compose']
    scenes = composition['scenes']
    silent_final = ctx['artifacts']['concat']['video_path']
    degraded_narration = ctx['artifacts']['narration']['degraded']
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
    engine._write(sidecar, '\n'.join(lines))

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
        engine._phase_started(task, phases, 'burn_in')
        burned = os.path.join(workdir, 'final_burned.mp4')
        with heartbeat(task, lambda t, ev: engine._emit(t, ev), 'burn_in'):
            br = mv.burn_in_subtitles(
                silent_final, sidecar, burned,
                fontsdir=task.get('burn_in_fontsdir') or '',
                abort_event=task.get('abort_event'))
        if not br.get('ok'):
            raise RuntimeError('burn-in failed: ' + br.get('detail', ''))
        video_final = burned
        engine._emit(task, build_phase(Phase.BURN_IN,
                                duration_s=br.get('duration'),
                                auto=bool(degraded_narration)))
    return {'video_path': video_final, 'srt_path': sidecar, 'burn_in': burn_in_eff}


def _run_mux(ctx: dict) -> dict:
    task = ctx['task']
    workdir = ctx['workdir']
    phases = ctx['phases']
    narration_artifact = ctx['artifacts']['narration']
    narration = narration_artifact['enabled']
    manifest = narration_artifact['manifest']
    timeline = ctx['artifacts']['timeline']
    audio_plan = timeline['audio_plan']
    video_final = ctx['artifacts']['subtitles']['video_path']
    # ── 8. mux (optional) ──
    final_path = os.path.join(workdir, 'final.pending.mp4')
    Path(final_path).unlink(missing_ok=True)
    narration_wav = ''
    if narration:
        engine._phase_started(task, phases, 'mux')
        wavs = [e['wav'] for e in manifest['scenes'] if e.get('wav')]
        narration_wav = os.path.join(workdir, 'audio', 'narration.wav')
        with heartbeat(task, lambda t, ev: engine._emit(t, ev), 'mux'):
            cn = mv.concat_narrations(wavs, narration_wav)
            if not cn.get('ok'):
                raise RuntimeError('narration concat failed: '
                                   + cn.get('detail', ''))
            mx = assemble_candidate(video_final, narration_wav, audio_plan,
                final_path, media=mv, abort_event=task.get('abort_event'))
        if not mx.get('ok'):
            raise RuntimeError('mux failed: ' + mx.get('detail', ''))
        engine._emit(task, build_phase(Phase.MUX,
                                duration_s=mx.get('duration')))
    elif audio_plan:
        engine._phase_started(task, phases, 'mux')
        with heartbeat(task, lambda t, ev: engine._emit(t, ev), 'mux'):
            mx = assemble_candidate(video_final, '', audio_plan, final_path,
                media=mv, abort_event=task.get('abort_event'))
        if not mx.get('ok'):
            raise RuntimeError('audio mix failed: '
                               + mx.get('detail', ''))
        engine._emit(task, build_phase(Phase.MUX,
                                duration_s=mx.get('duration')))
    else:
        assemble_candidate(video_final, '', None, final_path,
            media=mv, abort_event=task.get('abort_event'))
    return {'candidate_path': final_path}


def _run_verify(ctx: dict) -> dict:
    task = ctx['task']
    workdir = ctx['workdir']
    composition = ctx['artifacts']['compose']
    scenes = composition['scenes']
    total = len(scenes)
    narration_artifact = ctx['artifacts']['narration']
    narration = narration_artifact['enabled']
    timeline = ctx['artifacts']['timeline']
    timeline_info = timeline['timeline']
    audio_plan = timeline['audio_plan']
    from lib.motion_video._audio_cues import audio_plan_summary
    final_path = ctx['artifacts']['mux']['candidate_path']
    scene_records = composition['scene_records']
    scene_gate_issues = composition['scene_gate_issues']
    authoring = composition['authoring']
    authored = composition['authored']
    degraded_narration = narration_artifact['degraded']
    sidecar = ctx['artifacts']['subtitles']['srt_path']
    burn_in_eff = ctx['artifacts']['subtitles']['burn_in']
    media = inspect_candidate(final_path, probe_video=mv.probe_video)
    probe = media['probe']
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
    _quality = engine._quality_verdict(
        degraded_narration=degraded_narration,
        scene_gate_issues=scene_gate_issues,
        authoring=bool(authoring), authored=authored, total=total,
        scene_records=scene_records)
    return {'result': result, 'quality': _quality, 'media': media}
