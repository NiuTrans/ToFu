"""lib/self_update/_apply.py — the two update strategies + dispatcher.

``_apply_via_git`` (git pull --ff-only), ``_overlay_skip`` +
``_apply_via_tarball`` (download + overlay for non-git deployments), and
``apply_update`` which picks between them automatically.
"""

from __future__ import annotations

import os
import subprocess

from lib.http_client import http_stream
from lib.log import audit_log, log_context
from lib.runtime_layout import is_overlay_skipped as _rl_is_overlay_skipped
from lib.self_update._config import (
    UPDATE_BRANCH,
    UPDATE_REMOTE,
    _DOWNLOAD_TIMEOUT,
    _GIT_TIMEOUT,
    _ROOT,
    _TARBALL_URL,
    _UPDATE_BACKUP_DIR,
)
from lib.self_update._git import (
    _head_sha,
    _run_git,
    _run_git_streaming,
    git_available,
)
from lib.self_update._requirements import (
    _install_requirements,
    _requirements_changed,
)
from lib.self_update._status import working_tree_status
from lib.self_update._version import current_version, fetch_latest_release

from lib.log import get_logger

logger = get_logger(__name__)


def _facade(name, default):
    """Resolve ``name`` from the ``lib.self_update`` package namespace so tests
    that monkeypatch the facade (e.g. ``su.http_stream = ...``, ``su._ROOT =
    ...``) transparently affect these functions after the package split.
    Falls back to ``default`` (the value imported into this sub-module)."""
    import lib.self_update as _pkg
    return getattr(_pkg, name, default)


