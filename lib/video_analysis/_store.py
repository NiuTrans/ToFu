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

import os
import re
import time

from lib.identity import require_user_id
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


def create_record(video_id: str, *, name: str, size_bytes: int,
                  user_id: int) -> dict:
    """Insert a fresh ``processing`` record and lazily prune expired ones."""
    owner_user_id = require_user_id(user_id, context='video record')
    record = {
        'video_id': video_id,
        'user_id': owner_user_id,
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


def get_record(video_id: str, *, user_id: int) -> dict | None:
    """Return the record for ``video_id`` (or None). Never raises.

    Lazily flips stale ``processing`` records to ``failed`` (crash sweep)."""
    owner_user_id = require_user_id(user_id, context='video record lookup')
    from lib.json_store import read_json
    try:
        reg = read_json(_registry_path(), default={})
    except Exception as e:
        logger.warning('[VideoStore] read failed: %s', e)
        return None
    rec = reg.get(video_id) if isinstance(reg, dict) else None
    if (not isinstance(rec, dict)
            or int(rec.get('user_id') or 0) != owner_user_id):
        return None
    if (rec.get('status') == 'processing'
            and _now() - rec.get('updated_at', 0) > STALE_PROCESSING_S):
        logger.warning('[VideoStore] %s stale in processing — swept to failed', video_id)
        fail_record(video_id, 'processing interrupted (server restarted)')
        rec = dict(rec, status='failed',
                   error='processing interrupted (server restarted)')
    return rec


_VIDEO_FILENAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}')


def _asset_owner_manifest_path(filename: str) -> str:
    from lib.video_analysis._pipeline import videos_dir

    return os.path.join(videos_dir(), f'{filename}.owner.json')


def register_video_asset(
    filename: str,
    *,
    video_id: str,
    user_id: int,
) -> None:
    """Persist the principal allowed to read one durable original video.

    The manifest sits beside the binary and shares its lifecycle.  This keeps
    authorization available after the short-lived processing registry entry
    expires without introducing an unbounded process-global ownership map.
    """
    owner_user_id = require_user_id(user_id, context='video asset')
    if (os.path.basename(filename) != filename
            or not _VIDEO_FILENAME.fullmatch(filename)):
        raise ValueError('video asset filename is invalid')
    from lib.json_store import write_json_atomic

    write_json_atomic(_asset_owner_manifest_path(filename), {
        'schema': 'tofu.video-asset-owner/v1',
        'video_id': str(video_id),
        'filename': filename,
        'user_id': owner_user_id,
        'created_at': _now(),
    })


def resolve_owned_video_asset(filename: str, *, user_id: int) -> str:
    """Return an owner's durable video path, else ``''`` (default deny)."""
    owner_user_id = require_user_id(user_id, context='video asset lookup')
    if (os.path.basename(filename) != filename
            or not _VIDEO_FILENAME.fullmatch(filename)):
        return ''
    from lib.json_store import read_json
    from lib.video_analysis._pipeline import videos_dir

    manifest = read_json(_asset_owner_manifest_path(filename), default=None)
    if (not isinstance(manifest, dict)
            or manifest.get('schema') != 'tofu.video-asset-owner/v1'
            or manifest.get('filename') != filename
            or int(manifest.get('user_id') or 0) != owner_user_id):
        return ''
    path = os.path.join(videos_dir(), filename)
    return path if os.path.isfile(path) else ''


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
