"""lib/paper/podcast_runtime.py — TaskRuntime for paper-podcast tasks.

Rides :class:`lib.production.runtime.ProductionRuntime` — the dedup index,
create-with-field-shape, append+touch, stale sweep and id minting that used to
be hand-rolled here now live in the substrate (P6, driven by the P7
shared lifecycle contract in docs/modules/production.md).

Background podcast generation (report → script → TTS → audio) with a
dedup-by-(paper_hash, mode, lang, voice, model) index so a second request
for the same podcast joins the in-flight task instead of regenerating —
and a request for a DIFFERENT model never silently joins a task the user
didn't ask for (cache-key-skew family). Events:
status / delta / done / error / aborted, plus the podcast-specific
``script`` / ``segment_done`` / ``audio_ready`` events the engine emits.
"""

from __future__ import annotations

from lib.log import get_logger
from lib.production.runtime import ProductionRuntime

logger = get_logger(__name__)

_production = ProductionRuntime(
    'paper-podcast', id_prefix='podcast', ttl=3600, push_channel='paper',
    error_source='routes.paper:podcast', log_label='Paper:Podcast',
    stall_timeout=120)

#: The underlying TaskRuntime discovered by the generic task API.
_podcast_runtime = _production.runtime


def _podcast_index_get(
    paper_hash, mode, lang, voice, model='', *, user_id: int,
):
    """Return a live task_id for the dedup key, pruning stale entries."""
    return _production.index_get(
        (user_id, paper_hash, mode, lang, voice, model or ''))


def _podcast_task_shape(paper_hash, mode, lang, voice, model):
    return {
        'meta': {'paper_hash': paper_hash, 'mode': mode, 'lang': lang,
              'voice': voice, 'model': model},
        'fields': {
            'paper_hash': paper_hash,
            'mode': mode,
            'lang': lang,
            'voice': voice,
            'model': model,
            'script': None,
            'script_meta': None,
            'audio_url': '',
            'duration_sec': 0.0,
            'script_only': False,
            'progress': {'done': 0, 'total': 0},
        },
    }


def _claim_podcast_task(paper_hash, mode, lang, voice, model, *, user_id: int):
    """Atomically join or create the owner/model-specific production task."""
    return _production.claim_task(
        (user_id, paper_hash, mode, lang, voice, model or ''),
        _production.new_task_id(), user_id=user_id,
        **_podcast_task_shape(paper_hash, mode, lang, voice, model))


def _append_podcast_event(task, event):
    """Append one event (monotonic seq + WS push, like the report worker)."""
    return _production.append_event(task, event)


def _cleanup_stale_podcast_tasks():
    """Drop finished/error/aborted tasks past TTL; prune dedup entries."""
    return _production.cleanup_stale()


__all__ = [
    '_production',
    '_podcast_runtime',
    '_podcast_index_get',
    '_claim_podcast_task',
    '_append_podcast_event',
    '_cleanup_stale_podcast_tasks',
]
