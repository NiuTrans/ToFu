"""routes/api_v1/motion.py — Motion-video headless API.

Routes:
  GET  /api/v1/motion/status                     — render-chain env + TTS probe
  POST /api/v1/motion/videos                     — start (or dedup-join) a video task
  GET  /api/v1/motion/videos/poll/<task_id>      — cursor poll (factory)
  POST /api/v1/motion/videos/abort/<task_id>     — abort (factory)
  GET  /api/v1/motion/videos/<task_id>/file      — final MP4 (Range) [sync]
  GET  /api/v1/motion/videos/<task_id>/file?part=srt — aligned sidecar SRT

Body for POST /videos::

    {
      "srt": "<SRT text>",              // or "srt_path": "/abs/path.srt"
      "scenes_path": "/abs/scenes.json" // optional agent-made storyboard
      "narration": true,                // TTS voice-over (degrades to silent)
      "voice": "", "speed": null,       // TTS overrides
      "alignment": "loose",             // loose | strict
      "quality": "standard",            // draft | standard | high
      "parallel": 2,                    // scene render pool (1..4)
      "aspect": "1080x1440"             // 1080x1440 | 1080x1920 | 1920x1080 | 1080x1080
    }

The engine (:mod:`lib.motion_video.engine`) is the zero-LLM fallback path;
the chat-agent flow (motion_video_* tools) remains the creative one.
"""

from __future__ import annotations

import hashlib
import os

from flask import Blueprint, jsonify, send_file

from lib.api_response import api_bad_request, api_not_found
from lib.log import get_logger
from lib.motion_video.runtime import _motion_runtime
from lib.openapi import api_meta
from lib.request_parser import async_parse_body
from routes._task_routes import register_task_routes

from .auth import require_auth

logger = get_logger(__name__)

api_v1_motion_bp = Blueprint('api_v1_motion', __name__)

_ASPECTS = {
    '1080x1440': (1080, 1440),
    '1080x1920': (1080, 1920),
    '1920x1080': (1920, 1080),
    '1080x1080': (1080, 1080),
}
_QUALITIES = ('draft', 'standard', 'high')
_ALIGNMENTS = ('loose', 'strict')
_MAX_SRT_BYTES = 2 * 1024 * 1024


@api_v1_motion_bp.route('/api/v1/motion/status', methods=['GET'])
@require_auth
@api_meta(summary='Motion-video environment probe',
          description='Render-chain readiness (node/hyperframes/ffmpeg/'
                      'ffprobe/Chrome) + TTS availability.',
          tags=['motion'])
async def motion_status():
    from lib import motion_video as mv
    env = mv.probe_env()
    try:
        import lib.tts as _tts
        env['tts_available'] = bool(_tts.tts_available())
    except Exception as e:
        logger.debug('[Motion.v1] tts probe failed: %s', e)
        env['tts_available'] = False
    return jsonify(env)


@api_v1_motion_bp.route('/api/v1/motion/videos', methods=['POST'])
@require_auth
@api_meta(summary='Start (or join) a motion-video task',
          description='Dedup key: (srt sha, voice, alignment, aspect, '
                      'narration). A second identical POST joins the '
                      'in-flight task.',
          tags=['motion'])
