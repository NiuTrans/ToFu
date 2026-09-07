"""lib/slides/engine.py — headless worker for slide-deck jobs.

Thin by design (same posture as lib/longform/engine.py): the recipe owns the
work, the production substrate owns the checkpointed resume, this file only
bridges a TaskRuntime task to them and shapes the result.
"""

from __future__ import annotations

import os

from lib.agent_core.events import Phase, build_phase
from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['run_slides_task', 'slides_root', 'resume_interrupted_decks',
           'start_slides_job']


def slides_root() -> str:
    """Writable root for slide-deck state (job workdirs)."""
    from lib.runtime_paths import data_root
    path = os.path.join(data_root(), 'slides')
    os.makedirs(path, exist_ok=True)
    return path


def _emit(task: dict, event: dict) -> None:
    from lib.slides.runtime import _append_slides_event
    try:
        _append_slides_event(task, event)
    except Exception as e:
        logger.debug('[Slides] emit failed: %s', e)


#: Task fields persisted so a crashed process can re-spawn this job.
_MANIFEST_FIELDS = ('task_id', 'topic', 'lang', 'style', 'max_pages', 'size',
                    'conv_id', 'workdir', 'model', 'creative_mode', 'user_id')


def _write_manifest(task: dict, state: str) -> None:
    from lib.production.jobs import write_manifest
    write_manifest(task.get('workdir') or '', task, fields=_MANIFEST_FIELDS,
                   kind='slides-deck', state=state, log_label='Slides')


def run_slides_task(task: dict) -> None:
    """Worker entry — topic → editable PPTX + preview grid."""
    from lib.production.stages import StageAborted, StageFailed
    from lib.slides.recipe import build_deck_from_topic
    from lib.slides.readiness import (
        SlidesRuntimeUnavailable,
        ensure_slides_runtime_ready,
    )
    from lib.slides.runtime import _slides_runtime

    task_id = task['task_id']
    owner_user_id = int(task.get('user_id') or task.get('_userId') or 0)
    if owner_user_id < 1:
        raise ValueError('slides task requires an explicit owner user_id')
    try:
        _write_manifest(task, 'running')
        _slides_runtime.mark_running(task_id)
        _emit(task, build_phase(Phase.START, topic=task.get('topic', '')))
        # Re-check inside the worker as well as at admission.  This protects
        # crash-resumed manifests and future out-of-process worker adapters.
        ensure_slides_runtime_ready()
        result = build_deck_from_topic(
            task['topic'], task['workdir'], lang=task.get('lang') or 'zh',
            style=task.get('style') or '', size=task.get('size') or (1280, 720),
            max_pages=int(task.get('max_pages') or 12),
            creative_mode=task.get('creative_mode') or 'director',
            model=task.get('model') or None,
            owner_user_id=owner_user_id,
            abort_event=task.get('abort_event'),
            emit=lambda ev: _emit(task, {'type': 'stage', **ev}))
        result['workdir'] = task['workdir']

        # Quality axis: a deck whose pages all degraded to fallbacks is a
        # structurally valid file out of a broken pipeline — status stays
        # 'done' by design; artifact_quality carries the truth.
        total = result.get('pages', 0)
        authored = result.get('authored_pages', 0)
        quality = result.get('quality') or {}
        degraded = bool(quality.get('degraded'))
        reason = str(quality.get('reason') or '')
        _write_manifest(task, 'degraded' if degraded else 'done')
        _emit(task, {'type': 'final', **result, 'degraded': degraded,
                     'degraded_reason': reason})
        _slides_runtime.finish(task_id, result=result, degraded=degraded,
                               degraded_reason=reason)
        logger.info('[Slides] %s %s — %d/%d pages authored, %d bytes',
                    task_id, 'degraded' if degraded else 'done',
                    authored, total, result.get('bytes', 0))
    except (InterruptedError, StageAborted):
        logger.info('[Slides] task %s aborted', task_id)
        _write_manifest(task, 'aborted')
        _slides_runtime.finish(task_id, error='aborted',
                               error_context='slides:abort')
    except SlidesRuntimeUnavailable as e:
        logger.error('[Slides] task %s has no render runtime: %s', task_id, e)
        _write_manifest(task, 'error')
        _slides_runtime.finish(task_id, error=e.error_envelope(),
                               error_context='slides:runtime-readiness')
    except StageFailed as e:
        from lib.error_envelope import make_envelope
        gate_detail = '; '.join(str(item) for item in e.errors[:4])
        detail = f'{e.stage}: {gate_detail or e.detail}'
        logger.error('[Slides] task %s stopped by quality gate: %s',
                     task_id, detail)
        _write_manifest(task, 'error')
        _slides_runtime.finish(
            task_id,
            error=make_envelope(
                'generic',
                message='PPT 质量门禁未通过\nPPT quality gate did not pass',
                detail=detail,
                hint=(
                    '流水线已保留通过校验的页面缓存。请稍后重试；系统只会重做'
                    '失败页面，不会把兜底页、缺失预览或未解决的溢出当作成品。\n\n'
                    'Validated page checkpoints were retained. Retry later; '
                    'only failed pages will be re-authored, and fallback pages '
                    'or unresolved layout defects will not be published.'),
                context=f'slides:quality:{e.stage}',
                source='lib.slides.engine',
                retryable=True,
            ),
            error_context=f'slides:quality:{e.stage}')
    except Exception as e:
        logger.error('[Slides] task %s failed: %s', task_id, e, exc_info=True)
        _write_manifest(task, 'error')
        _slides_runtime.finish(task_id, error=e,
                               error_context='slides:engine')


