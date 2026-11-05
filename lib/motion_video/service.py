"""Initial-film admission shared by HTTP, paper abstracts and Chat tools.

Callers validate their domain inputs and choose dedup/interactive policy. This
owner materializes accepted sources and submits exactly one worker through the
existing ProductionRuntime; engine owns stages, cancellation and publication.
No recipe, renderer or provider is imported until a new task is submitted.
"""

from __future__ import annotations

import json
import os


def start_motion_video(*, user_id: int, dedup_key: tuple | None = None,
                        force: bool = False, srt_text: str = '',
                        srt_path: str = '', scenes_path: str = '',
                        scenes: list[dict] | None = None,
                        voice: str = '', speed=None, alignment: str = 'loose',
                        narration: bool = True, quality: str = 'standard',
                        parallel: int = 2, width: int = 1080, height: int = 1440,
                        burn_in: bool | None = None,
                        burn_in_fontsdir: str | None = None,
                        scene_author: bool | None = None,
                        author_token_budget: int | None = None,
                        audio_plan: dict | None = None, audio_plan_path: str = '',
                        audio_base_dir: str = '', model: str | None = None,
                        qa_model: str | None = None, paper_hash: str | None = None,
                        topic: str = '', lang: str = 'zh', max_scenes: int = 8,
                        creative_mode: str = '',
                        interactive_storyboard: bool | None = None) -> tuple[str, bool]:
    """Return ``(task_id, joined)`` for one initial film.

    Dedup keys are scoped to the explicit owner here. No key means fresh work;
    force replaces the live index without cancelling the previous film. Only
    the winning claim writes sources. Optional engine fields remain absent
    when the caller has no preference, preserving their owning defaults.
    """
    from lib.error_envelope import BadRequest
    from lib.identity import require_user_id
    from lib.motion_video._env import motion_root
    from lib.motion_video.runtime import _production, _motion_task_shape

    user_id = require_user_id(user_id, context='motion video')
    key = (user_id, *dedup_key) if dedup_key is not None else None
    if key is not None and not force:
        existing = _production.index_get(key)
        if existing:
            return existing, True
    if not srt_text and srt_path and not os.path.isfile(srt_path):
        raise BadRequest('srt_path is not a file', field='srt_path')

    task_id = _production.new_task_id()
    workdir = os.path.join(motion_root(), 'jobs', task_id)
    if srt_text:
        srt_path = os.path.join(workdir, 'transcription.srt')
    if scenes is not None:
        scenes_path = os.path.join(workdir, 'scenes.json')
    meta, fields = _motion_task_shape(
        srt_path=srt_path, workdir=workdir, voice=voice, speed=speed,
        alignment=alignment, narration=narration, quality=quality,
        parallel=parallel, width=width, height=height, scenes_path=scenes_path)
    fields.update({name: value for name, value in {
        'burn_in': burn_in, 'burn_in_fontsdir': burn_in_fontsdir,
        'scene_author': scene_author, 'author_token_budget': author_token_budget,
        'audio_plan': audio_plan or None, 'audio_plan_path': audio_plan_path or None,
        'audio_base_dir': audio_base_dir or None, 'model': model, 'qa_model': qa_model,
        'paper_hash': paper_hash, 'interactive_storyboard': interactive_storyboard,
    }.items() if value is not None})
    if topic:
        fields.update(topic=topic, lang=lang, max_scenes=max_scenes,
                      creative_mode=creative_mode)
    fields['source_kind'] = 'topic' if topic else 'scenes'

    if key is not None and not force:
        task, existing = _production.claim_task(
            key, task_id, user_id=user_id, meta=meta, fields=fields)
        if existing:
            return existing, True
    else:
        task = _production.create_task(task_id, user_id=user_id, meta=meta, fields=fields)
        if key is not None:
            _production.index_register(key, task_id)
    try:
        os.makedirs(workdir, exist_ok=True)
        if srt_text:
            with open(srt_path, 'w', encoding='utf-8') as source:
                source.write(srt_text)
        if scenes is not None:
            with open(scenes_path, 'w', encoding='utf-8') as source:
                json.dump(scenes, source, ensure_ascii=False, indent=1)
        from lib.motion_video.engine import run_motion_task
        _production.runtime.spawn(task_id, run_motion_task, task)
    except Exception as exc:
        _production.runtime.finish(task_id, error=exc, error_context='motion-video:start')
        raise
    return task_id, False
