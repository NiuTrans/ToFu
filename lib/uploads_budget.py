"""Explicit orphan quarantine lifecycle for the uploads directory.

The caller supplies exact referenced names. Fresh and referenced files are
retained; old unreferenced files enter quarantine before grace-based expiry.
No directory-size quota or upload admission belongs to this lifecycle owner.
Dependencies: stdlib and lib.log. Entry point: sweep_orphans.
"""

from __future__ import annotations

import os
import time

from lib.log import get_logger

logger = get_logger(__name__)

_ORPHAN_DIR = '.orphans'
_ORPHAN_GRACE_S = 30 * 24 * 3600
_QUARANTINE_DELETE_S = 30 * 24 * 3600


def sweep_orphans(
    images_dir: str,
    referenced_names: set[str],
    *,
    now: float | None = None,
) -> dict[str, int]:
    """Quarantine unreferenced images, then expire the quarantine.

    A file is an orphan candidate only when it is absent from
    ``referenced_names`` AND older than the grace window — an in-flight
    upload that has not been persisted into a conversation yet is never
    touched. Quarantined files past the second grace window are deleted;
    that is the only deletion in this module and it follows a full
    quarantine stage, per the durable-state policy.
    """
    now = time.time() if now is None else now
    stats = {'scanned': 0, 'quarantined': 0, 'deleted': 0, 'errors': 0}
    quarantine_dir = os.path.join(images_dir, _ORPHAN_DIR)

    for name in os.listdir(quarantine_dir) if os.path.isdir(
            quarantine_dir) else ():
        path = os.path.join(quarantine_dir, name)
        try:
            if os.path.isfile(path) and (
                now - os.path.getmtime(path) > _QUARANTINE_DELETE_S
            ):
                os.unlink(path)
                stats['deleted'] += 1
        except OSError as exc:
            stats['errors'] += 1
            logger.debug('[Uploads] quarantine expiry failed for %s: %s',
                         name, exc)

    if not os.path.isdir(images_dir):
        return stats
    for name in os.listdir(images_dir):
        path = os.path.join(images_dir, name)
        if name == _ORPHAN_DIR or not os.path.isfile(path):
            continue
        stats['scanned'] += 1
        if name in referenced_names:
            continue
        try:
            age = now - os.path.getmtime(path)
        except OSError:
            continue
        if age < _ORPHAN_GRACE_S:
            continue
        try:
            os.makedirs(quarantine_dir, exist_ok=True)
            os.replace(path, os.path.join(quarantine_dir, name))
            stats['quarantined'] += 1
        except OSError as exc:
            stats['errors'] += 1
            logger.debug('[Uploads] orphan quarantine failed for %s: %s',
                         name, exc)
    if stats['quarantined'] or stats['deleted']:
        logger.info('[Uploads] orphan sweep: %s', stats)
    return stats