def _apply_via_git(progress=None) -> dict:
    """Run ``git fetch`` + ``git pull --ff-only``. Returns a result dict.

    Refuses (without mutating anything) when:
      * git is unavailable, or
      * the working tree has blocking (non-runtime) changes.

    On a successful pull that touched ``requirements.txt``, also runs
    ``pip install -r requirements.txt`` against the running interpreter so
    the update is self-contained — the outcome does NOT depend on the
    launcher (bootstrap.py) nor on a crash-and-recover restart.

    Args:
        progress: Optional callable ``fn(stage, status, detail='')`` invoked
            as the update advances so a UI can render live progress instead
            of staring at a frozen modal. ``stage`` is one of
            ``fetch`` / ``pull`` / ``deps``; ``status`` is
            ``active`` / ``done`` / ``skip`` / ``error``. Never lets a
            callback exception break the update.

    Returns::

        {'ok': bool, 'old_version': str, 'new_version': str,
         'changed': bool, 'needs_restart': bool, 'error': str|None,
         'detail': str, 'deps_changed': bool, 'deps_installed': bool,
         'deps_detail': str}
    """
    def _emit(stage: str, status: str, detail: str = '', meta=None):
        if not progress:
            return
        try:
            progress(stage, status, detail, meta)
        except TypeError:
            # Back-compat: a 3-arg progress callback (no meta).
            try:
                progress(stage, status, detail)
            except Exception as e:
                logger.debug('[Update] progress callback failed: %s', e)
        except Exception as e:
            logger.debug('[Update] progress callback failed: %s', e)

    old = current_version()
    result = {'ok': False, 'old_version': old, 'new_version': old,
              'changed': False, 'needs_restart': False,
              'error': None, 'detail': '', 'method': 'git',
              'deps_changed': False, 'deps_installed': False,
              'deps_detail': ''}

    status = working_tree_status()
    if not status['clean']:
        sample = ', '.join(status['blocking'][:5])
        result['error'] = (
            'Local changes to tracked files would be overwritten by the '
            'update. Commit, revert, or remove them first.')
        result['detail'] = f'{len(status["blocking"])} changed file(s): {sample}'
        logger.warning('[Update] apply refused: dirty tree (%d blocking) — %s',
                       len(status['blocking']), sample)
        _emit('fetch', 'error', result['detail'])
        return result

    before_sha = _head_sha()

    # Forward git's own ``<phase>: <pct>%`` progress (Receiving objects /
    # Resolving deltas) to the UI so a slow fetch shows a determinate bar
    # instead of an opaque spinner.
    #
    # CRUCIAL for "no stage ever looks stuck": git's LOCAL checkout/merge runs
    # AFTER the transfer but still INSIDE this single ``pull`` subprocess, and
    # it emits NO percentage. If we left the bar at the last determinate frame
    # (fetch pct=100), it would sit frozen at a FULL bar for many seconds on a
    # big update — the exact "looks stuck" symptom. So the instant a transfer
    # phase reaches 100%, we mark ``fetch`` done and flip to the ``pull``
    # stage's INDETERMINATE sweep (emit with no meta), which then visibly keeps
    # moving through the silent checkout for the rest of the subprocess. If a
    # later phase resumes below 100% (e.g. "Resolving deltas" following
    # "Receiving objects"), we flip back to the determinate ``fetch`` bar; it
    # re-lands on the indeterminate sweep at the true final 100%.
    _git_ui = {'flipped': False}

    def _git_progress(phase: str, pct: int, line: str):
        if pct >= 100:
            if not _git_ui['flipped']:
                _emit('fetch', 'active', phase, {'pct': 100, 'phase': phase})
                _emit('fetch', 'done')
                _emit('pull', 'active')  # no meta → indeterminate sweep
                _git_ui['flipped'] = True
        else:
            if _git_ui['flipped']:
                # A new sub-100% transfer phase started after we had already
                # flipped — return the determinate fetch bar for it.
                _emit('fetch', 'active')
                _git_ui['flipped'] = False
            _emit('fetch', 'active', phase, {'pct': pct, 'phase': phase})

    with log_context('self_update.git_pull', logger=logger):
        try:
            # ONE network round-trip: ``pull --ff-only`` fetches AND
            # fast-forwards. The previous code ran an explicit ``fetch``
            # first and then ``pull`` (which fetches AGAIN) — the remote was
            # transferred twice. Folding them halves the network wait, the
            # dominant cost on a slow connection.
            _emit('fetch', 'active')
            pull_cp = _run_git_streaming(
                ['pull', '--ff-only', UPDATE_REMOTE, UPDATE_BRANCH],
                timeout=_GIT_TIMEOUT, on_progress=_git_progress)
            if pull_cp.returncode != 0:
                blob = (pull_cp.stderr or pull_cp.stdout)[:500]
                # Distinguish "couldn't reach the remote" from "diverged
                # history" so the surfaced stage + message is accurate.
                if 'ff-only' in blob or 'diverge' in blob or 'non-fast-forward' in blob:
                    result['error'] = ('git pull --ff-only failed (history may '
                                       'have diverged).')
                    _emit('pull', 'error', blob)
                else:
                    result['error'] = 'git fetch/pull failed.'
                    _emit('fetch', 'error', blob)
                result['detail'] = blob
                logger.error('[Update] git pull failed: %s', result['detail'])
                return result
            # If no progress frames arrived at all (e.g. a tiny fast-forward,
            # or git built without progress), ensure fetch is closed and the
            # pull stage is shown before we finalize it.
            if not _git_ui['flipped']:
                _emit('fetch', 'done')
                _emit('pull', 'active')

            out = (pull_cp.stdout or '').strip()
            result['detail'] = out[:500]
            result['changed'] = 'Already up to date' not in out
            _emit('pull', 'done')
        except (FileNotFoundError, subprocess.TimeoutExpired,
                subprocess.SubprocessError) as e:
            result['error'] = 'git command error during update.'
            result['detail'] = str(e)[:500]
            logger.error('[Update] git pull errored: %s', e, exc_info=True)
            _emit('pull', 'error', result['detail'])
            return result

    # Re-read VERSION from disk (it may have just changed on a real pull).
    try:
        from pathlib import Path
        new = (Path(_ROOT) / 'VERSION').read_text(encoding='utf-8').strip()
    except Exception as e:
        logger.warning('[Update] Could not re-read VERSION post-pull: %s', e)
        new = old
    result['new_version'] = new
    result['ok'] = True
    # Any successful pull that changed files needs a restart to take effect.
    result['needs_restart'] = result['changed']

    # ── Install new dependencies if the pull touched requirements.txt ──
    # This makes the update self-contained: it does not rely on the
    # launcher (bootstrap.py) nor on server.py's ImportError-triggered
    # re-exec into bootstrap. A failed install does NOT revert the pull —
    # the code is already updated — but it DOES flip ok=False so the UI
    # tells the user to fix deps before restarting (a restart into a
    # missing-import state would just bounce through bootstrap anyway).
    if result['changed']:
        after_sha = _head_sha()
        result['deps_changed'] = _requirements_changed(before_sha, after_sha)
        if result['deps_changed']:
            _emit('deps', 'active')
            dep = _install_requirements(
                on_line=lambda ln: _emit('deps', 'active', ln[:120]))
            result['deps_installed'] = dep['ok']
            result['deps_detail'] = dep['detail']
            if not dep['ok']:
                result['ok'] = False
                result['error'] = (
                    'Code updated, but installing new dependencies failed. '
                    'Run "pip install -r requirements.txt" manually, then '
                    'restart.')
                _emit('deps', 'error', dep['detail'])
            else:
                _emit('deps', 'done')
        else:
            _emit('deps', 'skip')
    else:
        _emit('deps', 'skip')

    audit_log('self_update',
              old_version=old, new_version=new,
              changed=result['changed'], remote=UPDATE_REMOTE,
              branch=UPDATE_BRANCH, method='git',
              deps_changed=result['deps_changed'],
              deps_installed=result['deps_installed'])
    logger.info('[Update] applied via git: %s → %s (changed=%s deps_changed=%s '
                'deps_installed=%s)', old, new, result['changed'],
                result['deps_changed'], result['deps_installed'])
    return result


