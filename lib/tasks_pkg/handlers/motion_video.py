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
from lib.tasks_pkg.manager._registry import task_user_id
from lib.tools.motion_video import MOTION_VIDEO_TOOL_NAMES
from lib.tools.produce import (EDIT_SLIDES_TOOL_NAME, EDIT_VIDEO_TOOL_NAME,
                               PATCH_SLIDES_TOOL_NAME, RESTYLE_SLIDES_TOOL_NAME,
                               REVERT_SLIDES_TOOL_NAME, REVERT_VIDEO_TOOL_NAME,
                               PRODUCE_REPORT_TOOL_NAME,
                               PRODUCE_RESEARCH_TOOL_NAME,
                               PRODUCE_SLIDES_TOOL_NAME,
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


#: Runtime ``kind`` for each background producer, bound in ONE place. The
#: literals are pinned against the runtime definitions by
#: tests/motion_video/test_motion_video_p4.py (drift = red), so they double as the
#: public work-unit kind vocabulary (docs/contracts/API_CONTRACT.md §2.18).
_PRODUCE_WORK_UNIT_KINDS = {
    PRODUCE_VIDEO_TOOL_NAME: 'motion-video',
    PRODUCE_REPORT_TOOL_NAME: 'longform-report',
    PRODUCE_SLIDES_TOOL_NAME: 'slides-deck',
    PRODUCE_RESEARCH_TOOL_NAME: 'research',
    EDIT_VIDEO_TOOL_NAME: 'motion-video',
    REVERT_VIDEO_TOOL_NAME: 'motion-video',
}


def _work_unit_ref(result: dict, fn_name: str) -> dict | None:
    """meta extra carrying the durable work-unit handle for the rich card.

    The card re-attaches to ``/api/v4/tasks/{id}`` by this handle instead of
    re-parsing the tool payload; absent when no job was accepted.
    """
    if not isinstance(result, dict) or result.get('ok') is not True:
        return None
    task_id = str(result.get('task_id') or '')[:80]
    kind = _PRODUCE_WORK_UNIT_KINDS.get(fn_name)
    if not task_id or not kind:
        return None
    return {'workUnitRef': {'taskId': task_id, 'kind': kind}}


def _mark_background_task_accepted(task: dict, fn_name: str,
                                   result: dict, round_num: int) -> None:
    """Stamp the explicit post-dispatch terminal contract for root chat.

    The worker owns subsequent progress. Asking the model to poll it creates a
    second state machine, spends provider rounds, and can fabricate completion
    claims. The root loop consumes this marker immediately after every tool in
    the current round has settled.
    """
    if not isinstance(task, dict) or result.get('ok') is not True:
        return
    task_id = str(result.get('task_id') or '')[:80]
    if not task_id:
        return
    lang = str(result.get('lang') or 'zh')
    labels = {
        PRODUCE_VIDEO_TOOL_NAME: ('视频', 'video'),
        PRODUCE_REPORT_TOOL_NAME: ('报告', 'report'),
        PRODUCE_SLIDES_TOOL_NAME: ('PPT', 'PPT deck'),
        PRODUCE_RESEARCH_TOOL_NAME: ('研究任务', 'research job'),
        EDIT_VIDEO_TOOL_NAME: ('镜头编辑', 'scene edit'),
        REVERT_VIDEO_TOOL_NAME: ('镜头回滚', 'scene revert'),
    }
    zh_label, en_label = labels.get(fn_name, ('后台任务', 'background job'))
    if lang == 'en':
        message = (
            f'The {en_label} is now running in the background (task '
            f'`{task_id}`). Track it in the task panel; completion and '
            'artifact quality will be reported there.')
    else:
        message = (
            f'{zh_label} 已在后台开始生成（任务 `{task_id}`）。请在任务面板'
            '查看进度；完成状态和成品质量会以后台任务结果为准。')
    task['_backgroundTaskAccepted'] = {
        'tool': fn_name,
        'taskId': task_id,
        'poll': str(result.get('poll') or '')[:240],
        'download': str(result.get('download') or '')[:240],
        'deduped': bool(result.get('deduped')),
        'round': int(round_num) + 1,
        'message': message,
    }


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
                                    transitions=fn_args.get('transitions'),
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
                        abort_event=abort_event,
                        owner_user_id=task_user_id(task))
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
            plan_path = _resolve(fn_args.get('audio_plan_path', ''), proj)
            if plan_path:
                scenes_path = _resolve(fn_args.get('scenes_path', ''), proj)
                with open(plan_path, encoding='utf-8') as handle:
                    raw_plan = json.load(handle)
                scenes = []
                if scenes_path:
                    with open(scenes_path, encoding='utf-8') as handle:
                        scenes = json.load(handle)
                    if not all(
                            scene.get('timeline_contract_version')
                            == mv.TIMELINE_CONTRACT_VERSION
                            for scene in scenes):
                        # Keep the direct tool on the same program-time clock
                        # as the topic engine.  Legacy storyboards do not yet
                        # carry timeline_start_s, so scene-relative cues would
                        # otherwise all resolve against t=0.
                        mv.normalise_timeline_contract(scenes)
                probe = mv.probe_video(video) or {}
                duration = float(probe.get('duration') or 0)
                out_dir = os.path.dirname(os.path.abspath(output)) or '.'
                plan = mv.normalise_audio_plan(
                    raw_plan, scenes,
                    base_dir=os.path.dirname(os.path.abspath(plan_path)),
                    stage_dir=os.path.join(out_dir, 'audio', 'assets'),
                    program_duration=duration)
                from lib.json_store import write_json_atomic
                normalized_path = os.path.join(out_dir, 'audio_plan.json')
                attribution_path = os.path.join(
                    out_dir, 'audio_attribution.txt')
                write_json_atomic(normalized_path, plan)
                mv.write_audio_attribution(plan, attribution_path)
                result = mv.mix_audio_timeline(
                    video, audio, plan, output, abort_event=abort_event)
                result['audio_plan_path'] = normalized_path
                result['audio_attribution_path'] = attribution_path
            else:
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
            model = str(fn_args.get('model') or '').strip()
            from lib.production.contracts import normalise_creative_mode
            creative_mode = normalise_creative_mode(
                fn_args.get('creative_mode'))

            job_id = _motion_task_id()
            workdir = _os.path.join(motion_root(), 'jobs', job_id)
            _os.makedirs(workdir, exist_ok=True)
            job = _new_motion_task(
                job_id, srt_path='', workdir=workdir, voice='', speed=None,
                alignment='loose', narration=narration, quality='standard',
                parallel=2, width=width, height=height, scenes_path='',
                user_id=task_user_id(task))
            job['topic'] = topic
            job['lang'] = lang
            job['max_scenes'] = max_scenes
            job['creative_mode'] = creative_mode
            job['kind'] = 'topic'
            # C4: chat-initiated films pause ONCE for storyboard review
            # (same shared human-gate registry as the slides outline gate);
            # headless/API jobs keep the flag off.
            job['interactive_storyboard'] = True
            if model:
                job['model'] = model
                job['qa_model'] = model
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
                      'creative_mode': creative_mode,
                      'visual_quality': visual,
                      'quality_hint': quality_hint,
                      'poll': f'/api/v4/motion/videos/poll/{job_id}',
                      'note': 'Video is generating in the background; watch '
                              'the video panel for progress.'}
            badge = 'queued'
        except Exception as e:
            logger.error('[Produce] failed to start video job: %s', e, exc_info=True)
            result = {'ok': False, 'detail': str(e)}
            badge = 'failed'

    tool_content = _fmt(result)
    _mark_background_task_accepted(task, fn_name, result, rn)
    meta = _build_simple_meta(
        fn_name, tool_content, source='Produce', title='video',
        snippet=tool_content.split('\n', 1)[0][:120] if tool_content else '',
        badge=badge, extra=_work_unit_ref(result, fn_name))
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
    checkpointed resume, and the generic ``/api/v4/tasks/*`` endpoints own
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
                                       conv_id=conv_id,
                                       user_id=task_user_id(task))
            tid = started['task_id']
            result = {'ok': True, 'task_id': tid, 'topic': topic,
                      'lang': lang, 'depth': depth,
                      'deduped': started.get('deduped', False),
                      'poll': f'/api/v4/tasks/{tid}',
                      'note': 'Report is generating in the background; it will '
                              'be published as a markdown artifact.'}
            badge = 'running' if started.get('deduped') else 'queued'
        except Exception as e:
            logger.error('[Produce] failed to start report job: %s', e,
                         exc_info=True)
            result = {'ok': False, 'detail': str(e)}
            badge = 'failed'

    tool_content = _fmt(result)
    _mark_background_task_accepted(task, fn_name, result, rn)
    meta = _build_simple_meta(
        fn_name, tool_content, source='Produce', title='report',
        snippet=tool_content.split('\n', 1)[0][:120] if tool_content else '',
        badge=badge, extra=_work_unit_ref(result, fn_name))
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, tool_content, False


@tool_registry.tool_set(
    {PRODUCE_SLIDES_TOOL_NAME},
    category='video',
    description='High-level topic → designer-quality editable PPTX')
def _handle_produce_slides(task, tc, fn_name, tc_id, fn_args, rn,
                           round_entry, cfg, project_path,
                           project_enabled, all_tools=None):
    """Handle produce_slides: kick off a background topic→deck job.

    Same substrate posture as produce_report: the recipe owns outline →
    design → author → assets → render → visual QA → export,
    lib.production.stages owns the checkpointed resume, and the generic
    /api/v4/tasks/* endpoints own poll / stream / abort. Files are served
    by routes/api_v1/slides.py.
    """
    topic = str(fn_args.get('topic') or '').strip()
    if not topic:
        result = {'ok': False, 'detail': 'topic is required'}
        badge = 'failed'
    else:
        try:
            from lib.slides.engine import start_slides_job
            from lib.slides.readiness import SlidesRuntimeUnavailable

            lang = 'en' if str(fn_args.get('lang') or 'zh').strip() == 'en' \
                else 'zh'
            style = str(fn_args.get('style') or '').strip()
            model = str(fn_args.get('model') or '').strip()
            from lib.production.contracts import normalise_creative_mode
            creative_mode = normalise_creative_mode(
                fn_args.get('creative_mode'))
            try:
                max_pages = int(fn_args.get('max_pages') or 12)
            except (TypeError, ValueError) as _e:
                logger.debug('handle produce slides: bad max_pages (%s)', _e)
                max_pages = 12
            max_pages = max(3, min(max_pages, 20))
            _SIZES = {'1280x720': (1280, 720), '960x540': (960, 540),
                      '720x540': (720, 540)}
            size = _SIZES.get(str(fn_args.get('size') or '1280x720'),
                              (1280, 720))
            conv_id = ''
            if isinstance(task, dict):
                conv_id = task.get('conv_id') or task.get('convId') or ''
            template_id = str(fn_args.get('template_id') or '').strip()
            started = start_slides_job(topic, lang=lang, style=style,
                                       max_pages=max_pages, size=size,
                                       conv_id=conv_id, model=model,
                                       creative_mode=creative_mode,
                                       interactive_outline=True,
                                       template_id=template_id,
                                       user_id=task_user_id(task))
            tid = started['task_id']
            result = {'ok': True, 'task_id': tid, 'topic': topic,
                      'lang': lang, 'style': style, 'max_pages': max_pages,
                      'model': model,
                      'creative_mode': creative_mode,
                      'template_id': template_id or None,
                      'deduped': started.get('deduped', False),
                      'poll': f'/api/v4/tasks/{tid}',
                      'download': f'/api/v4/slides/{tid}/file',
                      'note': 'The deck generates in the background. You '
                              'will get ONE outline to review in the card '
                              '(approve or request changes), then every '
                              'page streams in as a thumbnail; download the '
                              'PPTX when done.'}
            badge = 'running' if started.get('deduped') else 'queued'
        except SlidesRuntimeUnavailable as e:
            logger.error('[Produce] slides runtime is unavailable: %s', e)
            result = e.tool_result()
            badge = 'env-missing'
        except Exception as e:
            logger.error('[Produce] failed to start slides job: %s', e,
                         exc_info=True)
            result = {'ok': False, 'detail': str(e)}
            badge = 'failed'

    tool_content = _fmt(result)
    _mark_background_task_accepted(task, fn_name, result, rn)
    meta = _build_simple_meta(
        fn_name, tool_content, source='Produce', title='slides',
        snippet=tool_content.split('\n', 1)[0][:120] if tool_content else '',
        badge=badge, extra=_work_unit_ref(result, fn_name))
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, tool_content, False


@tool_registry.tool_set(
    {EDIT_SLIDES_TOOL_NAME},
    category='video',
    description='Plain-language single-page edit of a produced deck')
def _handle_edit_slides(task, tc, fn_name, tc_id, fn_args, rn,
                        round_entry, cfg, project_path,
                        project_enabled, all_tools=None):
    """Handle edit_slides: re-author one page of a finished deck job.

    Works on the live task AND on the disk anchor (a deck finished before a
    restart stays editable — same P-UX4 contract as motion's file serving).
    """
    task_id = str(fn_args.get('task_id') or '').strip()
    instruction = str(fn_args.get('instruction') or '').strip()

    def _page_list() -> tuple:
        """(pages, error) — single `page` or batch `pages` (≤8, deduped)."""
        raw_pages = fn_args.get('pages')
        if isinstance(raw_pages, list) and raw_pages:
            out = []
            for item in raw_pages[:8]:
                try:
                    n = int(item)
                except (TypeError, ValueError):
                    continue
                if n >= 1 and n not in out:
                    out.append(n)
            return out, ''
        try:
            n = int(fn_args.get('page') or 0)
        except (TypeError, ValueError):
            n = 0
        return ([n] if n >= 1 else []), ''

    pages, _pages_err = _page_list()
    if not task_id or not instruction or not pages:
        result = {'ok': False,
                  'detail': 'task_id, page/pages (1-based) and instruction '
                            'are required'}
        badge = 'failed'
    else:
        try:
            deck_dir = _resolve_deck_dir(task_id)
            if not deck_dir:
                result = {'ok': False,
                          'detail': f'no deck found for task {task_id}'}
                badge = 'failed'
            else:
                from lib.slides.author import edit_page
                from lib.slides.versions import list_versions, snapshot_deck
                lang = ('en' if str(fn_args.get('lang') or 'zh') == 'en'
                        else 'zh')
                owner = task_user_id(task)
                snapshot_deck(deck_dir, op='edit',
                              summary=f"pages {pages}: {instruction[:200]}")
                if len(pages) == 1:
                    page = pages[0]
                    res = edit_page(
                        deck_dir, page - 1, instruction, lang=lang,
                        owner_user_id=owner)
                    res['versions'] = list_versions(deck_dir)[:8]
                    res['task_id'] = task_id
                    res['download'] = f'/api/v4/slides/{task_id}/file'
                    res['preview_url'] = (f'/api/v4/slides/{task_id}/pages/'
                                          f'{page}.png')
                    result = res
                    badge = 'edited' if res.get('ok') else 'failed'
                else:
                    result = _batch_edit_pages(
                        deck_dir, task_id, pages, instruction, lang=lang,
                        owner=owner)
                    result['versions'] = list_versions(deck_dir)[:8]
                    badge = 'edited' if result.get('ok') else 'failed'
        except Exception as e:
            logger.error('[Produce] edit_slides failed: %s', e, exc_info=True)
            result = {'ok': False, 'detail': str(e)}
            badge = 'failed'

    tool_content = _fmt(result)
    meta = _build_simple_meta(
        fn_name, tool_content, source='Produce', title='edit slides',
        snippet=tool_content.split('\n', 1)[0][:120] if tool_content else '',
        badge=badge)
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, tool_content, False


def _batch_edit_pages(deck_dir: str, task_id: str, pages: list,
                      instruction: str, *, lang: str, owner: int) -> dict:
    """Batch edit_slides: per-page re-authoring behind a 2-worker bound,
    ONE PPTX re-export after every page settled.

    Per-page failures never fail the batch (a page that rejects its edit
    keeps its previous layout); the export only runs when at least one page
    actually changed, and concurrent exports are avoided by deferring each
    page's own export (``export=False``).
    """
    from lib.observability import InstrumentedThreadPoolExecutor
    from lib.slides.author import edit_page

    outcomes: dict[int, dict] = {}

    def _one(page: int) -> None:
        try:
            outcomes[page] = edit_page(
                deck_dir, page - 1, instruction, lang=lang,
                owner_user_id=owner, export=False)
        except Exception as e:   # edit_page never raises; belt anyway
            logger.debug('edit one page failed: %s', e)
            outcomes[page] = {'ok': False, 'detail': str(e)}

    with InstrumentedThreadPoolExecutor(max_workers=2, metric_pool='slides-batch-edit',
                                        thread_name_prefix='slides-batch-edit') as pool:
        list(pool.map(_one, pages))

    edited = [p for p in pages if (outcomes.get(p) or {}).get('ok')]
    failed = {p: (outcomes.get(p) or {}).get('detail') or 'edit failed'
              for p in pages if p not in edited}
    result: dict = {'ok': bool(edited), 'task_id': task_id,
                    'edited': edited, 'failed': failed,
                    'pages': {str(p): {'ok': (outcomes.get(p) or {}).get('ok'),
                                       'detail': (outcomes.get(p) or {}).get('detail', '')}
                              for p in pages},
                    'download': f'/api/v4/slides/{task_id}/file'}
    if edited:
        import glob as _glob
        from lib.slides.export_pptx import export_pptx
        from lib.slides.pptd import parse_deck
        existing = _glob.glob(os.path.join(deck_dir, '*.pptx'))
        out_path = existing[0] if existing else os.path.join(deck_dir,
                                                             'deck.pptx')
        try:
            export_pptx(parse_deck(os.path.join(deck_dir, 'deck.pptd')),
                        out_path)
            result['pptx_path'] = out_path
        except Exception as e:
            logger.error('[Produce] batch edit final export failed: %s', e,
                         exc_info=True)
            result['ok'] = False
            result['detail'] = (f'{len(edited)} page(s) edited but the final '
                                f'export failed: {e}')
            return result
        result['preview_url'] = (f'/api/v4/slides/{task_id}/pages/'
                                 f'{edited[0]}.png')
        result['note'] = (f'{len(edited)}/{len(pages)} page(s) re-authored; '
                          'the PPTX was re-exported once with every change.')
    else:
        result['detail'] = 'no page accepted the edit'
    return result


def _resolve_deck_dir(task_id: str) -> str:
    """Live slides task OR its disk anchor → the job's deck dir ('' = none).

    Same P-UX4 contract as motion's file serving: a deck finished before a
    restart stays editable from its on-disk manifest.
    """
    from lib.slides.runtime import _slides_runtime
    job = _slides_runtime.get(task_id)
    workdir = ''
    if job:
        workdir = ((job.get('result') or {}).get('workdir')
                   or job.get('workdir') or '')
    if not workdir:
        import re as _re
        if _re.fullmatch(r'[A-Za-z0-9_-]{1,64}', task_id):
            from lib.production.jobs import read_manifest
            from lib.slides.engine import slides_root
            cand = os.path.join(slides_root(), 'jobs', task_id)
            if read_manifest(cand):
                workdir = cand
    deck_dir = os.path.join(workdir, 'deck') if workdir else ''
    if not deck_dir or not os.path.isfile(
            os.path.join(deck_dir, 'deck.pptd')):
        return ''
    return deck_dir


@tool_registry.tool_set(
    {PATCH_SLIDES_TOOL_NAME},
    category='video',
    description='Deterministic field-level patch of a produced deck page')
def _handle_patch_slides(task, tc, fn_name, tc_id, fn_args, rn,
                         round_entry, cfg, project_path,
                         project_enabled, all_tools=None):
    """Handle patch_slides: zero-LLM ops on ONE page of a finished deck job.

    Synchronous (seconds, no LLM): ops apply against the raw page YAML,
    validation + measured layout gate roll back on regression, then the
    preview re-renders and the PPTX re-exports. Returns the structured
    ``changes[]`` so the model can narrate exactly what moved.
    """
    task_id = str(fn_args.get('task_id') or '').strip()
    ops = fn_args.get('ops')
    try:
        page = int(fn_args.get('page') or 0)
    except (TypeError, ValueError) as _e:
        logger.debug('handle patch slides: bad page (%s)', _e)
        page = 0
    if not task_id or not isinstance(ops, list) or not ops or page < 1:
        result = {'ok': False,
                  'detail': 'task_id, page (1-based) and a non-empty ops '
                            'array are required'}
        badge = 'failed'
    else:
        try:
            deck_dir = _resolve_deck_dir(task_id)
            if not deck_dir:
                result = {'ok': False,
                          'detail': f'no deck found for task {task_id}'}
                badge = 'failed'
            else:
                from lib.slides.patch import apply_page_patch
                from lib.slides.versions import list_versions, snapshot_deck
                op_names = ','.join(sorted({
                    str(o.get('op') or '') for o in ops
                    if isinstance(o, dict)}) - {''})
                snapshot_deck(deck_dir, op='patch',
                              summary=f'page {page}: {op_names}'[:400])
                res = apply_page_patch(deck_dir, page - 1, ops)
                res['versions'] = list_versions(deck_dir)[:8]
                res['task_id'] = task_id
                res['download'] = f'/api/v4/slides/{task_id}/file'
                res['preview_url'] = (f'/api/v4/slides/{task_id}/pages/'
                                      f'{page}.png')
                if res.get('ok'):
                    res['note'] = ('Patch applied deterministically (no '
                                   'LLM). changes[] lists every mutation '
                                   'with before/after — narrate them to the '
                                   'user verbatim.')
                result = res
                badge = 'edited' if res.get('ok') else 'failed'
        except Exception as e:
            logger.error('[Produce] patch_slides failed: %s', e,
                         exc_info=True)
            result = {'ok': False, 'detail': str(e)}
            badge = 'failed'

    tool_content = _fmt(result)
    meta = _build_simple_meta(
        fn_name, tool_content, source='Produce', title='patch slides',
        snippet=tool_content.split('\n', 1)[0][:120] if tool_content else '',
        badge=badge)
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, tool_content, False


@tool_registry.tool_set(
    {RESTYLE_SLIDES_TOOL_NAME},
    category='video',
    description='Deck-level deterministic restyle (theme/palette/fonts)')
def _handle_restyle_slides(task, tc, fn_name, tc_id, fn_args, rn,
                           round_entry, cfg, project_path,
                           project_enabled, all_tools=None):
    """Handle restyle_slides: zero-LLM theme/palette/font swap + full
    re-render + re-export. Synchronous (one browser boot)."""
    task_id = str(fn_args.get('task_id') or '').strip()
    theme_id = str(fn_args.get('theme_id') or '').strip()
    colors = fn_args.get('colors')
    fonts = fn_args.get('fonts')
    if not task_id or (not theme_id and not colors and not fonts):
        result = {'ok': False,
                  'detail': 'task_id plus at least one of theme_id / colors '
                            '/ fonts is required'}
        badge = 'failed'
    else:
        try:
            deck_dir = _resolve_deck_dir(task_id)
            if not deck_dir:
                result = {'ok': False,
                          'detail': f'no deck found for task {task_id}'}
                badge = 'failed'
            else:
                from lib.slides.theme_ops import apply_deck_restyle
                from lib.slides.versions import list_versions, snapshot_deck
                snapshot_deck(
                    deck_dir, op='restyle',
                    summary=(theme_id or str(colors or fonts or ''))[:400])
                res = apply_deck_restyle(
                    deck_dir, theme_id=theme_id,
                    colors=colors if isinstance(colors, dict) else None,
                    fonts=fonts if isinstance(fonts, dict) else None)
                res['versions'] = list_versions(deck_dir)[:8]
                res['task_id'] = task_id
                res['download'] = f'/api/v4/slides/{task_id}/file'
                if res.get('ok'):
                    res['note'] = ('Deck restyled deterministically (no '
                                   'LLM); every page was re-rendered and '
                                   'the PPTX re-exported.')
                result = res
                badge = 'edited' if res.get('ok') else 'failed'
        except Exception as e:
            logger.error('[Produce] restyle_slides failed: %s', e,
                         exc_info=True)
            result = {'ok': False, 'detail': str(e)}
            badge = 'failed'

    tool_content = _fmt(result)
    meta = _build_simple_meta(
        fn_name, tool_content, source='Produce', title='restyle slides',
        snippet=tool_content.split('\n', 1)[0][:120] if tool_content else '',
        badge=badge)
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, tool_content, False


@tool_registry.tool_set(
    {REVERT_SLIDES_TOOL_NAME},
    category='video',
    description='Restore a deck to a pre-edit snapshot version')
def _handle_revert_slides(task, tc, fn_name, tc_id, fn_args, rn,
                          round_entry, cfg, project_path,
                          project_enabled, all_tools=None):
    """Handle revert_slides: restore a snapshot, re-render, re-export."""
    task_id = str(fn_args.get('task_id') or '').strip()
    try:
        version = int(fn_args.get('version') or 0)
    except (TypeError, ValueError):
        version = 0
    if not task_id or version < 1:
        result = {'ok': False,
                  'detail': 'task_id and version (1-based snapshot number) '
                            'are required'}
        badge = 'failed'
    else:
        try:
            deck_dir = _resolve_deck_dir(task_id)
            if not deck_dir:
                result = {'ok': False,
                          'detail': f'no deck found for task {task_id}'}
                badge = 'failed'
            else:
                from lib.slides.versions import list_versions, revert_deck
                res = revert_deck(deck_dir, version)
                res['versions'] = list_versions(deck_dir)[:8]
                res['task_id'] = task_id
                res['download'] = f'/api/v4/slides/{task_id}/file'
                if res.get('ok'):
                    res['note'] = (
                        f'Restored the snapshot v{version} taken before '
                        f'“{res.get("summary") or "that edit"}”. Every page '
                        'was re-rendered and the PPTX re-exported.')
                result = res
                badge = 'edited' if res.get('ok') else 'failed'
        except Exception as e:
            logger.error('[Produce] revert_slides failed: %s', e,
                         exc_info=True)
            result = {'ok': False, 'detail': str(e)}
            badge = 'failed'

    tool_content = _fmt(result)
    meta = _build_simple_meta(
        fn_name, tool_content, source='Produce', title='revert slides',
        snippet=tool_content.split('\n', 1)[0][:120] if tool_content else '',
        badge=badge)
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, tool_content, False


@tool_registry.tool_set(
    {REVERT_VIDEO_TOOL_NAME},
    category='video',
    description='Restore a video scene to a pre-edit composition snapshot')
def _handle_revert_video(task, tc, fn_name, tc_id, fn_args, rn,
                         round_entry, cfg, project_path,
                         project_enabled, all_tools=None):
    """Handle revert_video: restore one scene's composition snapshot, then
    re-render only that scene and re-assemble the film (async receipt)."""
    task_id = str(fn_args.get('task_id') or '').strip()
    scene_ref = str(fn_args.get('scene') or '').strip()
    try:
        version = int(fn_args.get('version') or 0)
    except (TypeError, ValueError):
        version = 0

    def _scene_id(ref: str, scenes: list) -> str:
        if not ref:
            return ''
        for sc in scenes:
            if str(sc.get('id') or '') == ref:
                return ref
        try:
            n = int(ref)
        except ValueError:
            return ''
        for sc in scenes:
            sid = str(sc.get('id') or '')
            if sid == f'scene-{n:03d}' or sid == str(n):
                return sid
        return ''

    if not task_id or not scene_ref or version < 1:
        result = {'ok': False,
                  'detail': 'task_id, scene and version (1-based snapshot '
                            'number) are required'}
        badge = 'failed'
    else:
        try:
            from lib.motion_video.engine import (
                resolve_revision_parent, spawn_scene_revision)
            owner = task_user_id(task)
            params, workdir, error = resolve_revision_parent(task_id, owner)
            scenes = []
            scene_id = ''
            if not error and workdir:
                import json as _json
                try:
                    with open(os.path.join(workdir, 'scenes.json'),
                              encoding='utf-8') as f:
                        scenes = _json.load(f)
                    scene_id = _scene_id(scene_ref, scenes)
                except OSError:
                    scenes = []
            if error == 'not_done':
                result = {'ok': False,
                          'detail': 'the video job is still running — revert '
                                    'when it finishes'}
                badge = 'failed'
            elif error or not workdir:
                result = {'ok': False,
                          'detail': f'no finished video job found for task '
                                    f'{task_id}'}
                badge = 'failed'
            elif not scene_id:
                result = {'ok': False,
                          'detail': f'scene {scene_ref!r} not found'}
                badge = 'failed'
            else:
                sub_id, _kind = spawn_scene_revision(
                    task_id, scene_id, params, workdir, owner_user_id=owner,
                    instruction=str(version), op='revert')
                result = {
                    'ok': True, 'task_id': sub_id, 'edit_of': task_id,
                    'scene_id': scene_id, 'op': 'revert',
                    'version': version,
                    'poll': f'/api/v4/tasks/{sub_id}',
                    'file': f'/api/v4/motion/videos/{task_id}/file',
                    'note': 'The scene revert is running in the background: '
                            'the snapshot composition is restored, only that '
                            'scene is re-rendered, and the final video '
                            're-assembles at its original URL.'}
                badge = 'queued'
        except Exception as e:
            logger.error('[Produce] revert_video failed: %s', e,
                         exc_info=True)
            result = {'ok': False, 'detail': str(e)}
            badge = 'failed'

    tool_content = _fmt(result)
    _mark_background_task_accepted(task, fn_name, result, rn)
    meta = _build_simple_meta(
        fn_name, tool_content, source='Produce', title='revert video',
        snippet=tool_content.split('\n', 1)[0][:120] if tool_content else '',
        badge=badge, extra=_work_unit_ref(result, fn_name))
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, tool_content, False


@tool_registry.tool_set(
    {EDIT_VIDEO_TOOL_NAME},
    category='video',
    description='Plain-language single-scene edit of a produced video')
def _handle_edit_video(task, tc, fn_name, tc_id, fn_args, rn,
                       round_entry, cfg, project_path,
                       project_enabled, all_tools=None):
    """Handle edit_video: rewrite one scene of a finished video job.

    Async receipt posture (unlike edit_slides' synchronous page edit): the
    scene author + re-render + re-assembly take minutes, so the handler
    resolves the parent job (live OR disk manifest — same P-UX4 contract as
    the route), spawns the edit sub-task, and returns its id immediately.
    The workUnit card tracks the sub-task to completion.
    """
    task_id = str(fn_args.get('task_id') or '').strip()
    scene_ref = str(fn_args.get('scene') or '').strip()
    instruction = str(fn_args.get('instruction') or '').strip()
    model = str(fn_args.get('model') or '').strip()
    op = str(fn_args.get('op') or 'rewrite').strip()
    if op not in ('rewrite', 'set_narration', 'set_subtitle_style',
                  'swap_asset'):
        op = 'rewrite'
    style = fn_args.get('style')
    style = dict(style) if isinstance(style, dict) else None
    if op == 'swap_asset':
        style = {'kind': str(fn_args.get('asset_kind') or 'stock'),
                 'target': str(fn_args.get('target') or '')}

    def _scene_id(ref: str, scenes: list) -> str:
        """'2' / 'scene-002' → the storyboard's scene id ('' = no match)."""
        if not ref:
            return ''
        for sc in scenes:
            if str(sc.get('id') or '') == ref:
                return ref
        try:
            n = int(ref)
        except ValueError:
            return ''
        for sc in scenes:
            sid = str(sc.get('id') or '')
            if sid == f'scene-{n:03d}' or sid == str(n):
                return sid
        return ''

    job_level = op == 'set_subtitle_style'
    if not task_id or (not job_level and not scene_ref) or not instruction:
        result = {'ok': False,
                  'detail': 'task_id, scene (for scene-level ops) and '
                            'instruction are required'}
        badge = 'failed'
    elif job_level and not style:
        result = {'ok': False,
                  'detail': 'set_subtitle_style needs a style object '
                            '(font_scale/font_px/margin_v/outline/font_name)'}
        badge = 'failed'
    else:
        try:
            from lib.motion_video.engine import resolve_revision_parent
            owner = task_user_id(task)
            params, workdir, error = resolve_revision_parent(task_id, owner)
            scenes = []
            scene_id = ''
            if job_level:
                scene_id = '_'
            elif not error and workdir:
                import json as _json
                scenes_file = os.path.join(workdir, 'scenes.json')
                try:
                    with open(scenes_file, encoding='utf-8') as f:
                        scenes = _json.load(f)
                    scene_id = _scene_id(scene_ref, scenes)
                except OSError:
                    scenes = []
            if error == 'not_done':
                result = {'ok': False,
                          'detail': 'the video job is still running — edit it '
                                    'when it finishes'}
                badge = 'failed'
            elif error or not workdir:
                result = {'ok': False,
                          'detail': f'no finished video job found for task '
                                    f'{task_id}'}
                badge = 'failed'
            elif not scene_id:
                known = [str(sc.get('id') or '') for sc in scenes][:12]
                result = {'ok': False,
                          'detail': f'scene {scene_ref!r} not found; this job '
                                    f'has: {", ".join(known) or "none"}'}
                badge = 'failed'
            elif not os.path.isfile(os.path.join(
                    workdir, 'scenes', scene_id, 'index.html')):
                result = {'ok': False,
                          'detail': f'scene {scene_id} has no composition to '
                                    'edit'}
                badge = 'failed'
            else:
                from lib.motion_video.engine import spawn_scene_revision
                if model:
                    params = dict(params, model=model)
                sub_id, _kind = spawn_scene_revision(
                    task_id, scene_id, params, workdir, owner_user_id=owner,
                    instruction=instruction[:2000], op=op, style=style)
                lang = ('en' if str(fn_args.get('lang') or 'zh') == 'en'
                        else 'zh')
                if op == 'swap_asset':
                    note = ('The asset swap is running in the background: '
                            'the replacement resolves from your query, the '
                            'composition updates honestly, then only that '
                            'scene is re-rendered and the film re-assembles '
                            'in place.' if lang == 'en' else
                            '素材替换已在后台进行：按你的描述解析新素材并诚实'
                            '更新构图引用，然后只重渲该镜头并原地重组装。')
                elif op == 'set_subtitle_style':
                    note = ('The subtitle restyle is running in the '
                            'background: the overrides are pinned for every '
                            'later revision, then the film is re-burned and '
                            're-muxed in place.' if lang == 'en' else
                            '字幕样式替换已在后台进行：样式会先固定下来（后续'
                            '所有修订都会沿用），然后重烧字幕并原地重新混流。')
                elif op == 'set_narration':
                    note = ('The narration swap is running in the '
                            'background: new TTS for that scene (it must '
                            'fit the existing duration), then the audio '
                            'track and subtitles rebuild — the frame is '
                            'untouched.' if lang == 'en' else
                            '旁白替换已在后台进行：为该镜头合成新配音（须容纳于'
                            '现有时长），随后重建音轨与字幕——画面不变。')
                else:
                    note = (
                        'The scene edit is running in the background: the '
                        'author rewrites the composition with your '
                        'instruction, the better of old/new wins, then only '
                        'that scene is re-rendered and the final video is '
                        're-assembled at its original URL.'
                        if lang == 'en' else
                        '镜头编辑已在后台进行：作者会基于当前画面按你的指令重写'
                        '构图，新旧两版择优保留，然后只重渲该镜头并在原链接上'
                        '重新合成最终视频。')
                result = {
                    'ok': True, 'task_id': sub_id, 'edit_of': task_id,
                    'scene_id': scene_id, 'op': op, 'lang': lang,
                    'poll': f'/api/v4/tasks/{sub_id}',
                    'file': f'/api/v4/motion/videos/{task_id}/file',
                    'note': note}
                badge = 'queued'
        except Exception as e:
            logger.error('[Produce] edit_video failed: %s', e, exc_info=True)
            result = {'ok': False, 'detail': str(e)}
            badge = 'failed'

    tool_content = _fmt(result)
    _mark_background_task_accepted(task, fn_name, result, rn)
    meta = _build_simple_meta(
        fn_name, tool_content, source='Produce', title='edit video',
        snippet=tool_content.split('\n', 1)[0][:120] if tool_content else '',
        badge=badge, extra=_work_unit_ref(result, fn_name))
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
    and the generic ``/api/v4/tasks/*`` endpoints own poll / stream / abort.
    """
    direction = str(fn_args.get('direction') or '').strip()
    if not direction:
        result = {'ok': False, 'detail': 'direction is required'}
        badge = 'failed'
    else:
        try:
            from lib.research.api import produce_research
            from lib.research.contracts import normalize_research_request

            request = normalize_research_request(
                direction, lang=fn_args.get('lang'),
                n_ideas=fn_args.get('n_ideas'),
                seed_arxiv_ids=fn_args.get('seed_arxiv_ids'))
            conv_id = ''
            if isinstance(task, dict):
                conv_id = task.get('conv_id') or task.get('convId') or ''
            started = produce_research(
                request.direction, lang=request.lang,
                n_ideas=request.n_ideas, conv_id=conv_id,
                seed_arxiv_ids=(list(request.seed_arxiv_ids) or None),
                user_id=task_user_id(task))
            tid = started['task_id']
            result = {'ok': True, 'task_id': tid,
                      'direction': request.direction,
                      'lang': request.lang, 'n_ideas': request.n_ideas,
                      'deduped': started.get('deduped', False),
                      'poll': f'/api/v4/tasks/{tid}',
                      'note': 'Research is running in the background: the '
                              'literature corpus is harvested and surveyed '
                              'first, then ideas are scored against it.'}
            badge = 'running' if started.get('deduped') else 'queued'
        except Exception as e:
            logger.error('[Produce] failed to start research job: %s', e,
                         exc_info=True)
            result = {'ok': False, 'detail': str(e)}
            badge = 'failed'

    tool_content = _fmt(result)
    _mark_background_task_accepted(task, fn_name, result, rn)
    meta = _build_simple_meta(
        fn_name, tool_content, source='Produce', title='research',
        snippet=tool_content.split('\n', 1)[0][:120] if tool_content else '',
        badge=badge, extra=_work_unit_ref(result, fn_name))
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, tool_content, False