async def start_motion_task():
    from lib.motion_video.runtime import (
        _cleanup_stale_motion_tasks,
        _motion_index_get,
        _motion_index_register,
        _motion_task_id,
        _new_motion_task,
    )
    from lib.motion_video._env import motion_root
    from lib.motion_video.engine import run_motion_task

    body = await async_parse_body()
    srt_text = (body.get('srt') or '').strip()
    srt_path_in = (body.get('srt_path') or '').strip()
    scenes_path = (body.get('scenes_path') or '').strip()
    topic = (body.get('topic') or '').strip()
    if not srt_text and not srt_path_in and not scenes_path and not topic:
        return api_bad_request('srt, srt_path, scenes_path or topic is required',
                               field='topic')
    if len(topic) > 500:
        return api_bad_request('topic too long (500 chars max)', field='topic')
    if len(srt_text.encode('utf-8')) > _MAX_SRT_BYTES:
        return api_bad_request('srt too large (2 MB max)', field='srt')
    lang = (body.get('lang') or 'zh').strip()
    if lang not in ('zh', 'en'):
        return api_bad_request('lang must be zh|en', field='lang')
    try:
        max_scenes = int(body.get('max_scenes') or 8)
    except (TypeError, ValueError):
        return api_bad_request('max_scenes must be an int', field='max_scenes')
    max_scenes = max(3, min(max_scenes, 12))

    aspect = (body.get('aspect') or '1080x1440').strip()
    if aspect not in _ASPECTS:
        return api_bad_request(
            f'unknown aspect {aspect!r} (one of {sorted(_ASPECTS)})',
            field='aspect')
    width, height = _ASPECTS[aspect]
    quality = (body.get('quality') or 'standard').strip()
    if quality not in _QUALITIES:
        return api_bad_request('quality must be draft|standard|high',
                               field='quality')
    alignment = (body.get('alignment') or 'loose').strip()
    if alignment not in _ALIGNMENTS:
        return api_bad_request('alignment must be loose|strict',
                               field='alignment')
    narration = bool(body.get('narration', True))
    voice = (body.get('voice') or '').strip()
    speed = body.get('speed')
    if speed is not None:
        try:
            speed = float(speed)
        except (TypeError, ValueError):
            return api_bad_request('speed must be a number', field='speed')
    try:
        parallel = int(body.get('parallel') or 2)
    except (TypeError, ValueError):
        return api_bad_request('parallel must be an int', field='parallel')
    parallel = max(1, min(parallel, 4))
    if scenes_path and not os.path.isfile(scenes_path):
        return api_bad_request('scenes_path is not a file',
                               field='scenes_path')
    burn_in = bool(body.get('burn_in', False))
    burn_in_fontsdir = (body.get('burn_in_fontsdir') or '').strip()
    # Per-scene composition author (P5). None = follow the server default
    # (env TOFU_MOTION_SCENE_AUTHOR); an explicit bool overrides per job.
    scene_author = body.get('scene_author')
    if scene_author is not None:
        scene_author = bool(scene_author)
    # 0 = "no preference": the default lives in ONE place
    # (lib/motion_video/_scene_author._DEFAULT_*). Writing the literals here
    # is what made the 60000 → 90000 raise dead on this path.
    try:
        author_rounds = int(body.get('author_rounds') or 0)
        author_token_budget = int(body.get('author_token_budget') or 0)
    except (TypeError, ValueError):
        return api_bad_request('author_rounds / author_token_budget must be ints',
                               field='author_rounds')
    author_rounds = min(max(author_rounds, 0), 8)
    author_token_budget = (0 if author_token_budget <= 0
                           else max(5000, min(author_token_budget, 400000)))

    # ── Dedup ──
    _cleanup_stale_motion_tasks()
    srt_material = srt_text or srt_path_in or scenes_path or (f'topic:{lang}:{topic}')
    srt_sha = hashlib.sha256(srt_material.encode('utf-8')).hexdigest()[:16]
    key = (srt_sha, voice, alignment, aspect, narration, quality, burn_in)
    existing = _motion_index_get(key)
    if existing:
        logger.info('[Motion.v1] dedup join: %s', existing)
        return jsonify({'ok': True, 'task_id': existing, 'deduped': True})

    task_id = _motion_task_id()
    workdir = os.path.join(motion_root(), 'jobs', task_id)
    os.makedirs(workdir, exist_ok=True)

    if srt_text:
        srt_path = os.path.join(workdir, 'transcription.srt')
        with open(srt_path, 'w', encoding='utf-8') as f:
            f.write(srt_text)
    elif srt_path_in:
        srt_path = srt_path_in
        if not os.path.isfile(srt_path):
            return api_bad_request('srt_path is not a file', field='srt_path')
    else:
        srt_path = ''  # scenes-only / topic run — the recipe/storyboard is the source

    task = _new_motion_task(
        task_id, srt_path=srt_path, workdir=workdir, voice=voice,
        speed=speed, alignment=alignment, narration=narration,
        quality=quality, parallel=parallel, width=width, height=height,
        scenes_path=scenes_path)
    task['burn_in'] = burn_in
    task['burn_in_fontsdir'] = burn_in_fontsdir
    if scene_author is not None:
        task['scene_author'] = scene_author
    task['author_rounds'] = author_rounds
    task['author_token_budget'] = author_token_budget
    if topic:
        task['topic'] = topic
        task['lang'] = lang
        task['max_scenes'] = max_scenes
        task['kind'] = 'topic'
    _motion_index_register(key, task_id)
    _motion_runtime.spawn(task_id, run_motion_task, task)
    logger.info('[Motion.v1] started %s (aspect=%s narration=%s parallel=%d '
                'topic=%s)', task_id, aspect, narration, parallel, bool(topic))
    return jsonify({'ok': True, 'task_id': task_id, 'deduped': False})