def _overlay_skip(rel: str) -> bool:
    """True if ``rel`` (project-root-relative, '/'-separated) must NOT be
    overwritten by a tarball overlay — user/runtime state (see
    ``lib.runtime_layout.OVERLAY_SKIP_PREFIXES``). Delegates to the single-source
    registry, which also covers any ``.tofu*`` agent artifact at any depth."""
    return _rl_is_overlay_skipped(rel)


def _apply_via_tarball(tag: str, progress=None) -> dict:
    """Update a non-git deployment by overlaying the release tarball.

    Strategy (every step reversible / non-destructive until validated):

      1. **fetch**  — download ``…/tarball/<tag>`` to a temp file.
      2. **pull**   — extract to a temp dir, *validate* it carries
         ``server.py`` / ``VERSION`` / ``lib/``, then copy each tracked
         file onto the project root, backing up any replaced file to
         ``.update_backup/<ts>/`` first. Skips ``_OVERLAY_SKIP_PREFIXES``
         (user data / runtime state) so memories, DB, configs survive.
      3. **deps**   — if ``requirements.txt`` changed, pip-install it.

    A tarball overlay cannot delete files removed upstream — documented in
    the module docstring and surfaced in ``result['detail']``.

    Args:
        tag: The release tag to install (e.g. ``'v0.13.0'``).
        progress: Same ``fn(stage, status, detail='')`` contract as
            ``_apply_via_git`` — reuses the ``fetch`` / ``pull`` / ``deps``
            stage keys so the frontend stepper is identical.

    Returns the same result dict shape as ``_apply_via_git`` (with
    ``method='tarball'``).
    """
    import hashlib
    import shutil
    import tarfile
    import tempfile
    import time
    from pathlib import Path

    def _emit(stage: str, status: str, detail: str = '', meta=None):
        if not progress:
            return
        try:
            progress(stage, status, detail, meta)
        except TypeError:
            try:
                progress(stage, status, detail)
            except Exception as e:
                logger.debug('[Update] progress callback failed: %s', e)
        except Exception as e:
            logger.debug('[Update] progress callback failed: %s', e)

    def _fmt_bytes(n: float) -> str:
        for unit in ('B', 'KB', 'MB', 'GB'):
            if n < 1024 or unit == 'GB':
                return (f'{n:.0f} {unit}' if unit == 'B'
                        else f'{n:.1f} {unit}')
            n /= 1024.0
        return f'{n:.1f} GB'

    old = current_version()
    result = {'ok': False, 'old_version': old, 'new_version': old,
              'changed': False, 'needs_restart': False,
              'error': None, 'detail': '', 'method': 'tarball',
              'deps_changed': False, 'deps_installed': False,
              'deps_detail': ''}

    # Resolve patchable collaborators through the facade so tests that set
    # ``su.http_stream`` / ``su._ROOT`` / ``su._install_requirements`` /
    # ``su._overlay_skip`` still take effect after the package split.
    _root = _facade('_ROOT', _ROOT)
    _stream = _facade('http_stream', http_stream)
    url = _TARBALL_URL.format(ref=tag)
    tmp_root = tempfile.mkdtemp(prefix='tofu-update-')
    tar_path = os.path.join(tmp_root, 'release.tar.gz')

    try:
        # ── 1. Download ──────────────────────────────────────────────
        _emit('fetch', 'active')
        with log_context('self_update.tarball_download', logger=logger):
            try:
                with _stream('GET', url, timeout=_DOWNLOAD_TIMEOUT,
                                 headers={'Accept': 'application/vnd.github+json'}) as resp:
                    if resp.status_code != 200:
                        result['error'] = 'Could not download the release archive.'
                        result['detail'] = f'HTTP {resp.status_code} from {url}'
                        logger.error('[Update] tarball download HTTP %s for %s',
                                     resp.status_code, url)
                        _emit('fetch', 'error', result['detail'])
                        return result
                    # Content-Length lets us render a determinate bar; GitHub's
                    # tarball redirect usually carries it. Absent → indeterminate
                    # (we still stream byte count + speed).
                    try:
                        content_len = int(resp.headers.get('Content-Length') or 0)
                    except (TypeError, ValueError):
                        content_len = 0
                    total = 0
                    t0 = time.monotonic()
                    last_emit = 0.0
                    with open(tar_path, 'wb') as fh:
                        for chunk in resp.iter_content(64 * 1024):
                            if not chunk:
                                continue
                            fh.write(chunk)
                            total += len(chunk)
                            # Throttle frames to ~4/s so a fast download does
                            # not flood the push channel.
                            now = time.monotonic()
                            if now - last_emit < 0.25:
                                continue
                            last_emit = now
                            elapsed = max(now - t0, 1e-6)
                            speed = total / elapsed
                            pct = (int(total * 100 / content_len)
                                   if content_len else None)
                            if content_len:
                                detail = (f'{_fmt_bytes(total)} / '
                                          f'{_fmt_bytes(content_len)} · '
                                          f'{_fmt_bytes(speed)}/s')
                            else:
                                detail = (f'{_fmt_bytes(total)} · '
                                          f'{_fmt_bytes(speed)}/s')
                            _emit('fetch', 'active', detail, {
                                'pct': pct, 'loaded': total,
                                'total': content_len or None,
                                'speed': speed})
            except Exception as e:
                result['error'] = 'Could not download the release archive.'
                result['detail'] = str(e)[:500]
                logger.error('[Update] tarball download failed: %s', e, exc_info=True)
                _emit('fetch', 'error', result['detail'])
                return result
        if total < 1024:
            result['error'] = 'Downloaded archive is implausibly small — aborting.'
            result['detail'] = f'{total} bytes'
            logger.error('[Update] tarball too small (%d bytes) — aborting', total)
            _emit('fetch', 'error', result['detail'])
            return result
        logger.info('[Update] tarball downloaded: %d bytes', total)
        _emit('fetch', 'done')

        # ── 2. Extract + validate + overlay ──────────────────────────
        _emit('pull', 'active')
        extract_dir = os.path.join(tmp_root, 'extract')
        os.makedirs(extract_dir, exist_ok=True)
        try:
            with tarfile.open(tar_path, 'r:gz') as tf:
                members = tf.getmembers()
                # GitHub wraps everything in a single top-level dir
                # (``<owner>-<repo>-<sha>/``); strip it. Guard against path
                # traversal (``..`` / absolute) before extracting anything.
                safe = []
                for m in members:
                    name = m.name
                    if name.startswith('/') or '..' in name.split('/'):
                        logger.warning('[Update] skipping unsafe tar member: %s', name)
                        continue
                    safe.append(m)
                tf.extractall(extract_dir, members=safe)
        except Exception as e:
            result['error'] = 'Could not extract the release archive (corrupt download?).'
            result['detail'] = str(e)[:500]
            logger.error('[Update] tarball extract failed: %s', e, exc_info=True)
            _emit('pull', 'error', result['detail'])
            return result

        # Resolve the single wrapper dir.
        entries = [os.path.join(extract_dir, n) for n in os.listdir(extract_dir)]
        roots = [p for p in entries if os.path.isdir(p)]
        src_root = roots[0] if len(roots) == 1 else extract_dir

        # Validate: this must look like a Tofu source tree, else abort
        # WITHOUT touching the live install.
        for sentinel in ('server.py', 'VERSION', 'lib'):
            if not os.path.exists(os.path.join(src_root, sentinel)):
                result['error'] = ('Downloaded archive is not a valid Tofu '
                                   'release — aborting (nothing changed).')
                result['detail'] = f'missing {sentinel}'
                logger.error('[Update] tarball validation failed: missing %s', sentinel)
                _emit('pull', 'error', result['detail'])
                return result

        new_ver = old
        try:
            new_ver = (Path(src_root) / 'VERSION').read_text(encoding='utf-8').strip() or old
        except Exception as e:
            logger.warning('[Update] could not read VERSION from archive: %s', e)

        # Decide whether requirements.txt actually changed so we can SKIP the
        # slow pip install when it didn't (the previous code always installed
        # defensively — often the single longest stage of the whole update).
        def _req_digest(p) -> str:
            try:
                return hashlib.sha256(Path(p).read_bytes()).hexdigest()
            except Exception:
                return ''
        _req_old = _req_digest(os.path.join(_root, 'requirements.txt'))
        _req_new = _req_digest(os.path.join(src_root, 'requirements.txt'))
        # Unknown either way (missing file / read error) → install defensively.
        deps_changed = (_req_new != _req_old) or not _req_new or not _req_old

        # Overlay every file, backing up replacements first.
        backup_dir = os.path.join(_root, _UPDATE_BACKUP_DIR,
                                   time.strftime('%Y%m%d-%H%M%S'))
        copied = 0
        skipped = 0
        backed_up = 0
        src_root_p = Path(src_root)
        try:
            for abs_src in src_root_p.rglob('*'):
                if abs_src.is_dir():
                    continue
                rel = abs_src.relative_to(src_root_p).as_posix()
                if _facade('_overlay_skip', _overlay_skip)(rel):
                    skipped += 1
                    continue
                dest = os.path.join(_root, rel)
                # Back up an existing file before overwriting it.
                if os.path.isfile(dest):
                    bpath = os.path.join(backup_dir, rel)
                    os.makedirs(os.path.dirname(bpath), exist_ok=True)
                    shutil.copy2(dest, bpath)
                    backed_up += 1
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copy2(str(abs_src), dest)
                copied += 1
        except Exception as e:
            # A mid-overlay failure leaves a partially-updated tree. The
            # backup dir holds the originals of everything replaced so far;
            # tell the user where it is rather than silently half-updating.
            result['error'] = ('Update failed partway through writing files. '
                               'Original files were backed up.')
            result['detail'] = (f'{str(e)[:300]} — backup at '
                                f'{os.path.relpath(backup_dir, _root)}')
            logger.error('[Update] tarball overlay failed after %d file(s): %s',
                         copied, e, exc_info=True)
            _emit('pull', 'error', result['detail'])
            return result

        result['changed'] = copied > 0
        result['new_version'] = new_ver
        result['detail'] = (f'overlaid {copied} file(s), backed up {backed_up}, '
                           f'preserved {skipped} (note: a tarball update cannot '
                           f'remove files deleted upstream)')
        logger.info('[Update] tarball overlay: copied=%d backed_up=%d skipped=%d '
                    'backup=%s', copied, backed_up, skipped, backup_dir)
        _emit('pull', 'done')

        # ── 3. Dependencies ──────────────────────────────────────────
        result['ok'] = True
        result['needs_restart'] = result['changed']
        # Install ONLY when requirements.txt actually changed (hash compared
        # above). This skips the slow pip stage entirely for the common case
        # of a code-only release.
        if result['changed'] and deps_changed:
            result['deps_changed'] = True
            _emit('deps', 'active')
            dep = _facade('_install_requirements', _install_requirements)(
                on_line=lambda ln: _emit('deps', 'active', ln[:120]))
            result['deps_installed'] = dep['ok']
            result['deps_detail'] = dep['detail']
            if not dep['ok']:
                result['ok'] = False
                result['error'] = (
                    'Code updated, but installing dependencies failed. '
                    'Run "pip install -r requirements.txt" manually, then '
                    'restart.')
                _emit('deps', 'error', dep['detail'])
            else:
                _emit('deps', 'done')
        else:
            _emit('deps', 'skip')

        audit_log('self_update',
                  old_version=old, new_version=new_ver,
                  changed=result['changed'], method='tarball', tag=tag,
                  deps_changed=result['deps_changed'],
                  deps_installed=result['deps_installed'])
        logger.info('[Update] applied via tarball: %s → %s (changed=%s)',
                    old, new_ver, result['changed'])
        return result
    finally:
        try:
            shutil.rmtree(tmp_root, ignore_errors=True)
        except Exception as e:
            logger.debug('[Update] temp cleanup failed: %s', e)


