"""Strict resolution and validation for the prebuilt Vite application graph."""

from __future__ import annotations

import asyncio
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
VITE_AUTHORING_DIGEST_FIELD = 'tofuAuthoringSha256'
VITE_AUTHORING_INPUTS_PATH = os.path.join(
    BASE_DIR, 'frontend', 'authoring-inputs.json')
with open(VITE_AUTHORING_INPUTS_PATH, encoding='utf-8') as _authoring_file:
    _VITE_AUTHORING_INPUTS = json.load(_authoring_file)
if (not isinstance(_VITE_AUTHORING_INPUTS.get('configPaths'), list)
        or not isinstance(_VITE_AUTHORING_INPUTS.get('sourceSuffixes'), list)):
    raise RuntimeError('frontend/authoring-inputs.json has an invalid shape')
I18N_LOCALE_PATHS = tuple(
    os.path.join(BASE_DIR, 'frontend', 'src', 'i18n', 'locales', f'{language}.json')
    for language in ('zh', 'en')
)
VITE_AUTHORING_CONFIG_PATHS = tuple(
    os.path.join(BASE_DIR, *relative_path.split('/'))
    for relative_path in _VITE_AUTHORING_INPUTS['configPaths']
)
VITE_AUTHORING_SUFFIXES = frozenset(
    _VITE_AUTHORING_INPUTS['sourceSuffixes'])
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


def _source_vite_authoring_digest() -> str:
    """Hash every Vite build input with stable repository-relative framing."""
    digest = hashlib.sha256()
    digest.update(b'tofu-vite-authoring-v1\0')
    inputs = sorted(
        vite_authoring_inputs(),
        key=lambda path: os.path.relpath(path, BASE_DIR).replace(os.sep, '/'),
    )
    for path in inputs:
        relative_path = os.path.relpath(path, BASE_DIR).replace(os.sep, '/')
        digest.update(relative_path.encode('utf-8'))
        digest.update(b'\0')
        with open(path, 'rb') as handle:
            while True:
                chunk = handle.read(1 << 20)
                if not chunk:
                    break
                digest.update(chunk)
        digest.update(b'\0')
    return digest.hexdigest()


def _validate_vite_authoring_digest(
        manifest: dict, *, validate_authoring_sources: bool) -> None:
    main = manifest.get(VITE_ENTRY)
    value = main.get(VITE_AUTHORING_DIGEST_FIELD) \
        if isinstance(main, dict) else None
    if (not isinstance(value, str) or len(value) != 64
            or any(character not in '0123456789abcdef' for character in value)):
        raise ValueError('Vite manifest has no valid authoring-input digest')
    if not validate_authoring_sources:
        return
    source_root = os.path.join(BASE_DIR, 'frontend', 'src')
    if not os.path.isdir(source_root):
        # Minimal release images may contain only the committed artifact. Its
        # digest remains mandatory, while absence of all authoring sources is
        # not interpreted as drift.
        return
    if value != _source_vite_authoring_digest():
        raise ValueError(
            'Vite authoring inputs are stale; run npm run build:frontend')


