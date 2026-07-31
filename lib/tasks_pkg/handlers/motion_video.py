"""lib/tasks_pkg/handlers/motion_video.py — Motion-video tool handlers.

Thin dispatch layer over :mod:`lib.motion_video`: path resolution
(project-relative or absolute), abort wiring (the task's ``abort_event``
reaches the render/concat subprocess wrappers), and tool-round
finalization with plain-text badges (per CLAUDE.md §3.4).
"""

from __future__ import annotations

import json
import os

from lib.log import get_logger

logger = get_logger(__name__)

from lib.tasks_pkg.executor import _build_simple_meta, _finalize_tool_round
from lib.tasks_pkg.executor import tool_registry
from lib.tools.motion_video import MOTION_VIDEO_TOOL_NAMES
from lib.tools.produce import (PRODUCE_REPORT_TOOL_NAME,
                               PRODUCE_RESEARCH_TOOL_NAME,
                               PRODUCE_VIDEO_TOOL_NAME)


def _resolve(path: str, project_path: str | None) -> str:
    """Resolve a tool path arg: absolute stays, relative joins the project."""
    path = (path or '').strip()
    if not path:
        return ''
    if os.path.isabs(path):
        return path
    if project_path:
        return os.path.join(project_path, path)
    return os.path.abspath(path)


def _fmt(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=1)


@tool_registry.tool_set(
    MOTION_VIDEO_TOOL_NAMES,
    category='video',
    description='Motion video (MG animation) generation pipeline')
