"""Strict resolution and validation for the prebuilt Vite application graph."""

from __future__ import annotations

import hashlib
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
I18N_CATALOG_DIGEST_FIELD = 'tofuI18nCatalogSha256'
I18N_LOCALE_PATHS = tuple(
    os.path.join(BASE_DIR, 'frontend', 'src', 'i18n', 'locales', f'{language}.json')
    for language in ('zh', 'en')
)
_CACHE_TTL_SECONDS = 5.0
_cache_lock = threading.Lock()
_cache: dict[tuple[object, ...], tuple[float, str]] = {}


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


def _source_i18n_catalog_digest() -> str:
    """Hash locale source bytes with the same versioned framing as Node."""
    digest = hashlib.sha256()
    digest.update(b'tofu-i18n-catalog-v1\0')
    for language, path in zip(('zh', 'en'), I18N_LOCALE_PATHS):
        digest.update(language.encode('ascii'))
        digest.update(b'\0')
        with open(path, 'rb') as handle:
            digest.update(handle.read())
        digest.update(b'\0')
    return digest.hexdigest()


def _validate_i18n_catalog_digest(
        manifest: dict, *, validate_authoring_sources: bool) -> None:
    """Validate the published digest and, at deployment boundaries, sources.

    Request serving is defined by the atomically published manifest.  Source
    files are authoring inputs, so an edit made after startup must not withdraw
    the last valid graph from users who hard-refresh the running application.
    """
    main = manifest.get(VITE_ENTRY)
    value = main.get(I18N_CATALOG_DIGEST_FIELD) if isinstance(main, dict) else None
    if (not isinstance(value, str) or len(value) != 64
            or any(character not in '0123456789abcdef' for character in value)):
        raise ValueError('Vite manifest has no valid i18n catalog digest')
    if not validate_authoring_sources:
        return
    present = tuple(os.path.isfile(path) for path in I18N_LOCALE_PATHS)
    if any(present) and not all(present):
        raise ValueError('frontend i18n locale sources are incomplete')
    if all(present) and value != _source_i18n_catalog_digest():
        raise ValueError(
            'Vite i18n chunks are stale; run npm run build:frontend')


def _load_manifest(
        entries: tuple[str, ...], *, validate_authoring_sources: bool) -> dict:
    with open(VITE_MANIFEST, encoding='utf-8') as handle:
        manifest = _validate_manifest(json.load(handle), entries)
    _validate_i18n_catalog_digest(
        manifest, validate_authoring_sources=validate_authoring_sources)
    return manifest


