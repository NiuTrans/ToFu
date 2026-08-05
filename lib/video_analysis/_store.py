"""lib/video_analysis/_store.py — the video-processing registry.

One JSON document (``<data_root>/video_analysis.json``) mapping
``video_id → record``. The record is the LIVE status the polling endpoint
serves; it is NOT the durable payload — once processing finishes, the
frontend embeds the full frame list + transcript into the conversation
message itself (the same pattern as ``images[]``), so conversations keep
working after the registry entry is pruned.

All writes go through :func:`lib.json_store.update_json_atomic` (per-path
lock + atomic rename), so the background pipeline thread and the polling
route can never tear a write.
"""

from __future__ import annotations

import time

from lib.json_store import update_json_atomic
from lib.log import get_logger
from lib.runtime_paths import data_root

from lib.video_analysis._config import RECORD_TTL_S

logger = get_logger(__name__)

#: Processing phases, in order — the frontend progress chip renders these.
PHASES = ('probe', 'persist', 'frames', 'storyboard', 'audio', 'done')


def _registry_path() -> str:
    import os
    return os.path.join(data_root(), 'video_analysis.json')


def _now() -> float:
    return time.time()


def create_record(video_id: str, *, name: str, size_bytes: int) -> dict:
    """Insert a fresh ``processing`` record and lazily prune expired ones."""
    record = {
        'video_id': video_id,
        'name': name,
        'size_bytes': size_bytes,
        'status': 'processing',
        'phase': 'probe',
        'error': '',
        'created_at': _now(),
        'updated_at': _now(),
    }

    def _mutate(reg):
        if not isinstance(reg, dict):
            reg = {}
        cutoff = _now() - RECORD_TTL_S
        stale = [k for k, v in reg.items()
                 if isinstance(v, dict) and v.get('updated_at', 0) < cutoff]
        for k in stale:
            reg.pop(k, None)
        if stale:
            logger.info('[VideoStore] pruned %d expired record(s)', len(stale))
        reg[video_id] = record
        return reg

    update_json_atomic(_registry_path(), _mutate, default={})
    logger.info('[VideoStore] created record %s (%s, %d bytes)',
                video_id, name, size_bytes)
    return record


#: A record still in ``processing`` this long after its last update means the
#: server died mid-pipeline (daemon threads don't survive a restart) — the
#: status endpoint reports it as failed instead of spinning forever.
STALE_PROCESSING_S = 30 * 60


def get_record(video_id: str) -> dict | None:
    """Return the record for ``video_id`` (or None). Never raises.

    Lazily flips stale ``processing`` records to ``failed`` (crash sweep)."""
    from lib.json_store import read_json
    try:
        reg = read_json(_registry_path(), default={})
    except Exception as e:
        logger.warning('[VideoStore] read failed: %s', e)
        return None
    rec = reg.get(video_id) if isinstance(reg, dict) else None
    if not isinstance(rec, dict):
        return None
    if (rec.get('status') == 'processing'
            and _now() - rec.get('updated_at', 0) > STALE_PROCESSING_S):
        logger.warning('[VideoStore] %s stale in processing — swept to failed', video_id)
        fail_record(video_id, 'processing interrupted (server restarted)')
        rec = dict(rec, status='failed',
                   error='processing interrupted (server restarted)')
    return rec


def update_record(video_id: str, **fields) -> None:
    """Merge ``fields`` into the record (bumps ``updated_at``). Never raises —
    a status-update failure must not kill the pipeline thread."""
    try:
        def _mutate(reg):
            if not isinstance(reg, dict):
                reg = {}
            rec = reg.setdefault(video_id, {'video_id': video_id})
            rec.update(fields)
            rec['updated_at'] = _now()
            return reg

        update_json_atomic(_registry_path(), _mutate, default={})
    except Exception as e:
        logger.error('[VideoStore] update %s failed: %s', video_id, e, exc_info=True)


def set_phase(video_id: str, phase: str) -> None:
    update_record(video_id, phase=phase)


def fail_record(video_id: str, error: str) -> None:
    logger.warning('[VideoStore] %s failed: %s', video_id, error)
    update_record(video_id, status='failed', error=error)


def complete_record(video_id: str, **payload) -> None:
    update_record(video_id, status='ready', phase='done', **payload)
