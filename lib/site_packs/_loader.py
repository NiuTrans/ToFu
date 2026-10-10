"""Three-layer site pack loading with hot reload.

Layers, later overriding by pack id:

    lib/site_packs/data/            core public packs, shipped with the package
    bundles/<name>/site_packs/      release-tiered bundles per export mode
    <config_path>/site_packs/       user-tier drop-ins

The catalog is hot-reloaded: a directory signature (path + mtime) gates a
full re-parse, so steady-state calls cost one directory listing. A malformed
or transiently invalid drop-in is skipped with a warning and never takes the
rest of the registry down. This module stays free of lib.browser imports so
any consumer (auth_sources, tools) can load packs without the browser stack.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from lib.config_dir import config_path
from lib.log import get_logger

from ._manifest import ManifestError, PackManifest, parse_manifest

logger = get_logger(__name__)

_REPO_ROOT = Path(__file__).parents[2]
_CORE_DIR = Path(__file__).parent / 'data'
_BUNDLES_DIR = _REPO_ROOT / 'bundles'

#: Registry bounds. Packs are onboarding/discovery surface; unbounded growth
#: would leak straight into generated tool descriptions.
MAX_PACKS = 64
MAX_PACK_FILE_BYTES = 262_144

_lock = threading.RLock()
_cache: dict[str, PackManifest] = {}
_cache_signature: tuple | None = None


def _layer_directories() -> tuple[Path, ...]:
    """Pack directories in override order (later wins by pack id)."""
    layers = [_CORE_DIR]
    if _BUNDLES_DIR.is_dir():
        try:
            layers.extend(
                child / 'site_packs'
                for child in sorted(_BUNDLES_DIR.iterdir())
                if child.is_dir() and (child / 'site_packs').is_dir()
            )
        except OSError as exc:
            logger.warning('[SitePacks] bundles scan degraded: %s', exc)
    layers.append(Path(config_path('site_packs')))
    return tuple(layers)


def _signature(directories: tuple[Path, ...]) -> tuple:
    entries = []
    for directory in directories:
        if not directory.is_dir():
            continue
        try:
            for path in sorted(directory.glob('*.json')):
                try:
                    stat = path.stat()
                    # mtime granularity varies by filesystem (network mounts
                    # may quantize to seconds); size disambiguates same-tick
                    # edits so hot reload never serves a stale pack.
                    entries.append((str(path), stat.st_mtime_ns, stat.st_size))
                except OSError:
                    entries.append((str(path), -1, -1))
        except OSError as exc:
            logger.warning('[SitePacks] pack directory unreadable %s: %s',
                           directory, exc)
    return tuple(entries)


def _parse_file(path: Path) -> PackManifest | None:
    source = str(path)
    try:
        if path.stat().st_size > MAX_PACK_FILE_BYTES:
            logger.warning('[SitePacks] skipping oversized pack %s', path.name)
            return None
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning('[SitePacks] skipping unreadable pack %s: %s',
                       path.name, exc)
        return None
    try:
        return parse_manifest(raw, source=source)
    except ManifestError as exc:
        logger.warning('[SitePacks] skipping invalid pack %s: %s',
                       path.name, exc)
        return None


def load_packs() -> dict[str, PackManifest]:
    """Return the merged pack registry (id → manifest), hot-reloaded."""
    global _cache, _cache_signature
    directories = _layer_directories()
    signature = _signature(directories)
    with _lock:
        if signature == _cache_signature:
            return dict(_cache)
        packs: dict[str, PackManifest] = {}
        for directory in directories:
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob('*.json')):
                manifest = _parse_file(path)
                if manifest is None:
                    continue
                if manifest.id in packs:
                    logger.debug('[SitePacks] pack %s overridden by %s',
                                 manifest.id, path.name)
                packs[manifest.id] = manifest
        if len(packs) > MAX_PACKS:
            logger.warning('[SitePacks] pack count %d exceeds %d; truncating',
                           len(packs), MAX_PACKS)
            packs = dict(sorted(packs.items())[:MAX_PACKS])
        _cache = packs
        _cache_signature = signature
        return dict(packs)


def pack_auth_catalog() -> list[dict]:
    """Project pack auth sections into the auth_sources catalog shape.

    One entry per catalog domain: when two packs share a login session (an
    SSO parent domain), the later layer's projection wins the domain.
    """
    by_domain: dict[str, dict] = {}
    for pack in load_packs().values():
        auth = pack.auth
        if auth is None or not auth.project:
            continue
        domain = auth.domain or pack.domains[0]
        by_domain[domain] = {
            'domain': domain,
            'label': auth.label or pack.name,
            'aliases': list(auth.aliases),
            'access_strategy': auth.access_strategy,
            'login_url': auth.login_url,
            'risk_note_key': auth.risk_note_key,
            'fields': [dict(f) for f in auth.fields],
        }
    return list(by_domain.values())


def reset_cache_for_tests() -> None:
    global _cache, _cache_signature
    with _lock:
        _cache = {}
        _cache_signature = None
