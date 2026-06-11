"""lib/self_update.py — In-place self-update via ``git pull --ff-only``.

Backs the topbar "Update" button. Two responsibilities:

  1. **Check** — compare the local ``VERSION`` against the newest release
     tag published on the official GitHub repo (``UPDATE_REPO``). We use
     the *tags* API (``/repos/<owner>/<repo>/tags``) rather than the raw
     ``VERSION`` file so the check respects actual cut releases, not
     in-progress ``main`` commits. ``/releases/latest`` is intentionally
     NOT used because the project tags releases without creating GitHub
     "Release" objects (the endpoint 404s).

  2. **Apply** — ``git fetch`` + ``git pull --ff-only`` against the
     configured remote/branch. This mirrors what ``install.sh`` already
     does for existing checkouts, so the deployed copy is a git repo by
     construction.

Safety model
------------
* **Refuse hard on a dirty working tree** — never auto-stash, never
  ``--force``, never discard the user's edits. The user must resolve a
  dirty tree themselves.
* **User data is preserved by construction.** Everything mutable lives
  outside tracked code (``data/`` configs+DB, ``.env``, ``uploads/``,
  ``logs/``) and is gitignored, so a fast-forward pull never touches it.
* **Runtime-state churn is tolerated.** A few paths ARE git-tracked yet
  mutate during normal operation (``.tofu/`` memories + file-history,
  ``outputs/``, ``overleaf_cache/``). Counting those as "dirty" would
  make every real install permanently un-updatable, so the dirty check
  classifies changes and ignores those paths — while still blocking on
  genuine edits to source (``lib/``, ``routes/``, ``static/``, …).

Nothing here restarts the process. A fast-forward that pulls ``.py``
changes needs a restart to take effect; the route layer reports
``needs_restart`` and exposes a separate, explicit restart endpoint.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import Optional

from lib.http_client import http_get
from lib.log import audit_log, get_logger, log_context

logger = get_logger(__name__)

# ── Project root (one level up from lib/) ──
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── Official source — overridable for forks / mirrors via env ──
UPDATE_REPO = os.environ.get('TOFU_UPDATE_REPO', 'rangehow/ToFu')
UPDATE_REMOTE = os.environ.get('TOFU_UPDATE_REMOTE', 'origin')
UPDATE_BRANCH = os.environ.get('TOFU_UPDATE_BRANCH', 'main')

_TAGS_URL = f'https://api.github.com/repos/{UPDATE_REPO}/tags'
_GIT_TIMEOUT = 120  # seconds — fetch/pull on a slow corp network
_PIP_TIMEOUT = 600  # seconds — pip install can be slow on a fresh env
_REQUIREMENTS = 'requirements.txt'

# ── Tracked paths that legitimately mutate at runtime. Changes confined
#    to these do NOT count as a blocking dirty tree (see module docstring). ──
_RUNTIME_STATE_PREFIXES = (
    '.tofu/',          # memories, skills, file-history backups
    'data/',           # (gitignored, but be defensive)
    'logs/',
    'uploads/',
    'outputs/',
    'overleaf_cache/',
    'static/js/bundle-',  # regenerated bundle (gitignored, defensive)
)


def _run_git(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a git command in the project root, capturing output.

    Raises FileNotFoundError if git is not installed (caller handles it).
    """
    return subprocess.run(
        ['git', *args],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def git_available() -> bool:
    """True if this is a git checkout and git is on PATH."""
    if not os.path.isdir(os.path.join(_ROOT, '.git')):
        return False
    try:
        cp = _run_git(['rev-parse', '--git-dir'])
        return cp.returncode == 0
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        logger.warning('[Update] git not available: %s', e)
        return False


def _parse_semver(tag: str) -> Optional[tuple]:
    """Parse a 'vX.Y.Z' / 'X.Y.Z' tag into a comparable tuple, or None."""
    m = re.match(r'^v?(\d+)\.(\d+)\.(\d+)', tag.strip())
    if not m:
        return None
    return tuple(int(g) for g in m.groups())


def current_version() -> str:
    """Current installed version (from the VERSION file via lib.version)."""
    try:
        from lib.version import __version__
        return __version__ or '0.0.0'
    except Exception as e:
        logger.warning('[Update] Could not read current version: %s', e)
        return '0.0.0'


def fetch_latest_release() -> Optional[dict]:
    """Fetch the newest semver tag from the official GitHub repo.

    Returns ``{'tag': 'v0.9.3', 'version': '0.9.3'}`` for the highest
    semver tag, or None on any failure (network, parse, empty list).
    Failures are logged, not raised — the caller degrades gracefully.
    """
    try:
        resp = http_get(_TAGS_URL, timeout=15,
                        headers={'Accept': 'application/vnd.github+json'})
    except Exception as e:
        logger.warning('[Update] Failed to reach GitHub tags API: %s', e)
        return None

    if resp.status_code != 200:
        logger.warning('[Update] GitHub tags API returned %s for %s',
                       resp.status_code, UPDATE_REPO)
        return None

    try:
        tags = resp.json()
    except Exception as e:
        logger.warning('[Update] Could not parse GitHub tags JSON: %s', e)
        return None

    best_tag = None
    best_ver = None
    for entry in tags or []:
        name = (entry or {}).get('name') or ''
        parsed = _parse_semver(name)
        if parsed is None:
            continue
        if best_ver is None or parsed > best_ver:
            best_ver = parsed
            best_tag = name
    if best_tag is None:
        logger.warning('[Update] No semver tags found for %s', UPDATE_REPO)
        return None

    return {'tag': best_tag, 'version': '.'.join(str(p) for p in best_ver)}


def _is_runtime_state(path: str) -> bool:
    return any(path.startswith(p) for p in _RUNTIME_STATE_PREFIXES)


def working_tree_status() -> dict:
    """Classify ``git status --porcelain`` into blocking vs. tolerated.

    Returns::

        {
          'clean': bool,              # no blocking changes
          'blocking': [path, ...],    # tracked source edits that block update
          'runtime': int,             # count of tolerated runtime-state changes
        }

    "clean" means *safe to fast-forward*, not literally pristine — it
    tolerates runtime-state churn (see _RUNTIME_STATE_PREFIXES).
    """
    try:
        cp = _run_git(['status', '--porcelain'])
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        logger.warning('[Update] git status failed: %s', e)
        # Be conservative: unknown state → treat as dirty/blocking.
        return {'clean': False, 'blocking': ['<git status unavailable>'],
                'runtime': 0}

    blocking = []
    runtime = 0
    for line in cp.stdout.splitlines():
        if not line.strip():
            continue
        # Porcelain v1: 'XY <path>' (XY is 2 status chars, then a space).
        path = line[3:].strip()
        # Renames show 'old -> new'; key the destination for classification.
        if ' -> ' in path:
            path = path.split(' -> ', 1)[1].strip()
        # Strip optional surrounding quotes git adds for odd filenames.
        path = path.strip('"')
        if _is_runtime_state(path):
            runtime += 1
        else:
            blocking.append(path)

    return {'clean': not blocking, 'blocking': blocking, 'runtime': runtime}


def check_for_update() -> dict:
    """Assemble the full update-check payload for the UI.

    Returns a dict with: current, latest, tag, update_available,
    git_available, dirty, blocking (truncated), runtime_changes, error.
    Never raises — every failure is captured in the payload.
    """
    cur = current_version()
    payload = {
        'current': cur,
        'latest': None,
        'tag': None,
        'update_available': False,
        'git_available': git_available(),
        'dirty': False,
        'blocking': [],
        'runtime_changes': 0,
        'repo': UPDATE_REPO,
        'error': None,
    }

    latest = fetch_latest_release()
    if latest is None:
        payload['error'] = 'Could not determine the latest release.'
        return payload

    payload['latest'] = latest['version']
    payload['tag'] = latest['tag']

    cur_v = _parse_semver(cur)
    latest_v = _parse_semver(latest['version'])
    if cur_v is not None and latest_v is not None:
        payload['update_available'] = latest_v > cur_v

    if payload['git_available']:
        status = working_tree_status()
        payload['dirty'] = not status['clean']
        payload['blocking'] = status['blocking'][:20]
        payload['runtime_changes'] = status['runtime']

    logger.info('[Update] check: current=%s latest=%s available=%s '
                'git=%s dirty=%s', cur, payload['latest'],
                payload['update_available'], payload['git_available'],
                payload['dirty'])
    return payload


def _head_sha() -> Optional[str]:
    """Current HEAD commit SHA, or None if it can't be read."""
    try:
        cp = _run_git(['rev-parse', 'HEAD'])
        if cp.returncode == 0:
            return cp.stdout.strip()
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        logger.warning('[Update] rev-parse HEAD failed: %s', e)
    return None


def _requirements_changed(before_sha: Optional[str],
                          after_sha: Optional[str]) -> bool:
    """True if ``requirements.txt`` differs between two commits.

    Compares by SHA range rather than scraping ``git pull`` output, which
    is locale-dependent and unreliable. If either SHA is missing or the
    diff can't be computed, returns True (install defensively rather than
    silently skip a needed dependency).
    """
    if before_sha and after_sha and before_sha == after_sha:
        return False  # nothing was pulled
    if not before_sha or not after_sha:
        logger.warning('[Update] missing commit SHA — will install '
                       'dependencies defensively')
        return True
    try:
        cp = _run_git(['diff', '--name-only', before_sha, after_sha])
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        logger.warning('[Update] requirements diff failed (%s) — will '
                       'install defensively', e)
        return True
    if cp.returncode != 0:
        logger.warning('[Update] requirements diff returned %d — will '
                       'install defensively', cp.returncode)
        return True
    changed = [p.strip() for p in cp.stdout.splitlines() if p.strip()]
    return _REQUIREMENTS in changed


def _install_requirements() -> dict:
    """Run ``pip install -r requirements.txt`` with the current interpreter.

    Returns ``{'ok': bool, 'detail': str}``. Uses ``sys.executable -m pip``
    so the install targets the SAME interpreter the server runs under
    (critical inside a conda env). Never raises — failures are captured.
    """
    req_path = os.path.join(_ROOT, _REQUIREMENTS)
    if not os.path.isfile(req_path):
        logger.info('[Update] No %s — skipping dependency install',
                    _REQUIREMENTS)
        return {'ok': True, 'detail': 'no requirements.txt'}

    cmd = [sys.executable, '-m', 'pip', 'install', '-r', req_path]
    logger.info('[Update] Installing dependencies: %s', ' '.join(cmd))
    with log_context('self_update.pip_install', logger=logger):
        try:
            cp = subprocess.run(cmd, cwd=_ROOT, capture_output=True,
                                text=True, timeout=_PIP_TIMEOUT)
        except (FileNotFoundError, subprocess.TimeoutExpired,
                subprocess.SubprocessError) as e:
            logger.error('[Update] pip install errored: %s', e, exc_info=True)
            return {'ok': False, 'detail': str(e)[:500]}

    if cp.returncode != 0:
        tail = (cp.stderr or cp.stdout or '')[-500:]
        logger.error('[Update] pip install failed (exit %d): %s',
                     cp.returncode, tail)
        return {'ok': False, 'detail': tail}

    logger.info('[Update] Dependencies installed successfully')
    return {'ok': True, 'detail': (cp.stdout or '')[-300:]}


def apply_update(progress=None) -> dict:
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
    def _emit(stage: str, status: str, detail: str = ''):
        if not progress:
            return
        try:
            progress(stage, status, detail)
        except Exception as e:
            logger.debug('[Update] progress callback failed: %s', e)

    old = current_version()
    result = {'ok': False, 'old_version': old, 'new_version': old,
              'changed': False, 'needs_restart': False,
              'error': None, 'detail': '',
              'deps_changed': False, 'deps_installed': False,
              'deps_detail': ''}

    if not git_available():
        result['error'] = ('Not a git checkout — in-place update requires '
                           'git. Re-run install.sh to update.')
        logger.warning('[Update] apply refused: git unavailable')
        _emit('fetch', 'error', result['error'])
        return result

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

    with log_context('self_update.git_pull', logger=logger):
        try:
            _emit('fetch', 'active')
            fetch_cp = _run_git(['fetch', UPDATE_REMOTE, UPDATE_BRANCH],
                                timeout=_GIT_TIMEOUT)
            if fetch_cp.returncode != 0:
                result['error'] = 'git fetch failed.'
                result['detail'] = (fetch_cp.stderr or fetch_cp.stdout)[:500]
                logger.error('[Update] git fetch failed: %s', result['detail'])
                _emit('fetch', 'error', result['detail'])
                return result
            _emit('fetch', 'done')

            _emit('pull', 'active')
            pull_cp = _run_git(
                ['pull', '--ff-only', UPDATE_REMOTE, UPDATE_BRANCH],
                timeout=_GIT_TIMEOUT)
            if pull_cp.returncode != 0:
                result['error'] = ('git pull --ff-only failed (history may '
                                   'have diverged).')
                result['detail'] = (pull_cp.stderr or pull_cp.stdout)[:500]
                logger.error('[Update] git pull failed: %s', result['detail'])
                _emit('pull', 'error', result['detail'])
                return result

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
            dep = _install_requirements()
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
              branch=UPDATE_BRANCH,
              deps_changed=result['deps_changed'],
              deps_installed=result['deps_installed'])
    logger.info('[Update] applied: %s → %s (changed=%s deps_changed=%s '
                'deps_installed=%s)', old, new, result['changed'],
                result['deps_changed'], result['deps_installed'])
    return result


__all__ = [
    'UPDATE_REPO', 'UPDATE_REMOTE', 'UPDATE_BRANCH',
    'git_available', 'current_version', 'fetch_latest_release',
    'working_tree_status', 'check_for_update', 'apply_update',
]
