"""Strict resolution and validation for the prebuilt Vite application graph."""

from __future__ import annotations

import html
import json
import os
import posixpath
import threading
import time
from urllib.parse import urlparse

from lib.log import get_logger


logger = get_logger(__name__)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VITE_OUT_DIR = os.path.join(BASE_DIR, 'static', 'vite')
VITE_MANIFEST = os.path.join(VITE_OUT_DIR, 'manifest.json')
VITE_ENTRIES = {
    'main': 'frontend/src/main.ts',
    'admin': 'frontend/src/admin.ts',
}
# Compatibility name for callers that only need the primary entry key.
VITE_ENTRY = VITE_ENTRIES['main']
_CACHE_TTL_SECONDS = 5.0
_cache_lock = threading.Lock()
_cache: dict[tuple[str, int, int], tuple[float, str]] = {}


class ViteAssetError(RuntimeError):
    """The required Vite artifact cannot be resolved safely."""


def _safe_asset_path(value: object, suffixes: tuple[str, ...] | None = None) -> str:
    path = str(value or '')
    if ('\\' in path or not path.startswith('assets/')
            or posixpath.normpath(path) != path
            or any(part in ('', '.', '..') for part in path.split('/'))
            or '?' in path or '#' in path):
        return ''
    if suffixes is not None and not path.lower().endswith(suffixes):
        return ''
    resolved = os.path.realpath(os.path.join(VITE_OUT_DIR, *path.split('/')))
    if os.path.commonpath((os.path.realpath(VITE_OUT_DIR), resolved)) != os.path.realpath(VITE_OUT_DIR):
        return ''
    return path


def _require_asset(value: object, suffixes: tuple[str, ...] | None = None) -> str:
    path = _safe_asset_path(value, suffixes)
    if not path:
        raise ValueError(f'unsafe Vite asset path {value!r}')
    full_path = os.path.join(VITE_OUT_DIR, *path.split('/'))
    if not os.path.isfile(full_path):
        raise ValueError(f'Vite asset is missing: {path!r}')
    return path


def _row_references(row: dict) -> tuple[str, ...]:
    references: list[str] = []
    for field in ('imports', 'dynamicImports'):
        values = row.get(field) or []
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise ValueError(f'Vite manifest field {field!r} must be a string array')
        references.extend(values)
    return tuple(references)


def _validate_manifest(manifest: object, entries: tuple[str, ...]) -> dict:
    if not isinstance(manifest, dict):
        raise ValueError('Vite manifest root must be an object')
    keys = tuple(VITE_ENTRIES[name] for name in entries)
    seen: set[str] = set()

    def visit(key: str) -> None:
        if key in seen:
            return
        row = manifest.get(key)
        if not isinstance(row, dict):
            raise ValueError(f'Vite manifest reference {key!r} is missing')
        seen.add(key)
        _require_asset(row.get('file'))
        for css in row.get('css') or ():
            _require_asset(css, ('.css',))
        for asset in row.get('assets') or ():
            _require_asset(asset)
        for reference in _row_references(row):
            visit(reference)

    for key in keys:
        row = manifest.get(key)
        if not isinstance(row, dict) or row.get('isEntry') is not True:
            raise ValueError(f'Vite manifest has no entry {key!r}')
        visit(key)
    # URL imports (for example the PDF.js worker) are represented by
    # standalone manifest rows rather than recursive import edges.
    for key in manifest:
        visit(key)
    return manifest


def _load_manifest(entries: tuple[str, ...]) -> dict:
    with open(VITE_MANIFEST, encoding='utf-8') as handle:
        return _validate_manifest(json.load(handle), entries)


def validate_vite_artifact(entries: tuple[str, ...] | None = None) -> dict:
    """Validate entry rows, recursive chunks, CSS, assets, paths, and files."""
    selected = tuple(VITE_ENTRIES) if entries is None else tuple(entries)
    unknown = set(selected) - set(VITE_ENTRIES)
    if unknown:
        raise ViteAssetError(f'unknown Vite entries: {sorted(unknown)!r}')
    try:
        return _load_manifest(selected)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ViteAssetError(f'required Vite artifact is invalid: {exc}') from exc


def _dev_server() -> str:
    raw = (os.environ.get('TOFU_VITE_DEV_SERVER') or '').strip().rstrip('/')
    if not raw:
        return ''
    parsed = urlparse(raw)
    if parsed.scheme not in ('http', 'https') or parsed.hostname not in (
            'localhost', '127.0.0.1', '::1'):
        logger.warning('[Vite] ignoring unsafe TOFU_VITE_DEV_SERVER=%r', raw)
        return ''
    return raw


def _dev_tags(server: str, entry_name: str) -> str:
    escaped = html.escape(server, quote=True)
    entry = html.escape(VITE_ENTRIES[entry_name], quote=True)
    return (
        f'<script type="module" src="{escaped}/@vite/client"></script>\n'
        f'<script type="module" src="{escaped}/{entry}"></script>'
    )


def _manifest_tags(manifest: dict, entry_name: str = 'main') -> str:
    entry_key = VITE_ENTRIES[entry_name]
    entry = manifest[entry_key]
    seen: set[str] = set()
    imports: list[str] = []
    styles: list[str] = []

    def visit(key: str) -> None:
        if key in seen:
            return
        seen.add(key)
        row = manifest[key]
        for imported in row.get('imports') or ():
            visit(imported)
        if key != entry_key:
            asset = _require_asset(row.get('file'), ('.js', '.mjs'))
            if asset not in imports:
                imports.append(asset)
        for css in row.get('css') or ():
            asset = _require_asset(css, ('.css',))
            if asset not in styles:
                styles.append(asset)

    visit(entry_key)
    source = _require_asset(entry.get('file'), ('.js', '.mjs'))
    tags = [
        f'<link rel="stylesheet" href="static/vite/{html.escape(css, quote=True)}">'
        for css in styles
    ]
    tags.extend(
        f'<link rel="modulepreload" href="static/vite/{html.escape(asset, quote=True)}">'
        for asset in imports
    )
    tags.append(
        f'<script type="module" src="static/vite/{html.escape(source, quote=True)}"></script>')
    return '\n'.join(tags)


def get_vite_asset_tags(entry: str = 'main') -> str:
    """Return safe tags for ``main`` or ``admin``, or fail closed."""
    if entry not in VITE_ENTRIES:
        raise ViteAssetError(f'unknown Vite entry {entry!r}')
    server = _dev_server()
    if server:
        return _dev_tags(server, entry)
    try:
        stat = os.stat(VITE_MANIFEST)
    except OSError as exc:
        raise ViteAssetError(
            f'required Vite manifest is unavailable: {VITE_MANIFEST}') from exc
    key = (entry, stat.st_mtime_ns, stat.st_size)
    now = time.monotonic()
    cached = _cache.get(key)
    if cached and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]
    with _cache_lock:
        cached = _cache.get(key)
        if cached and now - cached[0] < _CACHE_TTL_SECONDS:
            return cached[1]
        try:
            manifest = _load_manifest((entry,))
            tags = _manifest_tags(manifest, entry)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ViteAssetError(f'required Vite artifact is invalid: {exc}') from exc
        _cache.clear()
        _cache[key] = (now, tags)
        return tags


def clear_vite_asset_cache() -> None:
    with _cache_lock:
        _cache.clear()


__all__ = [
    'VITE_ENTRIES', 'VITE_ENTRY', 'VITE_MANIFEST', 'VITE_OUT_DIR',
    'ViteAssetError', 'clear_vite_asset_cache', 'get_vite_asset_tags',
    'validate_vite_artifact',
]
