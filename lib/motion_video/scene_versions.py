"""lib/motion_video/scene_versions.py — per-scene composition snapshots.

The video-side twin of ``lib/slides/versions.py``: before an
instruction-driven rewrite touches a scene's ``index.html``, the current
composition is snapshotted into ``scenes/<id>/.versions/<n>.html`` with one
row in ``versions.json`` (bounded, LRU-pruned). A revert restores the file
atomically; the engine then re-renders ONLY that scene and re-assembles
the film — the same tail as a regen, so a revert can never leave the film
half-current.

Snapshots are COMPOSITION-ONLY by design: narration WAVs and the storyboard
are user data with their own durability contract, and a composition revert
never implies re-synthesizing speech.
"""

from __future__ import annotations

import os
import time

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['snapshot_scene', 'list_scene_versions', 'restore_scene_version',
           'MAX_SCENE_VERSIONS']

MAX_SCENE_VERSIONS = 8
_INDEX = 'versions.json'
_DIR = '.versions'


def _index_path(scene_dir: str) -> str:
    return os.path.join(scene_dir, _DIR, _INDEX)


def _read_index(scene_dir: str) -> list:
    from lib.json_store import read_json
    rows = read_json(_index_path(scene_dir), default=[])
    return [r for r in rows if isinstance(r, dict) and r.get('version')]


def snapshot_scene(scene_dir: str, *, op: str, summary: str = '') -> int:
    """Snapshot the scene's current index.html; returns the version number.

    NEVER raises (a snapshot failure degrades undoability, not the edit).
    Returns 0 when there was nothing to store.
    """
    try:
        index_html = os.path.join(scene_dir, 'index.html')
        if not os.path.isfile(index_html):
            return 0
        rows = _read_index(scene_dir)
        version = (max(int(r['version']) for r in rows) + 1) if rows else 1
        dest_dir = os.path.join(scene_dir, _DIR)
        os.makedirs(dest_dir, exist_ok=True)
        with open(index_html, 'rb') as fin:
            payload = fin.read()
        with open(os.path.join(dest_dir, f'{version}.html'), 'wb') as fout:
            fout.write(payload)
        rows.append({'version': version, 'op': str(op)[:80],
                     'summary': str(summary)[:400], 'bytes': len(payload),
                     'ts': round(time.time(), 3)})
        while len(rows) > MAX_SCENE_VERSIONS:
            stale = rows.pop(0)
            try:
                os.unlink(os.path.join(
                    dest_dir, f"{int(stale['version'])}.html"))
            except OSError:
                pass
        from lib.json_store import write_json_atomic
        write_json_atomic(_index_path(scene_dir), rows, sort_keys=False)
        logger.info('[MotionVideo:versions] scene snapshot v%d (%s)',
                    version, op)
        return version
    except Exception as e:
        logger.warning('[MotionVideo:versions] snapshot failed (%s) — the '
                       'edit continues without an undo point', e)
        return 0


def list_scene_versions(scene_dir: str) -> list:
    """Public snapshot rows, newest first (version/op/summary/ts)."""
    return [{'version': int(r['version']), 'op': str(r.get('op') or ''),
             'summary': str(r.get('summary') or ''),
             'ts': float(r.get('ts') or 0)}
            for r in reversed(_read_index(scene_dir))]


def restore_scene_version(scene_dir: str, version: int) -> tuple:
    """Restore one snapshot's composition over index.html.

    Returns ``(html, error)`` — the restored composition (caller re-renders)
    or an actionable error string. The snapshot itself is KEPT.
    """
    rows = _read_index(scene_dir)
    row = next((r for r in rows if int(r['version']) == int(version)), None)
    if row is None:
        known = [int(r['version']) for r in rows]
        return '', (f'version {version} not found; available: '
                    f'{known or "none"}')
    src = os.path.join(scene_dir, _DIR, f'{int(version)}.html')
    try:
        with open(src, 'rb') as f:
            payload = f.read()
    except OSError as e:
        return '', f'snapshot file missing: {e}'
    from lib.json_store import write_bytes_atomic
    write_bytes_atomic(os.path.join(scene_dir, 'index.html'), payload)
    logger.info('[MotionVideo:versions] scene %s restored to v%d',
                os.path.basename(scene_dir), int(version))
    return payload.decode('utf-8', errors='replace'), ''
