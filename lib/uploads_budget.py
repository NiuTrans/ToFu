"""Uploads directory budget and orphan lifecycle.

Responsibility: keep ``uploads/images/`` — the product's last unbounded
persistent artifact — inside the personal-computer resource budget.

Entry points:
  - ``check_quota`` — called by the upload route before persisting a file;
    rejects the upload once the directory would exceed
    ``TOFU_UPLOADS_QUOTA_MB`` (probe-derivable, default 2048 MiB).
  - ``sweep_orphans`` — moves image files no longer referenced by any
    conversation into ``.orphans/`` (quarantine, never silent deletion of
    user state), and deletes quarantined files only after a second grace
    window. The caller supplies the referenced-name set so this module
    stays storage-agnostic; wiring a periodic collector to the semantic
    storage catalog is the owning scheduler's job (TODO(enterprise)).

Dependencies: stdlib + lib.log. Durable user state is never deleted here
without a quarantine stage and a full grace window.
"""

from __future__ import annotations

import os
import time

from lib.log import get_logger

logger = get_logger(__name__)

DEFAULT_UPLOADS_QUOTA_MB = 2048
_ORPHAN_DIR = '.orphans'
_ORPHAN_GRACE_S = 30 * 24 * 3600
_QUARANTINE_DELETE_S = 30 * 24 * 3600


def uploads_quota_bytes() -> int:
    """Directory-wide quota; env override clamped to a sane floor/ceiling."""
    raw = (os.environ.get('TOFU_UPLOADS_QUOTA_MB') or '').strip()
    try:
        value = int(raw) if raw else DEFAULT_UPLOADS_QUOTA_MB
    except ValueError:
        value = DEFAULT_UPLOADS_QUOTA_MB
    value = max(64, min(value, 64 * 1024))
    return value * 1024 * 1024


def directory_size_bytes(path: str) -> int:
    """Best-effort recursive size; unreadable entries count as zero."""
    total = 0
    for directory, names, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(directory, name))
            except OSError:
                continue
    return total


def check_quota(images_dir: str, incoming_bytes: int) -> str | None:
    """Return an error message when the write would exceed the quota."""
    quota = uploads_quota_bytes()
    current = directory_size_bytes(images_dir)
    if current + max(0, int(incoming_bytes)) > quota:
        logger.warning(
            '[Uploads] quota exceeded: %d + %d > %d bytes',
            current, incoming_bytes, quota)
        return (
            f'Image storage is full ({current // (1024 * 1024)} of '
            f'{quota // (1024 * 1024)} MiB used) — free space or raise '
            'TOFU_UPLOADS_QUOTA_MB'
        )
    return None


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
