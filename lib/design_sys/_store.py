"""lib/design_sys/_store.py — content-addressed asset store for design assets.

Self-contained on purpose. ``lib.motion_video._assets`` already implements the
same idea, but it lives under the motion root and answers to motion's
docstring contracts; design assets (fonts today, shared textures tomorrow)
belong to BOTH capabilities, so they get their own library at
``data_root()/design_sys/`` and must not import motion_video (the dependency
direction is motion_video → design_sys, never the reverse).

Same three-tier materialisation discipline as motion's store (hardlink →
symlink → copy, never predicted — some FUSE mounts answer ``os.link`` with
EPERM even on same-device paths), same atomic-write rule, same extension whitelist.
"""

from __future__ import annotations

import hashlib
import os
import shutil

from lib.json_store import write_bytes_atomic
from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['store_root', 'store_bytes', 'materialise', 'AssetStoreError',
           'ALLOWED_EXTENSIONS']

ALLOWED_EXTENSIONS = frozenset({
    '.otf', '.ttf', '.woff', '.woff2',
    '.png', '.jpg', '.jpeg', '.webp', '.svg',
    '.txt', '.md',                                   # license evidence files
})


class AssetStoreError(Exception):
    """Refused or failed asset-store operation."""


def store_root() -> str:
    from lib.runtime_paths import data_root
    path = os.path.join(data_root(), 'design_sys')
    os.makedirs(path, exist_ok=True)
    return path


def _ext_of(name: str) -> str:
    ext = os.path.splitext((name or '').strip().lower())[1]
    if not name or not ext:
        raise AssetStoreError(f'refusing an asset with no extension: {name!r}')
    if ext not in ALLOWED_EXTENSIONS:
        raise AssetStoreError(
            f'refusing asset extension {ext!r} — allowed: '
            + ', '.join(sorted(ALLOWED_EXTENSIONS)))
    return ext


def store_bytes(data: bytes, *, name: str, subdir: str = '',
                sha256: str = '') -> str:
    """Store ``data`` content-addressed; return the path. Verifies ``sha256``
    when given — a font whose bytes drifted from the audited pin must never
    reach a composition.
    """
    if not data:
        raise AssetStoreError('refusing to store an empty asset')
    ext = _ext_of(name)
    digest = hashlib.sha256(data).hexdigest()
    if sha256 and digest != sha256:
        raise AssetStoreError(
            f'sha256 mismatch for {name}: got {digest[:16]}…, want '
            f'{sha256[:16]}… — refusing to store bytes that were not audited')
    dest_dir = os.path.join(store_root(), subdir) if subdir else store_root()
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, f'{digest[:20]}{ext}')
    if os.path.isfile(dest) and os.path.getsize(dest) == len(data):
        return dest
    write_bytes_atomic(dest, data)
    logger.info('[DesignStore] stored %s (%d bytes)', name, len(data))
    return dest


def materialise(library_path: str, dest_dir: str, *, name: str = '') -> tuple[str, str]:
    """Link a stored asset into ``dest_dir``; return ``(rel_path, tier)``.

    ``rel_path`` is dest-dir-relative (``assets/<name>``) so renderers with a
    project-root containment rule accept it verbatim.
    """
    if not os.path.isfile(library_path):
        raise AssetStoreError(f'library asset missing: {library_path}')
    target_dir = os.path.join(dest_dir, 'assets')
    os.makedirs(target_dir, exist_ok=True)
    base = name or os.path.basename(library_path)
    _ext_of(base)
    dest = os.path.join(target_dir, base)
    rel = os.path.join('assets', base)
    if os.path.lexists(dest):
        if os.path.isfile(dest):
            return rel, 'existing'
        os.unlink(dest)
    for tier, fn in (('hardlink', os.link),
                     ('symlink', os.symlink),
                     ('copy', shutil.copy2)):
        try:
            fn(library_path, dest)
        except OSError as e:
            logger.info('[DesignStore] %s failed for %s (%s) — next tier',
                        tier, base, e.__class__.__name__)
            continue
        if not os.path.isfile(dest):
            try:
                os.unlink(dest)
            except OSError as e:
                logger.debug('[DesignStore] cleanup of unusable %s: %s',
                             dest, e)
            continue
        return rel, tier
    raise AssetStoreError(
        f'could not materialise {library_path} into {dest_dir}')
