"""lib/desktop_dist/mirror.py — keep a local copy of the published installers.

The server cannot build Windows/macOS installers itself (PyInstaller cannot
cross-compile, and this host has no Wine), but it CAN fetch each published
asset from GitHub once, in the background, and then serve every client from
local disk. That is the whole user-visible win either way: the install no
longer depends on the client's route to the public GitHub network.

Refresh policy
--------------
* ``ensure_fresh`` is called from the status route on every poll. It is
  cheap (a manifest-age check) and non-blocking: when the store is missing
  or older than ``_DEFAULT_MAX_AGE_S`` it spawns ONE worker thread (single
  flight — a second call while the worker runs changes nothing).
* A refresh downloads only what changed: an asset whose filename AND size
  match the local entry is kept (a 115 MB installer is not re-fetched every
  6 h). A new release tag brings new filenames, which simply miss and get
  fetched.
* Failure is stale-while-revalidate: a failed probe or download keeps the
  old artifacts servable and records ``last_error``; the next kick retries
  after a short backoff (``_ERROR_RETRY_S``) rather than every poll.
* Pruning happens only after a SUCCESSFUL probe, and only for mirrored
  files absent from the current release — a locally-built artifact is never
  the mirror's to delete.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import threading
import time

from lib.log import get_logger

from . import platforms, store

logger = get_logger(__name__)

_DEFAULT_MAX_AGE_S = int(os.environ.get('TOFU_DESKTOP_DIST_MAX_AGE_S',
                                        6 * 3600))
_ERROR_RETRY_S = 60.0

_worker_lock = threading.Lock()
_worker: threading.Thread | None = None


def is_running() -> bool:
    t = _worker
    return bool(t and t.is_alive())


def stale(max_age_s: float = _DEFAULT_MAX_AGE_S) -> bool:
    """True when a refresh is due (or a recent failure cleared for retry)."""
    age = store.manifest_age_s()
    if age is None:
        return True
    if age > max_age_s:
        return True
    err_age = store.last_error_age_s()
    return err_age is not None and err_age > _ERROR_RETRY_S


def ensure_fresh(max_age_s: float = _DEFAULT_MAX_AGE_S, *,
                 force: bool = False) -> bool:
    """Kick a background refresh if due. Returns True iff a worker is running.

    Never blocks the caller; never spawns a second worker while one runs.
    """
    if not force and not stale(max_age_s):
        return is_running()
    global _worker
    with _worker_lock:
        if _worker and _worker.is_alive():
            return True
        _worker = threading.Thread(target=_refresh_safe,
                                   name='desktop-dist-mirror', daemon=True)
        _worker.start()
    return True


def _refresh_safe() -> None:
    try:
        refresh_now()
    except Exception as e:
        logger.error('[DesktopDist] mirror refresh crashed: %s', e,
                     exc_info=True)
        store.mark_refresh('', error=f'crash: {e}')


def refresh_now() -> bool:
    """One synchronous refresh. Returns True on full success.

    Synchronous on purpose: the worker thread wraps it, and tests drive it
    directly. Per-asset failures degrade that asset only — the others still
    land, the old file stays servable for the failed one, and the manifest
    records the error so ``stale`` retries it.
    """
    rel = platforms.fetch_latest_release()
    if not rel:
        store.mark_refresh('', error='release probe failed')
        return False
    tag = rel.get('tag') or ''
    assets = rel.get('assets') or []

    wanted: dict = {}
    # Both component tables (docs/DESKTOP_AGENT_DIST_DESIGN.md §5.2):
    # releases that carry agent assets get them mirrored with kind='agent'
    # recorded; releases without them simply produce no agent rows.
    for kind in ('full', 'agent'):
        for row in platforms._platform_assets(kind):
            _os, _arch, label, pattern, _min = row
            hit = next((a for a in assets
                        if fnmatch.fnmatch(a.get('name', ''), pattern)), None)
            if hit:
                wanted[hit['name']] = (row, hit, kind)
    if not wanted:
        store.mark_refresh(tag, error='release carried no matching assets')
        return False

    existing = store.artifacts()
    ok = True
    for name, (row, asset, kind) in wanted.items():
        _os, _arch, label, _pattern, _min = row
        dest = os.path.join(store._store_dir(), name)
        cur = existing.get(name)
        if (cur and os.path.isfile(dest)
                and asset.get('size') is not None
                and cur.get('size') == asset.get('size')):
            continue  # unchanged — do not re-download a 100+ MB file
        try:
            size, sha = _download(asset['url'], dest + '.part')
            os.replace(dest + '.part', dest)
        except Exception as e:
            ok = False
            logger.warning('[DesktopDist] download of %s failed: %s',
                           name, e)
            try:
                if os.path.isfile(dest + '.part'):
                    os.remove(dest + '.part')
            except OSError as rm_err:
                logger.debug('[DesktopDist] .part cleanup failed: %s', rm_err)
            continue
        store.record_artifact({
            'os': _os, 'arch': _arch, 'label': label, 'filename': name,
            'size': size, 'sha256': sha, 'source': 'mirrored',
            'version': tag.lstrip('v'), 'kind': kind,
            'fetched_at': time.time(),
        })
        logger.info('[DesktopDist] mirrored %s (%d bytes, sha256 %.12s)',
                    name, size, sha)

    # Only a successful probe may prune, and only files the release no
    # longer carries — built artifacts are excluded inside remove_not_in.
    # AND never strand a platform: when the old file's replacement failed to
    # land, the old file stays servable (stale beats absent).
    keep = set(wanted)
    arts = store.artifacts()
    for name, entry in arts.items():
        if name in keep or (entry or {}).get('source') != 'mirrored':
            continue
        served = any(
            n != name and (e or {}).get('os') == (entry or {}).get('os')
            and (e or {}).get('arch') == (entry or {}).get('arch')
            and (e or {}).get('kind', 'full') == (entry or {}).get(
                'kind', 'full')
            and os.path.isfile(os.path.join(store._store_dir(), n))
            for n, e in arts.items())
        if not served:
            keep.add(name)
            logger.warning('[DesktopDist] keeping stale %s — its replacement '
                           'never landed, so pruning it would strand the '
                           'platform', name)
    store.remove_not_in(keep, sources=('mirrored',))
    store.mark_refresh(tag, error=None if ok else 'some assets failed')
    return ok


def _download(url: str, dest_part: str) -> tuple:
    """Stream ``url`` to ``dest_part``; return ``(size, sha256_hex)``.

    Writes ONLY the ``.part`` path — the caller's ``os.replace`` is what
    makes a completed download visible, so a killed refresh never leaves a
    truncated file under the final name.
    """
    from lib.http_client import http_stream
    h = hashlib.sha256()
    size = 0
    with http_stream('GET', url, timeout=60,
                     headers={'Accept': 'application/octet-stream'}) as resp:
        if resp.status_code != 200:
            raise RuntimeError(f'HTTP {resp.status_code} for {url}')
        with open(dest_part, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                h.update(chunk)
                size += len(chunk)
            f.flush()
            os.fsync(f.fileno())
    if size <= 0:
        raise RuntimeError(f'empty download from {url}')
    return size, h.hexdigest()


__all__ = ['ensure_fresh', 'refresh_now', 'stale', 'is_running']