def _load_manifest(
        entries: tuple[str, ...], *, validate_authoring_sources: bool) -> dict:
    with open(VITE_MANIFEST, encoding='utf-8') as handle:
        manifest = _validate_manifest(json.load(handle), entries)
    _validate_i18n_catalog_digest(
        manifest, validate_authoring_sources=validate_authoring_sources)
    _validate_vite_authoring_digest(
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
        manifest = _load_manifest(
            selected,
            validate_authoring_sources=validate_authoring_sources,
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ViteAssetError(f'required Vite artifact is invalid: {exc}') from exc
    _publish_vite_build_ids(manifest)
    return manifest


def validate_vite_artifact(entries: tuple[str, ...] | None = None) -> dict:
    """Validate a deployment graph, including checked-out locale freshness."""
    return _validate_vite_artifact(
        entries, validate_authoring_sources=True)


def vite_authoring_inputs() -> tuple[str, ...]:
    """Return the deterministic source/config inputs to the Vite publication."""
    inputs = [
        path for path in VITE_AUTHORING_CONFIG_PATHS
        if os.path.isfile(path)
    ]
    source_root = os.path.join(BASE_DIR, 'frontend', 'src')

    def _raise_walk_error(error: OSError) -> None:
        raise error

    for directory, child_directories, filenames in os.walk(
            source_root, topdown=True, onerror=_raise_walk_error,
            followlinks=False):
        child_directories.sort()
        for filename in sorted(filenames):
            if os.path.splitext(filename)[1].lower() \
                    not in VITE_AUTHORING_SUFFIXES:
                continue
            path = os.path.join(directory, filename)
            if os.path.isfile(path):
                inputs.append(path)
    return tuple(inputs)


def validate_source_vite_artifact(
        entries: tuple[str, ...] | None = None) -> dict:
    """Validate a source checkout's graph and authoring-content digest.

    Runtime requests deliberately consume the last atomically published graph.
    Lifecycle preflight is the separate boundary that may invoke Node, so it
    must reject an otherwise valid graph when any build input differs from the
    published generation and rebuild before the old worker is stopped. Content
    hashing remains correct when checkout/archive mtimes are rewritten.
    """
    return validate_vite_artifact(entries)


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
            # Every attached URL asset is part of the validated publication,
            # but only the small allowlist of first-paint fonts earns an HTML
            # preload. Data assets such as locale JSON remain fetch-on-demand.
            asset = _require_asset(bundled)
            if posixpath.splitext(asset)[1].lower() not in _FONT_MIME_TYPES:
                continue
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
    with _build_id_state_lock:
        _build_ids.clear()
        global _build_id_manifest_key
        _build_id_manifest_key = None


# Runtime request handlers read only this bounded snapshot. Full graph
# validation publishes it during the required frontend startup phase. A
# low-rate background refresh may replace it after an atomic manifest swap,
# but no HTTP/WebSocket event-loop path performs filesystem I/O.
_build_id_state_lock = threading.Lock()
_build_id_refresh_io_lock = threading.Lock()
_build_ids: dict[str, str] = {}
_build_id_manifest_key: tuple[int, int] | None = None
_build_id_refresh_pending = False


def _publish_vite_build_ids(
        manifest: dict, *, manifest_key: tuple[int, int] | None = None) -> None:
    published: dict[str, str] = {}
    for entry_name, manifest_entry in VITE_ENTRIES.items():
        row = manifest.get(manifest_entry)
        if not isinstance(row, dict):
            continue
        build_id = posixpath.basename(str(row.get('file') or ''))
        if build_id:
            published[entry_name] = build_id
    with _build_id_state_lock:
        _build_ids.clear()
        _build_ids.update(published)
        global _build_id_manifest_key
        _build_id_manifest_key = manifest_key


def get_vite_build_id(entry: str = 'main') -> str:
    """Basename of the currently-served entry bundle (``main-<hash>.js``).

    The browser compares this against the bundle IT was loaded with: a long-
    lived tab keeps running yesterday's JS until something tells it the disk
    moved on. Explicit health diagnostics and push pongs consume the startup-
    validated in-memory snapshot, so liveness never depends on FUSE/network
    filesystem latency. Returns '' in dev-server mode or before validation —
    the client then simply never reloads (fail-quiet: a missing build id must
    never cause a reload loop)."""
    if entry not in VITE_ENTRIES or _dev_server():
        return ''
    with _build_id_state_lock:
        return _build_ids.get(entry, '')


def refresh_vite_build_ids() -> str:
    """Refresh the validated snapshot from disk outside the serving loop.

    The I/O lock is deliberately held across ``stat`` and validation. A wedged
    network mount therefore consumes at most one executor worker instead of
    creating a cache-stampede of health/build probes. The last valid snapshot
    remains authoritative on every failure.
    """
    if _dev_server():
        return ''
    with _build_id_refresh_io_lock:
        try:
            stat = os.stat(VITE_MANIFEST)
            key = (stat.st_mtime_ns, stat.st_size)
            with _build_id_state_lock:
                if key == _build_id_manifest_key and _build_ids:
                    return _build_ids.get('main', '')
            manifest = _load_manifest(
                tuple(VITE_ENTRIES), validate_authoring_sources=False)
            _publish_vite_build_ids(manifest, manifest_key=key)
        except (OSError, ValueError, TypeError, json.JSONDecodeError,
                KeyError) as exc:
            logger.debug('[Vite] background build-id refresh unavailable: %s', exc)
        return get_vite_build_id('main')


def request_vite_build_id_refresh() -> bool:
    """Submit at most one background manifest refresh to the loop executor."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    with _build_id_state_lock:
        global _build_id_refresh_pending
        if _build_id_refresh_pending:
            return False
        _build_id_refresh_pending = True
    try:
        future = loop.run_in_executor(None, refresh_vite_build_ids)
    except Exception:
        with _build_id_state_lock:
            _build_id_refresh_pending = False
        return False

    def _settled(completed) -> None:
        with _build_id_state_lock:
            global _build_id_refresh_pending
            _build_id_refresh_pending = False
        try:
            completed.result()
        except Exception as exc:  # defensive: refresh itself is fail-quiet
            logger.debug('[Vite] build-id refresh executor failed: %s', exc)

    future.add_done_callback(_settled)
    return True


__all__ = [
    'I18N_CATALOG_DIGEST_FIELD', 'I18N_LOCALE_PATHS',
    'VITE_AUTHORING_DIGEST_FIELD',
    'VITE_AUTHORING_CONFIG_PATHS', 'VITE_AUTHORING_SUFFIXES',
    'VITE_ENTRIES', 'VITE_ENTRY', 'VITE_MANIFEST', 'VITE_OUT_DIR',
    'ViteAssetError', 'clear_vite_asset_cache', 'get_vite_asset_tags',
    'get_vite_build_id', 'refresh_vite_build_ids',
    'request_vite_build_id_refresh', 'validate_published_vite_artifact',
    'validate_source_vite_artifact', 'validate_vite_artifact',
    'vite_authoring_inputs',
]