def _handle_motion_video_tool(task, tc, fn_name, tc_id, fn_args, rn,
                              round_entry, cfg, project_path,
                              project_enabled, all_tools=None):
    """Handle the motion_video_* tool family."""
    from lib import motion_video as mv

    abort_event = task.get('abort_event') if isinstance(task, dict) else None
    proj = project_path if project_enabled else None
    badge = 'ok'

    try:
        if fn_name == 'motion_video_env_check':
            install = fn_args.get('install', True)
            if install:
                mv.ensure_hyperframes(install=True)
                mv.ensure_ffmpeg(install=True)
                mv.ensure_ffprobe(install=True)
            result = mv.probe_env()
            badge = 'ready' if result.get('ok') else 'env-missing'

        elif fn_name == 'motion_video_storyboard_check':
            srt_path = _resolve(fn_args.get('srt_path', ''), proj)
            scenes_path = _resolve(fn_args.get('scenes_path', ''), proj)
            tol = float(fn_args.get('tolerance') or 0.1)
            try:
                with open(srt_path, encoding='utf-8') as f:
                    entries = mv.parse_srt(f.read())
                with open(scenes_path, encoding='utf-8') as f:
                    scenes = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                logger.debug('handle motion video tool: unreadable/malformed JSON (%s)', e)
                result = {'ok': False, 'errors': [f'cannot read inputs: {e}']}
                badge = 'failed'
            else:
                if not entries:
                    result = {'ok': False,
                              'errors': ['SRT parsed to zero cues']}
                    badge = 'failed'
                else:
                    errors = mv.check_storyboard(scenes, mv.total_span(entries),
                                                 tol=tol)
                    result = {'ok': not errors, 'errors': errors,
                              'span_s': [round(s, 3) for s in mv.total_span(entries)]}
                    badge = 'ok' if not errors else f'{len(errors)} errors'

        elif fn_name == 'motion_video_check':
            project_dir = _resolve(fn_args.get('project_dir', ''), proj)
            result = mv.check_project(project_dir, abort_event=abort_event)
            badge = 'ok' if result.get('ok') else (result.get('category') or 'failed')

        elif fn_name == 'motion_video_render':
            project_dir = _resolve(fn_args.get('project_dir', ''), proj)
            output = _resolve(fn_args.get('output', ''), proj)
            quality = fn_args.get('quality') or 'standard'
            fps = fn_args.get('fps')
            timeout = int(fn_args.get('timeout') or 1800)
            result = mv.render_project(project_dir, output, quality=quality,
                                       fps=fps, timeout=timeout,
                                       abort_event=abort_event)
            badge = ('rendered' if result.get('ok')
                     else (result.get('category') or 'failed'))

        elif fn_name == 'motion_video_probe':
            path = _resolve(fn_args.get('path', ''), proj)
            info = mv.probe_video(path)
            result = {'ok': info is not None, 'path': path,
                      'probe': info or {}}
            badge = 'ok' if info is not None else 'failed'

        elif fn_name == 'motion_video_concat':
            inputs = [_resolve(p, proj) for p in (fn_args.get('inputs') or [])]
            output = _resolve(fn_args.get('output', ''), proj)
            timeout = int(fn_args.get('timeout') or 1800)
            result = mv.concat_mp4s(inputs, output, timeout=timeout,
                                    abort_event=abort_event)
            badge = ('concatenated' if result.get('ok')
                     else (result.get('category') or 'failed'))

        elif fn_name == 'motion_video_narrate':
            scenes_path = _resolve(fn_args.get('scenes_path', ''), proj)
            out_dir = _resolve(fn_args.get('out_dir', ''), proj)
            try:
                with open(scenes_path, encoding='utf-8') as f:
                    scenes = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                logger.debug('handle motion video tool: unreadable/malformed JSON (%s)', e)
                result = {'ok': False, 'detail': f'cannot read scenes: {e}'}
                badge = 'failed'
            else:
                try:
                    result = mv.synthesize_scene_narrations(
                        scenes, out_dir,
                        voice=fn_args.get('voice') or None,
                        speed=fn_args.get('speed'),
                        alignment=fn_args.get('alignment') or 'loose',
                        abort_event=abort_event)
                    if result.get('ok'):
                        badge = ('degraded' if result.get('degraded')
                                 else f"{len(result.get('scenes', []))} scenes")
                    else:
                        badge = ('degraded' if result.get('degraded')
                                 else 'failed')
                except mv.NarrationAborted as _e:
                    logger.debug('handle motion video tool: NarrationAborted (%s)', _e)
                    result = {'ok': False, 'category': 'aborted',
                              'detail': 'narration aborted by user'}
                    badge = 'aborted'

        elif fn_name == 'motion_video_mux':
            video = _resolve(fn_args.get('video', ''), proj)
            audio = _resolve(fn_args.get('audio', ''), proj)
            output = _resolve(fn_args.get('output', ''), proj)
            loudnorm = fn_args.get('loudnorm', True)
            result = mv.mux_audio_video(video, audio, output,
                                        loudnorm=bool(loudnorm),
                                        abort_event=abort_event)
            badge = ('muxed' if result.get('ok')
                     else (result.get('category') or 'failed'))

        else:
            result = {'ok': False, 'errors': [f'unknown tool {fn_name}']}
            badge = 'failed'

    except Exception as e:
        logger.error('[MotionVideo] %s failed: %s', fn_name, e, exc_info=True)
        result = {'ok': False, 'category': 'unknown', 'detail': str(e)}
        badge = 'failed'

    tool_content = _fmt(result)
    meta = _build_simple_meta(
        fn_name, tool_content,
        source='MotionVideo',
        title=fn_name.replace('motion_video_', ''),
        snippet=tool_content.split('\n', 1)[0][:120] if tool_content else '',
        badge=badge,
    )
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, tool_content, False


@tool_registry.tool_set(
    {PRODUCE_VIDEO_TOOL_NAME},
    category='video',
    description='High-level topic → finished video')