def _validate_vite_artifact(
        entries: tuple[str, ...] | None, *,
        validate_authoring_sources: bool) -> dict:
    selected = tuple(VITE_ENTRIES) if entries is None else tuple(entries)
    unknown = set(selected) - set(VITE_ENTRIES)
    if unknown:
        raise ViteAssetError(f'unknown Vite entries: {sorted(unknown)!r}')
    try:
        return _load_manifest(
            selected,
            validate_authoring_sources=validate_authoring_sources,
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ViteAssetError(f'required Vite artifact is invalid: {exc}') from exc


def validate_vite_artifact(entries: tuple[str, ...] | None = None) -> dict:
    """Validate a deployment graph, including checked-out locale freshness."""
    return _validate_vite_artifact(
        entries, validate_authoring_sources=True)


def validate_published_vite_artifact(
        entries: tuple[str, ...] | None = None) -> dict:
    """Validate the atomic runtime graph without consulting authoring sources.

    A published manifest is the request-serving commit point. Runtime recovery
    must therefore accept the same complete graph that a hard refresh already
    consumes, even when source files were edited after publication. Explicit
    lifecycle/deployment checks use :func:`validate_vite_artifact` instead.
    """
    return _validate_vite_artifact(
        entries, validate_authoring_sources=False)


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


# Basenames (pre-hash) of the self-hosted font binaries that earn a first-paint
# preload: the Plus Jakarta Sans UI weights (400/600/700) used by the toolbar
# and controls. Deliberately not 300/500, JetBrains Mono, or Pixelify — exactly
# the set the legacy shell preloaded from static/vendor/fonts/. Vite renames
# assets to ``<name>-<hash><ext>``, so match on the pre-hash basename.
_FONT_PRELOAD_BASENAMES = frozenset({
    'LDIbaomQNQcsA88c7O9yZ4KMCoOg4IA6-91aHEjcWuA_qU7NSg',
    'LDIbaomQNQcsA88c7O9yZ4KMCoOg4IA6-91aHEjcWuA_d0nNSg',
    'LDIbaomQNQcsA88c7O9yZ4KMCoOg4IA6-91aHEjcWuA_TknNSg',
})
_FONT_MIME_TYPES = {
    '.woff2': 'font/woff2',
    '.woff': 'font/woff',
    '.ttf': 'font/ttf',
    '.otf': 'font/otf',
}


def _font_preload_basename(asset: str) -> str:
    name = posixpath.basename(asset)
    stem, dot, _suffix = name.rpartition('.')
    if not dot:
        return ''
    base, dash, _hash = stem.rpartition('-')
    return base if dash else stem


def _manifest_tags(manifest: dict, entry_name: str = 'main') -> str:
    entry_key = VITE_ENTRIES[entry_name]
    entry = manifest[entry_key]
    seen: set[str] = set()
    imports: list[str] = []
    styles: list[str] = []
    fonts: list[str] = []

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
        for bundled in row.get('assets') or ():
            asset = _require_asset(bundled, tuple(_FONT_MIME_TYPES))
            if _font_preload_basename(asset) not in _FONT_PRELOAD_BASENAMES:
                continue
            if asset not in fonts:
                fonts.append(asset)

    visit(entry_key)
    source = _require_asset(entry.get('file'), ('.js', '.mjs'))
    tags = []
    for font in fonts:
        mime = _FONT_MIME_TYPES[posixpath.splitext(font)[1].lower()]
        tags.append(
            f'<link rel="preload" as="font" type="{mime}" crossorigin '
            f'href="static/vite/{html.escape(font, quote=True)}">')
    tags.extend(
        f'<link rel="stylesheet" href="static/vite/{html.escape(css, quote=True)}">'
        for css in styles
    )
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
            manifest = _load_manifest(
                (entry,), validate_authoring_sources=False)
            tags = _manifest_tags(manifest, entry)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ViteAssetError(f'required Vite artifact is invalid: {exc}') from exc
        _cache.clear()
        _cache[key] = (now, tags)
        return tags


def clear_vite_asset_cache() -> None:
    with _cache_lock:
        _cache.clear()


# Stat-keyed cache for get_vite_build_id — same TTL discipline as the tags
# cache above (a rebuild swaps the manifest mtime, which mints a fresh key).
_build_id_cache: dict[tuple[object, ...], tuple[float, str]] = {}


def get_vite_build_id(entry: str = 'main') -> str:
    """Basename of the currently-served entry bundle (``main-<hash>.js``).

    The browser compares this against the bundle IT was loaded with: a long-
    lived tab keeps running yesterday's JS until something tells it the disk
    moved on, and ``/api/health`` is that channel. Returns '' in dev-server
    mode or on any manifest problem — the client then simply never reloads
    (fail-quiet: a missing build id must never cause a reload loop)."""
    if entry not in VITE_ENTRIES or _dev_server():
        return ''
    try:
        stat = os.stat(VITE_MANIFEST)
    except OSError:
        return ''
    key = (stat.st_mtime_ns, stat.st_size)
    now = time.monotonic()
    cached = _build_id_cache.get(key)
    if cached and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]
    build_id = ''
    try:
        manifest = _load_manifest(
            (entry,), validate_authoring_sources=False)
        build_id = posixpath.basename(
            str(manifest[VITE_ENTRIES[entry]].get('file') or ''))
    except (OSError, ValueError, TypeError, json.JSONDecodeError,
            KeyError) as exc:
        logger.debug('[Vite] build id unavailable: %s', exc)
        build_id = ''
    _build_id_cache.clear()
    _build_id_cache[key] = (now, build_id)
    return build_id


__all__ = [
    'I18N_CATALOG_DIGEST_FIELD', 'I18N_LOCALE_PATHS',
    'VITE_ENTRIES', 'VITE_ENTRY', 'VITE_MANIFEST', 'VITE_OUT_DIR',
    'ViteAssetError', 'clear_vite_asset_cache', 'get_vite_asset_tags',
    'get_vite_build_id', 'validate_published_vite_artifact',
    'validate_vite_artifact',
]