register_task_routes(api_v1_motion_bp, _motion_runtime,
    url_prefix='/api/v1/motion/videos')


@api_v1_motion_bp.route('/api/v1/motion/videos/<task_id>/file', methods=['GET'])
@require_auth
def serve_motion_file(task_id):
    """Serve the final MP4 (or ?part=srt sidecar) with Range support.

    SYNC on purpose: pure file serving whose only blocking call is the
    sync-safe ``send_file`` shim (same carve-out as serve_paper_image).
    Path safety: we serve exactly the path recorded in the task result —
    never client-supplied path material.
    """
    task = _motion_runtime.get(task_id)
    from flask import request as _req
    part = (_req.args.get('part') or 'mp4').strip()
    if task:
        result = task.get('result') or {}
        path = result.get('srt_path') if part == 'srt' else result.get('final_path')
    else:
        # P-UX4: task gone from memory (restart / TTL) — serve from the
        # on-disk job dir so a finished video stays playable.
        workdir = _disk_job_workdir(task_id)
        if not workdir:
            return api_not_found('not_found')
        path = os.path.join(workdir, 'final.srt' if part == 'srt'
                            else 'final.mp4')
    if not path or not os.path.isfile(path):
        return api_not_found('file_not_ready')
    mimetype = 'application/x-subrip' if part == 'srt' else 'video/mp4'
    return send_file(path, mimetype=mimetype, conditional=True)


def _job_workdir(task) -> str:
    """The job workdir for a task (from its result or its own field)."""
    result = task.get('result') or {}
    return result.get('workdir') or task.get('workdir') or ''


def _disk_job_workdir(task_id: str) -> str:
    """Workdir of a job whose task is no longer live in the runtime (P-UX4).

    A finished video must stay playable after a server restart: the
    manifest at ``<motion_root>/jobs/<task_id>/job.json`` is the disk
    anchor. Returns '' when there is no manifest (unknown task id).
    """
    import re as _re

    if not _re.fullmatch(r'[A-Za-z0-9_-]{1,64}', task_id or ''):
        return ''
    from lib.motion_video._env import motion_root
    from lib.production.jobs import read_manifest
    workdir = os.path.join(motion_root(), 'jobs', task_id)
    if not read_manifest(workdir):
        return ''
    return workdir


@api_v1_motion_bp.route('/api/v1/motion/videos/<task_id>/scenes',
                        methods=['GET'])
@require_auth
@api_meta(summary='List a video job\'s scenes',
          description='Per-scene status from the job workdir: composition, '
                      'rendered mp4, narration wav presence.',
          tags=['motion'])