def _handle_produce_video(task, tc, fn_name, tc_id, fn_args, rn,
                          round_entry, cfg, project_path,
                          project_enabled, all_tools=None):
    """Handle produce_video: kick off a background topic→video job.

    Not project-gated (owner 拍板 #2). Spawns a motion task whose front half is
    the research→script→timeline recipe, then returns a task_id immediately —
    the render runs in the background and the user watches the video panel.
    """
    import os as _os

    topic = str(fn_args.get('topic') or '').strip()
    if not topic:
        result = {'ok': False, 'detail': 'topic is required'}
        badge = 'failed'
    else:
        try:
            from lib.motion_video._env import motion_root
            from lib.motion_video.engine import run_topic_motion_task
            from lib.motion_video.runtime import (
                _motion_task_id, _new_motion_task, _motion_runtime)

            _ASPECTS = {'1080x1440': (1080, 1440), '1080x1920': (1080, 1920),
                        '1920x1080': (1920, 1080), '1080x1080': (1080, 1080)}
            aspect = str(fn_args.get('aspect') or '1080x1440').strip()
            width, height = _ASPECTS.get(aspect, (1080, 1440))
            lang = 'en' if str(fn_args.get('lang') or 'zh').strip() == 'en' else 'zh'
            try:
                max_scenes = int(fn_args.get('max_scenes') or 8)
            except (TypeError, ValueError) as _e:
                logger.debug('handle produce video: unexpected type/unparseable (%s)', _e)
                max_scenes = 8
            max_scenes = max(3, min(max_scenes, 12))
            narration = bool(fn_args.get('narration', True))

            job_id = _motion_task_id()
            workdir = _os.path.join(motion_root(), 'jobs', job_id)
            _os.makedirs(workdir, exist_ok=True)
            job = _new_motion_task(
                job_id, srt_path='', workdir=workdir, voice='', speed=None,
                alignment='loose', narration=narration, quality='standard',
                parallel=2, width=width, height=height, scenes_path='')
            job['topic'] = topic
            job['lang'] = lang
            job['max_scenes'] = max_scenes
            job['kind'] = 'topic'
            # 默认精品 (owner 2026-07-27): the template path cannot pass the
            # renderer's own lint, so it must not be what a user gets by
            # default. 'template' stays reachable for explicit speed-over-looks.
            visual = str(fn_args.get('visual_quality') or 'authored').strip()
            job['scene_author'] = (visual != 'template')
            _motion_runtime.spawn(job_id, run_topic_motion_task, job)
            logger.info('[Produce] video job %s started topic=%r lang=%s '
                        'visual=%s', job_id, topic[:60], lang, visual)
            # 画质选择面 (owner 2026-07-26): the user cannot perceive the
            # orchestration, but they MUST be able to perceive which quality
            # tier was used and how to switch. Measured cost: authored ≈ 2×
            # time + ~40k extra tokens for an 8-scene film.
            if lang == 'zh':
                quality_hint = (
                    '本次为精品画质(每镜定制构图,约 2× 耗时)。'
                    if visual != 'template' else
                    '本次为标准画质(模板构图,单行文字卡),仅在你要求提速时使用。')
            else:
                quality_hint = (
                    'This run uses boutique quality (per-scene bespoke '
                    'composition, ~2× time).' if visual != 'template' else
                    'This run uses the fast template card (a single centred '
                    'line) — speed over looks, as requested.')
            result = {'ok': True, 'task_id': job_id, 'topic': topic,
                      'lang': lang, 'aspect': aspect,
                      'visual_quality': visual,
                      'quality_hint': quality_hint,
                      'poll': f'/api/v1/motion/videos/poll/{job_id}',
                      'note': 'Video is generating in the background; watch the '
                              'video panel for progress. ' + quality_hint}
            badge = 'started'
        except Exception as e:
            logger.error('[Produce] failed to start video job: %s', e, exc_info=True)
            result = {'ok': False, 'detail': str(e)}
            badge = 'failed'

    tool_content = _fmt(result)
    meta = _build_simple_meta(
        fn_name, tool_content, source='Produce', title='video',
        snippet=tool_content.split('\n', 1)[0][:120] if tool_content else '',
        badge=badge)
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, tool_content, False


@tool_registry.tool_set(
    {PRODUCE_REPORT_TOOL_NAME},
    category='video',
    description='High-level topic → long-form research report')