def resume_interrupted_decks() -> int:
    """Re-spawn deck jobs left ``running`` on disk by a crashed process."""
    from lib.production.jobs import resume_running_jobs
    from lib.slides.runtime import _new_slides_task, _slides_runtime

    def _respawn(task_id: str, workdir: str, m: dict) -> None:
        user_id = int(m['user_id'])
        if user_id < 1:
            raise ValueError('slides manifest has no valid owner')
        task = _new_slides_task(
            task_id, topic=m.get('topic') or '', workdir=workdir,
            lang=m.get('lang') or 'zh', style=m.get('style') or '',
            max_pages=int(m.get('max_pages') or 12),
            size=tuple(m.get('size') or (1280, 720)),
            conv_id=m.get('conv_id') or '', model=m.get('model') or '',
            creative_mode=m.get('creative_mode') or 'director',
            user_id=user_id)
        _slides_runtime.spawn(task_id, run_slides_task, task)

    return resume_running_jobs(
        os.path.join(slides_root(), 'jobs'),
        is_live=lambda tid: _slides_runtime.get(tid) is not None,
        respawn=_respawn, log_label='Slides')


def start_slides_job(topic: str, *, lang: str = 'zh', style: str = '',
                     max_pages: int = 12, size=(1280, 720),
                     conv_id: str = '', model: str = '',
                     creative_mode: str = 'director', user_id: int) -> dict:
    """Create + spawn a deck job; returns {task_id, deduped}."""
    from lib.slides.runtime import (
        _claim_slides_task, _cleanup_stale_slides_tasks, _slides_runtime,
        _slides_task_id)
    from lib.slides.contracts import (
        normalise_slide_model,
        normalise_slide_page_count,
        normalise_slide_size,
        normalise_slide_style,
        normalise_slide_topic,
    )
    from lib.production.contracts import CREATIVE_MODES, normalise_creative_mode

    _cleanup_stale_slides_tasks()
    topic = normalise_slide_topic(topic)
    style = normalise_slide_style(style)
    model = normalise_slide_model(model)
    creative_raw = str(creative_mode or '').strip().lower()
    if creative_raw and creative_raw not in CREATIVE_MODES:
        raise ValueError(
            f'creative_mode must be one of {"|".join(CREATIVE_MODES)}')
    creative_mode = normalise_creative_mode(creative_mode)
    max_pages = normalise_slide_page_count(max_pages)
    size = normalise_slide_size(size)
    key = (user_id, topic, lang, style, max_pages, size, model, creative_mode)
    # Fail before workdir creation, task registration, research, or LLM spend.
    from lib.slides.readiness import ensure_slides_runtime_ready
    ensure_slides_runtime_ready()
    tid = _slides_task_id()
    wd = os.path.join(slides_root(), 'jobs', tid)
    task, existing = _claim_slides_task(
        key, tid, topic=topic, workdir=wd, lang=lang, style=style,
        max_pages=max_pages, size=size, conv_id=conv_id, model=model,
        creative_mode=creative_mode, user_id=user_id)
    if existing:
        return {'task_id': existing, 'deduped': True}
    try:
        os.makedirs(wd, exist_ok=True)
    except Exception as exc:
        _slides_runtime.finish(tid, error=exc,
                               error_context='slides:create-workdir')
        raise
    _slides_runtime.spawn(tid, run_slides_task, task)
    logger.info('[Slides] started %s topic=%r lang=%s pages=%d mode=%s',
                tid, topic[:60], lang, max_pages, creative_mode)
    return {'task_id': tid, 'deduped': False}
