"""Podcast admission: report gate, owned cache, atomic claim and worker submit.

The HTTP adapter supplies validated fields and an explicit owner. TaskRuntime
owns cancellation and settlement; the worker owns script/audio publication.
Importing this service does not import the recipe or any provider.
"""

from __future__ import annotations


def start_podcast(paper_hash: str, *, mode: str, lang: str, voice: str,
                  model: str | None, force: bool, user_id: int) -> dict:
    """Join live work, reuse a matching cache, or submit one owned podcast.

    Force bypasses a completed cache, never a matching live task. A requested
    model must match the cache's making-model; omitted models accept old rows.
    """
    from lib.paper.podcast_engine.worker import (
        has_report, load_cached_podcast, podcast_audio_url, run_podcast_task,
    )
    from lib.paper.podcast_runtime import (
        _claim_podcast_task, _cleanup_stale_podcast_tasks,
        _podcast_index_get, _podcast_runtime,
    )

    _cleanup_stale_podcast_tasks()
    if not has_report(paper_hash, user_id=user_id):
        return {
            'ok': False, 'report_required': True, 'report_lang': lang,
            'error': 'a report is required before a podcast can be generated',
        }
    from lib import tts
    voice = voice or tts.default_voice()
    existing = _podcast_index_get(
        paper_hash, mode, lang, voice, model, user_id=user_id)
    if existing:
        return {'ok': True, 'task_id': existing, 'reused': True}

    cached = load_cached_podcast(paper_hash, mode, lang, voice, user_id=user_id)
    if cached and not force and not (model and (cached.get('model') or '') != model):
        status = cached.get('status') or ''
        return {
            'ok': True, 'cached': True, 'status': status,
            'script': cached.get('script_json') or {},
            'meta': cached.get('meta') or {},
            'scriptOnly': status == 'script_only',
            'model': cached.get('model') or '',
            'audioUrl': (podcast_audio_url(paper_hash, mode, lang, voice)
                         if status == 'done' else ''),
            'durationSec': cached.get('duration_sec') or 0,
        }

    task, existing = _claim_podcast_task(
        paper_hash, mode, lang, voice, model, user_id=user_id)
    if existing:
        return {'ok': True, 'task_id': existing, 'reused': True}
    task_id = task['task_id']
    try:
        _podcast_runtime.spawn(task_id, run_podcast_task, task)
    except Exception as exc:
        _podcast_runtime.finish(task_id, error=exc,
                                error_context='paper-podcast:start')
        raise
    return {'ok': True, 'task_id': task_id}
