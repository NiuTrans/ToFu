"""Content shadow: local-disk mirror of indexed project trees for fast grep.

The tree index already knows every indexed file's ``(path, size, mtime)``.
The shadow copies file CONTENTS to a local-disk mirror so grep's ripgrep
pass reads a fast volume instead of the project's network filesystem — on
this repo's FUSE mount that turns a ~900 ms candidate read into ~10 ms.

Design contract:

- **Reconstructible cache, never authority.**  Every shadow entry is
  validated by ``(size, mtime)`` against the live tree-index columns before
  it may serve; the project filesystem stays the only source of truth.
  Copies skip fsync by design (a crash just forces a re-copy).
- **Output parity.**  grep runs the same ripgrep binary with the same flags
  over the shadow tree and rewrites result path prefixes back to the real
  root, so ranking/line/context/count semantics are unchanged.
- **Bounded everywhere.**  Per-root file/byte caps, per-round sync budgets,
  a global byte cap with LRU root eviction, one background sync worker per
  root, and graceful fallback to the live walk whenever the shadow is
  absent, incomplete, stale, or not covering a query's candidates.
- **Tofu-owned writes stay exact.**  The tree index bumps its mutation
  revision on synchronized writes; a mismatched shadow manifest is rebuilt
  in the background while the current query falls back to the live path.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time

from lib.log import get_logger
from lib.project_mod import tree_index

logger = get_logger(__name__)

# Master switch / bounds (env-overridable, matching repo conventions).
_ENABLED = os.environ.get('TOFU_CONTENT_SHADOW', '1') not in ('0', 'false', 'False')
_MAX_FILE_BYTES = int(os.environ.get('TOFU_CONTENT_SHADOW_MAX_FILE_BYTES',
                                     str(8 * 1024 * 1024)))
_ROOT_MAX_FILES = int(os.environ.get('TOFU_CONTENT_SHADOW_ROOT_MAX_FILES', '200000'))
_ROOT_MAX_BYTES = int(os.environ.get('TOFU_CONTENT_SHADOW_ROOT_MAX_BYTES',
                                     str(1024 * 1024 * 1024)))
_GLOBAL_MAX_BYTES = int(os.environ.get('TOFU_CONTENT_SHADOW_GLOBAL_MAX_BYTES',
                                       str(4 * 1024 * 1024 * 1024)))
_SYNC_ROUND_FILES = 2000
_SYNC_ROUND_BYTES = 64 * 1024 * 1024
_SYNC_ROUND_SLEEP_S = 0.05

_lock = threading.RLock()
_workers: dict[str, threading.Thread] = {}
_last_used: dict[str, float] = {}


# ── Location ─────────────────────────────────────────────────────────────

def _shadow_home() -> str:
    override = os.environ.get('TOFU_CONTENT_SHADOW_DIR', '').strip()
    if override:
        return override
    xdg = os.environ.get('XDG_CACHE_HOME', '').strip()
    if xdg:
        return os.path.join(xdg, 'tofu', 'content-shadow')
    home = os.path.expanduser('~')
    if home and home != '~':
        return os.path.join(home, '.cache', 'tofu', 'content-shadow')
    return os.path.join(tempfile.gettempdir(),
                        f'tofu-content-shadow-{os.getuid()}')


def _root_dir(root: str) -> str:
    digest = hashlib.sha1(root.encode('utf-8')).hexdigest()[:16]
    return os.path.join(_shadow_home(), digest)


def _manifest_path(root_dir: str) -> str:
    return os.path.join(root_dir, 'manifest.json')


def _tree_dir(root_dir: str) -> str:
    return os.path.join(root_dir, 'tree')


# ── Manifest ─────────────────────────────────────────────────────────────

def _load_manifest(root_dir: str) -> dict | None:
    try:
        with open(_manifest_path(root_dir), encoding='utf-8') as f:
            manifest = json.load(f)
        if not isinstance(manifest, dict) or not isinstance(
                manifest.get('entries'), dict):
            return None
        return manifest
    except (OSError, ValueError):
        return None


def _write_manifest_atomic(root_dir: str, manifest: dict) -> None:
    os.makedirs(root_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=root_dir, suffix='.tmp', prefix='.manifest_')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(manifest, f, separators=(',', ':'))
        os.replace(tmp, _manifest_path(root_dir))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _columns_digest(entry) -> tuple[int, int, int]:
    return (len(entry.paths), sum(entry.sizes), sum(entry.mtimes))


# ── Sync worker ──────────────────────────────────────────────────────────

def _sync_worker(root: str, root_dir: str) -> None:
    """Incrementally mirror the live index's file contents into the shadow.

    Bounded rounds copy only new/changed files and delete removed ones; the
    manifest flips ``complete`` only after a fully converged round, so a
    crash anywhere just resumes on the next warm.  This thread never blocks
    a foreground query — the grep path falls back to the live walk until
    the shadow is complete and current.
    """
    tree = _tree_dir(root_dir)
    try:
        while True:
            entry = tree_index.acquire(root)
            if entry is None or not entry.complete:
                return  # no trustworthy index; a later warm retries
            manifest = _load_manifest(root_dir) or {
                'root': root, 'entries': {}, 'complete': False,
            }
            if manifest.get('root') != root:
                manifest = {'root': root, 'entries': {}, 'complete': False}
            entries = manifest['entries']
            digest = _columns_digest(entry)
            if manifest.get('complete') and manifest.get('digest') == list(digest):
                manifest['source_revision'] = entry.mutation_revision
                _write_manifest_atomic(root_dir, manifest)
                return

            wanted: dict[str, tuple[int, int]] = {}
            overflow = False
            total_bytes = 0
            for rel, size, mtime in zip(entry.paths, entry.sizes, entry.mtimes):
                if len(wanted) >= _ROOT_MAX_FILES:
                    overflow = True
                    break
                if size > _MAX_FILE_BYTES:
                    continue
                if total_bytes + size > _ROOT_MAX_BYTES:
                    overflow = True
                    break
                wanted[rel] = (int(size), int(mtime))
                total_bytes += size

            copied = 0
            copied_bytes = 0
            # Delete shadow files that are no longer wanted.
            for rel in list(entries):
                if rel not in wanted:
                    try:
                        os.unlink(os.path.join(tree, rel))
                    except OSError:
                        pass
                    entries.pop(rel, None)
            for rel, (size, mtime) in wanted.items():
                state = entries.get(rel)
                if state and state[0] == size and state[1] == mtime:
                    continue
                if copied >= _SYNC_ROUND_FILES or copied_bytes >= _SYNC_ROUND_BYTES:
                    break
                src = os.path.join(root, rel)
                dst = os.path.join(tree, rel)
                try:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    fd, tmp = tempfile.mkstemp(
                        dir=os.path.dirname(dst), suffix='.shadowtmp')
                    try:
                        with os.fdopen(fd, 'wb') as out, open(src, 'rb') as inp:
                            shutil.copyfileobj(inp, out, 256 * 1024)
                        os.replace(tmp, dst)
                    except BaseException:
                        try:
                            os.unlink(tmp)
                        except OSError:
                            pass
                        raise
                    entries[rel] = [size, mtime]
                    copied += 1
                    copied_bytes += size
                except OSError as e:
                    logger.debug('[ContentShadow] copy failed %s: %s', src, e)

            # Converged when every wanted file is mirrored (deletions ran
            # first, so extras are gone) and no cap cut the wanted set.
            # A budget-limited round simply leaves converged=False and the
            # next round resumes where this one stopped.
            converged = (not overflow) and all(rel in entries for rel in wanted)
            manifest.update({
                'root': root,
                'entries': entries,
                'complete': bool(converged and not overflow),
                'digest': list(digest),
                'source_revision': entry.mutation_revision,
                'updated_at': time.time(),
            })
            _write_manifest_atomic(root_dir, manifest)
            _enforce_global_cap()
            if converged or overflow:
                return
            time.sleep(_SYNC_ROUND_SLEEP_S)
    except Exception as e:
        logger.debug('[ContentShadow] sync worker for %s failed: %s', root, e)
    finally:
        with _lock:
            _workers.pop(root, None)


def warm(root: str) -> None:
    """Kick a background shadow sync for *root*.  Non-blocking."""
    if not _ENABLED or not root:
        return
    root = os.path.abspath(root)
    with _lock:
        if root in _workers and _workers[root].is_alive():
            return
        worker = threading.Thread(
            target=_sync_worker,
            args=(root, _root_dir(root)),
            name='content-shadow-sync', daemon=True)
        _workers[root] = worker
        worker.start()


def invalidate(root: str) -> None:
    """Drop a root's shadow (destructive commands may have mutated content)."""
    if not root:
        return
    root = os.path.abspath(root)
    with _lock:
        _workers.pop(root, None)
    # The worker re-reads the (invalidated) index and rebuilds honestly; the
    # directory removal just forces a full re-copy.
    shutil.rmtree(_root_dir(root), ignore_errors=True)


