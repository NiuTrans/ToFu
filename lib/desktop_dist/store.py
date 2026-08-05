"""lib/desktop_dist/store.py — the server-hosted desktop-installer store.

A directory (``<data_root>/desktop_dist/``, env-overridable via
``TOFU_DESKTOP_DIST_DIR``) holding installer artifacts plus one
``manifest.json`` describing them. Two writers, one reader:

  * ``mirror.py``  — downloads published GitHub release assets in the
    background (every platform the server cannot build itself);
  * ``builder.py`` — drops a natively-built artifact here (the platform the
    server CAN build: its own);
  * the status route reads it to offer same-origin download URLs with ZERO
    network in the request path — the fix for the settings-panel stall,
    which was a synchronous GitHub probe inside an async route.

Manifest shape::

    {
      "tag": "v0.14.2",                    # newest mirrored release tag
      "refreshed_at": 1785000000.0,        # last SUCCESSFUL mirror refresh
      "last_error": null,                  # or {"at": ts, "error": "…"}
      "artifacts": {
        "Tofu-Setup-0.14.2-win64.exe": {
          "os": "windows", "arch": "x86_64", "label": "Windows installer",
          "filename": "Tofu-Setup-0.14.2-win64.exe",
          "size": 115822886, "sha256": "…",
          "source": "mirrored",            # or "built"
          "version": "0.14.2",
          "fetched_at": 1785000000.0
        }, …
      }
    }

Artifacts are keyed by FILENAME and served by exact key match only — the
download route never sees path material, so traversal is structurally
impossible (``resolve_file`` rejects anything that is not a manifest key).

Everything degrades stale-while-revalidate: a failed refresh keeps the old
artifacts servable; entries are only replaced by atomic ``os.replace`` of a
fully-downloaded ``.part`` file.
"""

from __future__ import annotations

import os
import time

from lib.json_store import read_json, update_json_atomic, write_json_atomic
from lib.log import get_logger
from lib.runtime_paths import data_root

logger = get_logger(__name__)

_MANIFEST_NAME = 'manifest.json'


def _store_dir() -> str:
    """The artifact directory (created on demand).

    ``TOFU_DESKTOP_DIST_DIR`` overrides the default — tests use it to get an
    isolated store, and a deployment can point it at bigger/faster storage.
    Read at CALL time so a test's monkeypatch always takes effect.
    """
    override = os.environ.get('TOFU_DESKTOP_DIST_DIR', '').strip()
    d = override or os.path.join(data_root(), 'desktop_dist')
    try:
        os.makedirs(d, exist_ok=True)
    except OSError as e:
        logger.warning('[DesktopDist] store dir %s not creatable: %s', d, e)
    return d


def _manifest_path() -> str:
    return os.path.join(_store_dir(), _MANIFEST_NAME)


def load_manifest() -> dict:
    """The manifest, tolerating absence and corruption (both → empty shape).

    A corrupt manifest must not take the download surface down: the store
    simply looks empty, the mirror re-fetches, and the route falls back to
    the releases page. Never raises.
    """
    try:
        m = read_json(_manifest_path(), default=None)
    except Exception as e:
        logger.warning('[DesktopDist] manifest unreadable (%s) — treating '
                       'the store as empty', e)
        m = None
    if not isinstance(m, dict):
        return {'tag': '', 'refreshed_at': 0.0, 'last_error': None,
                'artifacts': {}}
    if not isinstance(m.get('artifacts'), dict):
        m['artifacts'] = {}
    m.setdefault('tag', '')
    m.setdefault('refreshed_at', 0.0)
    m.setdefault('last_error', None)
    return m


def save_manifest(m: dict) -> None:
    write_json_atomic(_manifest_path(), m)


def artifacts() -> dict:
    """``{filename: entry}`` for every recorded artifact."""
    return load_manifest().get('artifacts') or {}


def resolve_file(filename: str) -> str | None:
    """Absolute path of a servable artifact, or None.

    The ONLY way the download route maps a URL segment to a file: an exact
    manifest-key match plus an on-disk existence check. Anything containing
    path material is refused before the filesystem is consulted.
    """
    if (not filename or '/' in filename or '\\' in filename
            or filename in ('.', '..')):
        return None
    entry = artifacts().get(filename)
    if not entry:
        return None
    path = os.path.join(_store_dir(), filename)
    return path if os.path.isfile(path) else None


def record_artifact(entry: dict) -> None:
    """Insert/replace one artifact entry (atomic read-modify-write)."""
    name = entry.get('filename')
    if not name:
        logger.warning('[DesktopDist] record_artifact called without a '
                       'filename: %r', entry)
        return

    def _mut(m):
        if not isinstance(m, dict):
            m = {}
        arts = m.setdefault('artifacts', {})
        if not isinstance(arts, dict):
            m['artifacts'] = arts = {}
        arts[name] = entry
        return m

    update_json_atomic(_manifest_path(), _mut,
                       default={'artifacts': {}})