def apply_update(progress=None) -> dict:
    """Apply the available update, choosing git vs. tarball automatically.

    * **git checkout** → ``_apply_via_git`` (``git pull --ff-only``).
    * **non-git deployment** → ``_apply_via_tarball`` (download + overlay).

    Both paths share the same result-dict shape and the same
    ``fetch`` / ``pull`` / ``deps`` progress stages, so the route layer and
    frontend are agnostic to which ran (``result['method']`` records it).

    Args:
        progress: Optional ``fn(stage, status, detail='')`` callback.

    Returns the result dict (see ``_apply_via_git`` for the shape).
    """
    if _facade('git_available', git_available)():
        return _apply_via_git(progress=progress)

    # No git — fall back to the tarball overlay. Resolve the target tag from
    # the release check (the same source the badge uses).
    logger.info('[Update] no git checkout — using tarball-overlay fallback')
    latest = _facade('fetch_latest_release', fetch_latest_release)()
    if not latest or not latest.get('tag'):
        old = current_version()
        return {'ok': False, 'old_version': old, 'new_version': old,
                'changed': False, 'needs_restart': False, 'method': 'tarball',
                'error': 'Could not determine the latest release to download.',
                'detail': '', 'deps_changed': False, 'deps_installed': False,
                'deps_detail': ''}
    return _apply_via_tarball(latest['tag'], progress=progress)