# ── Global LRU cap ───────────────────────────────────────────────────────

def _enforce_global_cap() -> None:
    try:
        home = _shadow_home()
        totals = []
        for name in os.listdir(home):
            root_dir = os.path.join(home, name)
            manifest = _load_manifest(root_dir)
            if manifest is None:
                continue
            used = manifest.get('updated_at') or 0.0
            size = sum(state[0] for state in manifest['entries'].values()
                       if isinstance(state, list) and len(state) == 2)
            totals.append((used, root_dir, size))
    except OSError:
        return
    total = sum(size for _, _, size in totals)
    for _, root_dir, size in sorted(totals):
        if total <= _GLOBAL_MAX_BYTES:
            break
        shutil.rmtree(root_dir, ignore_errors=True)
        total -= size


# ── Query admission ──────────────────────────────────────────────────────

def shadow_candidates(root: str, entry, cand_abs: list[str]) -> list[str] | None:
    """Map real candidate paths to shadow paths, or ``None`` (fall back).

    Every candidate must be present in the shadow manifest with the exact
    ``(size, mtime)`` the live index reports — one partial file is never
    served.  On any miss the caller keeps the live walk and a background
    sync is kicked so later queries converge to the shadow.
    """
    if not _ENABLED or entry is None or not getattr(entry, 'complete', False):
        return None
    root_dir = _root_dir(root)
    manifest = _load_manifest(root_dir)
    if manifest is None or not manifest.get('complete'):
        warm(root)
        return None
    if manifest.get('root') != root:
        return None
    if (manifest.get('source_revision') != entry.mutation_revision
            and manifest.get('digest') != list(_columns_digest(entry))):
        warm(root)
        return None
    entries = manifest['entries']
    tree = _tree_dir(root_dir)
    base = root.rstrip('/') + '/'
    # Per-candidate (size, mtime) must match the LIVE index columns exactly:
    # the manifest-level revision/digest pre-check only decides freshness,
    # it cannot prove the mirrored bytes equal what the index reports now.
    live_state = {}
    for abs_path in cand_abs:
        if not abs_path.startswith(base):
            return None
        live_state[abs_path[len(base):]] = None
    import bisect as _bisect
    paths = entry.paths
    for rel in list(live_state):
        i = _bisect.bisect_left(paths, rel)
        if i >= len(paths) or paths[i] != rel:
            return None  # candidate not in the index at all — live path owns it
        live_state[rel] = (int(entry.sizes[i]), int(entry.mtimes[i]))
    mapped = []
    for rel, state in live_state.items():
        if entries.get(rel) != [state[0], state[1]]:
            warm(root)
            return None
        mapped.append(os.path.join(tree, rel))
    with _lock:
        _last_used[root_dir] = time.time()
    return mapped


def rewrite_output_paths(raw: bytes, root: str) -> bytes:
    """Rewrite shadow-root result prefixes back to the real project root."""
    tree_prefix = _tree_dir(_root_dir(root)).encode('utf-8') + b'/'
    real_prefix = root.rstrip('/').encode('utf-8') + b'/'
    return raw.replace(tree_prefix, real_prefix)


def shadow_metrics() -> dict:
    """Bounded, content-free telemetry for diagnostics/tests."""
    with _lock:
        return {
            'enabled': _ENABLED,
            'workers': len([w for w in _workers.values() if w.is_alive()]),
            'home': _shadow_home(),
        }