def remove_not_in(keep_names, *, sources=('mirrored',)) -> list:
    """Delete artifact files + entries whose name is not in ``keep_names``.

    Only entries whose ``source`` is in ``sources`` are eligible — a locally
    BUILT artifact is never pruned by the mirror just because no release
    asset shares its name. Returns the removed filenames.
    """
    m = load_manifest()
    arts = m.get('artifacts') or {}
    keep = set(keep_names or [])
    removed = []
    for name, entry in list(arts.items()):
        if name in keep:
            continue
        if (entry or {}).get('source', 'mirrored') not in sources:
            continue
        path = os.path.join(_store_dir(), name)
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError as e:
            logger.warning('[DesktopDist] could not remove stale artifact '
                           '%s: %s', path, e)
        del arts[name]
        removed.append(name)
    if removed:
        save_manifest(m)
        logger.info('[DesktopDist] pruned stale artifacts: %s', removed)
    return removed


def find_for_platform(os_key: str, arch: str = '',
                      kind: str = 'full') -> list:
    """The servable artifacts for one visitor, best-first per platform row.

    Shares the narrowing rule with the old GitHub-URL matcher
    (``platforms._platform_rows_for``) so the two supply paths can never
    disagree about which rows a visitor should see. When several entries
    satisfy one row (a locally-built tar.gz AND the mirrored one), the
    NEWER version wins — a stale local build must not pin the user below
    the mirrored release; ties go to the built artifact (it matches this
    server's own code).

    ``kind`` separates the two components (docs/DESKTOP_AGENT_DIST_DESIGN
    .md): entries carry ``kind: 'full' | 'agent'`` (absent ⇒ 'full').
    The default 'full' keeps every pre-kind caller byte-identical — and
    an agent artifact can NEVER shadow the full installer just for being
    newer (both are 'built' at the same version, so without the filter
    the fresher wrap wins the same row).
    """
    from lib.desktop_dist.platforms import _platform_rows_for

    arts = artifacts()
    out = []
    for _os, _arch, _label, _pat, _min in _platform_rows_for(os_key, arch,
                                                             kind):
        cands = [a for a in arts.values()
                 if isinstance(a, dict)
                 and a.get('kind', 'full') == kind
                 and a.get('os') == _os and a.get('arch') == _arch
                 and a.get('filename')
                 and os.path.isfile(
                     os.path.join(_store_dir(), a['filename']))]
        if not cands:
            continue
        cands.sort(key=lambda a: (
            _version_key(a.get('version')),
            1 if a.get('source') == 'built' else 0,
            a.get('fetched_at') or 0.0,
        ), reverse=True)
        out.append(cands[0])
    return out


def is_loopback_url(url: str) -> bool:
    """True when the URL can only ever mean 'this same machine'.

    Used to judge preseed URLs: a loopback preseed works only when the
    installer lands on the SERVER's own machine — offered to a remote
    controlled machine it attaches the agent to a void. Unparseable or
    empty input is NOT loopback (callers treat empty separately).
    """
    from urllib.parse import urlparse
    try:
        host = (urlparse((url or '').strip()).hostname or '').lower()
    except Exception as e:
        logger.debug('[DesktopDist] preseed url %r unparseable: %s', url, e)
        return False
    if not host:
        return False
    return (host == 'localhost' or host == '0.0.0.0' or host == '::1'
            or host.startswith('127.'))


def _version_key(version) -> tuple:
    """Sort key for a version string ('0.14.2' / 'v0.14.2'); unknown → 0."""
    try:
        from lib.self_update import _parse_semver
        parsed = _parse_semver(str(version or ''))
        if parsed:
            return tuple(parsed)
    except Exception as e:
        logger.debug('[DesktopDist] version %r unparseable: %s', version, e)
    return (0,)


def mark_refresh(tag: str, *, error: str | None = None) -> None:
    """Record a mirror outcome. ``error=None`` means full success."""
    m = load_manifest()
    if tag:
        m['tag'] = tag
    if error is None:
        m['refreshed_at'] = time.time()
        m['last_error'] = None
    else:
        m['last_error'] = {'at': time.time(), 'error': error}
    save_manifest(m)


def manifest_age_s() -> float | None:
    """Seconds since the last successful refresh; None when never refreshed."""
    m = load_manifest()
    ts = m.get('refreshed_at') or 0.0
    if not ts:
        return None
    return max(0.0, time.time() - float(ts))


def last_error_age_s() -> float | None:
    m = load_manifest()
    err = m.get('last_error')
    if not isinstance(err, dict) or not err.get('at'):
        return None
    return max(0.0, time.time() - float(err['at']))


__all__ = [
    '_store_dir', 'load_manifest', 'save_manifest', 'artifacts',
    'is_loopback_url',
    'resolve_file', 'record_artifact', 'remove_not_in', 'find_for_platform',
    'mark_refresh', 'manifest_age_s', 'last_error_age_s',
]
