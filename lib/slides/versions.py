"""lib/slides/versions.py — deck version snapshots for conversational edits.

Every chat-driven mutation (patch / restyle / edit_page / batch edit) is
one user-visible STEP; making it cheap means making it undoable. Before
any of those paths mutates the deck, it snapshots the manifest + every
page source into ``deck/.versions/<n>/`` and appends one row to
``versions.json`` — bounded to the newest :data:`_MAX_VERSIONS`, LRU-pruned
(media files are deliberately NOT snapshotted: edits only ever re-point
references, so restoring the YAML restores the picture).

A revert restores the snapshot's files atomically, then re-renders every
preview and re-exports the PPTX ONCE — the same post-edit pipeline as the
forward paths, so a deck can never sit half-current.
"""

from __future__ import annotations

import os
import time

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['snapshot_deck', 'list_versions', 'revert_deck', 'MAX_VERSIONS']

MAX_VERSIONS = 12
_INDEX = 'versions.json'
_DIR = '.versions'


def _index_path(deck_dir: str) -> str:
    return os.path.join(deck_dir, _DIR, _INDEX)


def _read_index(deck_dir: str) -> list:
    from lib.json_store import read_json
    rows = read_json(_index_path(deck_dir), default=[])
    return [r for r in rows if isinstance(r, dict) and r.get('version')]


def _write_index(deck_dir: str, rows: list) -> None:
    from lib.json_store import write_json_atomic
    write_json_atomic(_index_path(deck_dir), rows, sort_keys=False)


def _deck_source_files(deck_dir: str) -> list:
    """Manifest + page sources (media excluded by design — see module doc)."""
    out = []
    manifest = os.path.join(deck_dir, 'deck.pptd')
    if os.path.isfile(manifest):
        out.append('deck.pptd')
    pages_dir = os.path.join(deck_dir, 'pages')
    try:
        for name in sorted(os.listdir(pages_dir)):
            if name.endswith('.page'):
                out.append(f'pages/{name}')
    except OSError:
        pass
    return out


def snapshot_deck(deck_dir: str, *, op: str, summary: str = '') -> int:
    """Snapshot the deck's current source files; returns the version number.

    NEVER raises (a snapshot failure must not block the user's edit — it
    degrades undoability, not the deck). Returns 0 when nothing was stored.
    """
    try:
        rows = _read_index(deck_dir)
        version = (max(int(r['version']) for r in rows) + 1) if rows else 1
        dest = os.path.join(deck_dir, _DIR, str(version))
        os.makedirs(dest, exist_ok=True)
        stored = []
        for rel in _deck_source_files(deck_dir):
            src = os.path.join(deck_dir, rel)
            target = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(src, 'rb') as fin, open(target, 'wb') as fout:
                fout.write(fin.read())
            stored.append(rel)
        if not stored:
            return 0
        rows.append({'version': version, 'op': str(op)[:80],
                     'summary': str(summary)[:400],
                     'files': stored, 'ts': round(time.time(), 3)})
        # LRU bound: drop the oldest snapshots beyond MAX_VERSIONS.
        while len(rows) > MAX_VERSIONS:
            stale = rows.pop(0)
            import shutil
            shutil.rmtree(os.path.join(
                deck_dir, _DIR, str(stale['version'])), ignore_errors=True)
        _write_index(deck_dir, rows)
        logger.info('[Slides:versions] snapshot v%d (%s, %d file(s))',
                    version, op, len(stored))
        return version
    except Exception as e:
        logger.warning('[Slides:versions] snapshot failed (%s) — the edit '
                       'continues without an undo point', e)
        return 0


def list_versions(deck_dir: str) -> list:
    """Public snapshot rows, newest first (version/op/summary/ts only)."""
    rows = _read_index(deck_dir)
    return [{'version': int(r['version']), 'op': str(r.get('op') or ''),
             'summary': str(r.get('summary') or ''),
             'ts': float(r.get('ts') or 0)}
            for r in reversed(rows)]


def revert_deck(deck_dir: str, version: int) -> dict:
    """Restore one snapshot, then re-render + re-export ONCE.

    NEVER raises. Returns ``{'ok', 'version', 'restored', 'errors',
    'pptx_path'}``; the snapshot itself is KEPT (revert is not destructive —
    the user can bounce between versions).
    """
    from lib.slides.pptd import parse_deck, validate_deck
    rows = _read_index(deck_dir)
    row = next((r for r in rows if int(r['version']) == int(version)), None)
    if row is None:
        known = [int(r['version']) for r in rows]
        return {'ok': False,
                'errors': [f'version {version} not found; available: '
                           f'{known or "none"}'],
                'available_versions': known}
    src_dir = os.path.join(deck_dir, _DIR, str(int(version)))
    restored = []
    try:
        for rel in row.get('files') or []:
            src = os.path.join(src_dir, rel)
            target = os.path.join(deck_dir, rel)
            if not os.path.isfile(src):
                return {'ok': False,
                        'errors': [f'snapshot file missing: {rel}']}
            os.makedirs(os.path.dirname(target), exist_ok=True)
            from lib.json_store import write_bytes_atomic
            with open(src, 'rb') as f:
                write_bytes_atomic(target, f.read())
            restored.append(rel)
        deck = parse_deck(os.path.join(deck_dir, 'deck.pptd'))
        findings = validate_deck(deck)
        if findings:
            return {'ok': False,
                    'errors': [f'restored deck fails validation: '
                               f'{findings[0]}']}
    except Exception as e:
        logger.debug('revert deck failed: %s', e)
        return {'ok': False, 'errors': [f'restore failed: {e}']}

    try:
        from lib.slides.render_png import render_previews
        render = render_previews(deck, os.path.join(deck_dir, 'preview'),
                                 scale=2.0)
        if render.get('failed'):
            logger.warning('[Slides:versions] %d preview(s) failed after '
                           'revert', len(render['failed']))
    except Exception as e:
        logger.warning('[Slides:versions] preview re-render failed: %s', e)
    import glob as _glob
    from lib.slides.export_pptx import export_pptx
    existing = _glob.glob(os.path.join(deck_dir, '*.pptx'))
    out_path = existing[0] if existing else os.path.join(deck_dir,
                                                         'deck.pptx')
    try:
        export_pptx(deck, out_path)
    except Exception as e:
        logger.debug('revert deck failed: %s', e)
        return {'ok': False,
                'errors': [f'export failed after restore: {e}'],
                'restored': restored}
    logger.info('[Slides:versions] reverted to v%d (%d file(s))',
                int(version), len(restored))
    return {'ok': True, 'version': int(version),
            'restored': restored, 'errors': [], 'pptx_path': out_path,
            'summary': str(row.get('summary') or '')}