async def list_motion_scenes(task_id):
    import json as _json

    task = _motion_runtime.get(task_id)
    if task:
        workdir = _job_workdir(task)
        status = task['status']
    else:
        # P-UX4 disk fallback (finished job after a restart).
        workdir = _disk_job_workdir(task_id)
        if not workdir:
            return api_not_found('not_found')
        status = 'done'
    scenes_file = os.path.join(workdir, 'scenes.json') if workdir else ''
    if not scenes_file or not os.path.isfile(scenes_file):
        return api_not_found('scenes_not_ready')
    with open(scenes_file, encoding='utf-8') as f:
        scenes = _json.load(f)
    out = []
    for sc in scenes:
        sid = sc.get('id')
        scene_dir = os.path.join(workdir, 'scenes', sid)
        out.append({
            'scene_id': sid,
            'start': sc.get('start'),
            'end': sc.get('end'),
            'text': sc.get('text'),
            'has_composition': os.path.isfile(
                os.path.join(scene_dir, 'index.html')),
            'has_video': os.path.isfile(
                os.path.join(scene_dir, f'{sid}.mp4')),
            'has_narration': os.path.isfile(
                os.path.join(workdir, 'audio', f'{sid}.wav')),
        })
    return jsonify({'ok': True, 'task_id': task_id,
                    'status': status, 'scenes': out})


@api_v1_motion_bp.route('/api/v1/motion/videos/<task_id>/scenes/<scene_id>/file',
                        methods=['GET'])
@require_auth
def serve_scene_file(task_id, scene_id):
    """Serve one scene's rendered MP4 (Range). SYNC — see serve_motion_file."""
    import re as _re

    if not _re.fullmatch(r'[A-Za-z0-9_-]{1,64}', scene_id or ''):
        return api_not_found('not_found')
    task = _motion_runtime.get(task_id)
    if task:
        workdir = _job_workdir(task)
    else:
        # P-UX4 disk fallback (finished job after a restart).
        workdir = _disk_job_workdir(task_id)
        if not workdir:
            return api_not_found('not_found')
    path = os.path.join(workdir, 'scenes', scene_id, f'{scene_id}.mp4')
    if not workdir or not os.path.isfile(path):
        return api_not_found('file_not_ready')
    return send_file(path, mimetype='video/mp4', conditional=True)


@api_v1_motion_bp.route('/api/v1/motion/videos/<task_id>/scenes/<scene_id>/regen',
                        methods=['POST'])
@require_auth
@api_meta(summary='Re-render one scene and re-assemble the final video',
          description='Spawns a regen task on the finished job: re-renders '
                      'the scene from its EXISTING composition, re-concats, '
                      're-burns / re-muxes, atomically replaces final.mp4.',
          tags=['motion'])
async def regen_scene(task_id, scene_id):
    import re as _re

    if not _re.fullmatch(r'[A-Za-z0-9_-]{1,64}', scene_id or ''):
        return api_not_found('not_found')
    from lib.motion_video.runtime import (
        _motion_runtime as rt,
        _motion_task_id,
        _new_motion_task,
    )
    from lib.motion_video.engine import run_scene_regen_task

    task = _motion_runtime.get(task_id)
    if not task:
        return api_not_found('not_found')
    if task['status'] not in ('done',):
        return api_bad_request('job must be done before re-rendering a scene',
                               field='task_id')
    workdir = _job_workdir(task)
    if not workdir or not os.path.isdir(workdir):
        return api_not_found('workdir_gone')

    regen_id = _motion_task_id()
    sub = _new_motion_task(
        regen_id, srt_path='', workdir=workdir,
        voice=task.get('voice') or '', speed=task.get('speed'),
        alignment=task.get('alignment') or 'loose',
        narration=bool(task.get('narration')),
        quality=task.get('quality') or 'standard',
        parallel=1, width=task['width'], height=task['height'])
    sub['scene_id'] = scene_id
    sub['regen_of'] = task_id
    sub['burn_in'] = bool(task.get('burn_in'))
    sub['burn_in_fontsdir'] = task.get('burn_in_fontsdir') or ''
    rt.spawn(regen_id, run_scene_regen_task, sub)
    logger.info('[Motion.v1] regen %s of job %s → task %s',
                scene_id, task_id, regen_id)
    return jsonify({'ok': True, 'task_id': regen_id, 'regen_of': task_id,
                    'scene_id': scene_id})


__all__ = ['api_v1_motion_bp']