def _handle_produce_report(task, tc, fn_name, tc_id, fn_args, rn,
                           round_entry, cfg, project_path,
                           project_enabled, all_tools=None):
    """Handle produce_report: kick off a background topic→report job.

    The whole capability rides the substrate: the recipe owns research →
    outline → sections → assemble, ``lib.production.stages`` owns the
    checkpointed resume, and the generic ``/api/v1/tasks/*`` endpoints own
    poll / stream / abort (no bespoke routes were needed for this capability).
    """
    topic = str(fn_args.get('topic') or '').strip()
    if not topic:
        result = {'ok': False, 'detail': 'topic is required'}
        badge = 'failed'
    else:
        try:
            from lib.longform.engine import start_report_job

            lang = 'en' if str(fn_args.get('lang') or 'zh').strip() == 'en' else 'zh'
            depth = str(fn_args.get('depth') or 'standard').strip()
            if depth not in ('brief', 'standard', 'deep'):
                depth = 'standard'
            conv_id = ''
            if isinstance(task, dict):
                conv_id = task.get('conv_id') or task.get('convId') or ''
            started = start_report_job(topic, lang=lang, depth=depth,
                                       conv_id=conv_id)
            tid = started['task_id']
            result = {'ok': True, 'task_id': tid, 'topic': topic,
                      'lang': lang, 'depth': depth,
                      'deduped': started.get('deduped', False),
                      'poll': f'/api/v1/tasks/{tid}',
                      'note': 'Report is generating in the background; it will '
                              'be published as a markdown artifact.'}
            badge = 'joined' if started.get('deduped') else 'started'
        except Exception as e:
            logger.error('[Produce] failed to start report job: %s', e,
                         exc_info=True)
            result = {'ok': False, 'detail': str(e)}
            badge = 'failed'

    tool_content = _fmt(result)
    meta = _build_simple_meta(
        fn_name, tool_content, source='Produce', title='report',
        snippet=tool_content.split('\n', 1)[0][:120] if tool_content else '',
        badge=badge)
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, tool_content, False


@tool_registry.tool_set(
    {PRODUCE_RESEARCH_TOOL_NAME},
    category='video',
    description='High-level direction → scored novel research ideas')
def _handle_produce_research(task, tc, fn_name, tc_id, fn_args, rn,
                             round_entry, cfg, project_path,
                             project_enabled, all_tools=None):
    """Handle produce_research: kick off a background direction→ideas job.

    Same substrate posture as produce_report: the recipe owns harvest →
    survey → ideate, ``lib.production.stages`` owns the checkpointed resume,
    and the generic ``/api/v1/tasks/*`` endpoints own poll / stream / abort.
    """
    direction = str(fn_args.get('direction') or '').strip()
    if not direction:
        result = {'ok': False, 'detail': 'direction is required'}
        badge = 'failed'
    else:
        try:
            from lib.research import produce_research

            lang = 'zh' if str(fn_args.get('lang') or 'en').strip() == 'zh' else 'en'
            try:
                n_ideas = int(fn_args.get('n_ideas') or 6)
            except (TypeError, ValueError) as _e:
                logger.debug('handle produce research: unexpected type/unparseable (%s)', _e)
                n_ideas = 6
            n_ideas = max(3, min(n_ideas, 12))
            seeds = fn_args.get('seed_arxiv_ids') or None
            if seeds is not None and not isinstance(seeds, list):
                seeds = None
            conv_id = ''
            if isinstance(task, dict):
                conv_id = task.get('conv_id') or task.get('convId') or ''
            started = produce_research(direction, lang=lang, n_ideas=n_ideas,
                                       conv_id=conv_id, seed_arxiv_ids=seeds)
            tid = started['task_id']
            result = {'ok': True, 'task_id': tid, 'direction': direction,
                      'lang': lang, 'n_ideas': n_ideas,
                      'deduped': started.get('deduped', False),
                      'poll': f'/api/v1/tasks/{tid}',
                      'note': 'Research is running in the background: the '
                              'literature corpus is harvested and surveyed '
                              'first, then ideas are scored against it.'}
            badge = 'joined' if started.get('deduped') else 'started'
        except Exception as e:
            logger.error('[Produce] failed to start research job: %s', e,
                         exc_info=True)
            result = {'ok': False, 'detail': str(e)}
            badge = 'failed'

    tool_content = _fmt(result)
    meta = _build_simple_meta(
        fn_name, tool_content, source='Produce', title='research',
        snippet=tool_content.split('\n', 1)[0][:120] if tool_content else '',
        badge=badge)
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, tool_content, False
