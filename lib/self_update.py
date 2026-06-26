"""lib/self_update.py — In-place self-update via ``git pull --ff-only``.

Backs the topbar "Update" button. Two responsibilities:

  1. **Check** — compare the local ``VERSION`` against the newest release
     tag published on the official GitHub repo (``UPDATE_REPO``). We use
     the *tags* API (``/repos/<owner>/<repo>/tags``) rather than the raw
     ``VERSION`` file so the check respects actual cut releases, not
     in-progress ``main`` commits. ``/releases/latest`` is intentionally
     NOT used because the project tags releases without creating GitHub
     "Release" objects (the endpoint 404s).

  2. **Apply** — two strategies, chosen automatically by ``apply_update``:
     * **git checkout** → ``git fetch`` + ``git pull --ff-only`` against
       the configured remote/branch (mirrors what ``install.sh`` does for
       existing checkouts).
     * **non-git deployment** (exported copy, zip download — no ``.git``)
       → download the release tarball from GitHub and **overlay** tracked
       source onto the project root, backing up every replaced file to
       ``.update_backup/<ts>/`` so the overlay is reversible. This makes
       exported/zip deployments updatable, not just git checkouts.

Safety model
------------
* **git mode — refuse hard on a dirty working tree** — never auto-stash,
  never ``--force``, never discard the user's edits. The user must resolve
  a dirty tree themselves.
* **tarball mode — never destructive, always reversible** — validate the
  downloaded tree (must carry ``server.py`` / ``VERSION`` / ``lib/``)
  BEFORE touching anything; abort cleanly on a partial/corrupt download.
  Each replaced file is copied to ``.update_backup/<ts>/`` before being
  overwritten. A tarball overlay **cannot delete** files removed upstream
  (git pull can) — a documented, accepted limitation of the fallback.
* **User data is preserved by construction.** Everything mutable lives
  outside tracked code (``data/`` configs+DB, ``.env``, ``uploads/``,
  ``logs/``) and is gitignored, so neither a fast-forward pull nor a
  tarball overlay touches it. The overlay additionally skips
  ``_OVERLAY_SKIP_PREFIXES`` (``.tofu/`` memories+skills, ``.git/``, venvs)
  even if the archive happens to carry them.
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

from lib.agent_artifacts import is_agent_artifact
from lib.http_client import http_get, http_stream
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

# ── Tarball-overlay fallback (non-git deployments) ──
# Exported copies and zip downloads have no .git, so ``git pull`` is
# impossible. For those we download the release tarball and overlay tracked
# source onto the project root (see _apply_via_tarball).
_TARBALL_URL = f'https://api.github.com/repos/{UPDATE_REPO}/tarball/{{ref}}'
_DOWNLOAD_TIMEOUT = 300  # seconds — release tarball on a slow corp network
_UPDATE_BACKUP_DIR = '.update_backup'  # per-run backups of replaced files

# Paths NEVER overwritten by a tarball overlay: user/runtime state that is
# either gitignored (absent from the tarball) or git-tracked yet mutated
# locally (.tofu/ memories+skills). Overwriting these would clobber the
# user's data, so they are skipped even if the archive carries them.
_OVERLAY_SKIP_PREFIXES = _RUNTIME_STATE_PREFIXES + (
    '.git/', '.venv/', 'venv/', 'node_modules/', '__pycache__/',
    _UPDATE_BACKUP_DIR + '/',
)


_git_exe_cache: Optional[str] = None


def _git_exe() -> str:
    """Resolve the git executable, falling back to common install dirs.

    On Windows the server is often launched from a context that lacks
    Git's bin/ on PATH (e.g. a double-clicked launcher), so a bare
    ``'git'`` would raise FileNotFoundError and make the UI wrongly
    report "not a git checkout". We therefore probe PATH via
    ``shutil.which`` first, then the standard Windows install locations.
    Result is cached for the process lifetime. Returns ``'git'`` as a
    last resort so the OS still gets a chance to resolve it.
    """
    global _git_exe_cache
    if _git_exe_cache:
        return _git_exe_cache

    import shutil
    found = shutil.which('git')
    if found:
        _git_exe_cache = found
        return found

    candidates = []
    if os.name == 'nt':
        for base in (os.environ.get('ProgramFiles', r'C:\Program Files'),
                     os.environ.get('ProgramFiles(x86)',
                                    r'C:\Program Files (x86)'),
                     os.environ.get('LocalAppData', '')):
            if base:
                candidates.append(os.path.join(base, 'Git', 'cmd', 'git.exe'))
                candidates.append(os.path.join(base, 'Git', 'bin', 'git.exe'))
    for c in candidates:
        if c and os.path.isfile(c):
            _git_exe_cache = c
            logger.info('[Update] Resolved git via fallback: %s', c)
            return c

    _git_exe_cache = 'git'
    return 'git'


def _run_git(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a git command in the project root, capturing output.

    Raises FileNotFoundError if git is not installed (caller handles it).
    """
    return subprocess.run(
        [_git_exe(), *args],
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


def _fetch_latest_release_detailed() -> tuple:
    """Fetch the newest semver tag, returning ``(payload, error)``.

    On success ``payload`` is ``{'tag': 'v0.9.3', 'version': '0.9.3'}`` for
    the highest semver tag and ``error`` is ``None``. On failure ``payload``
    is ``None`` and ``error`` is a dict ``{'kind', 'detail', 'status'?}``
    that names the CONCRETE cause so the UI can tell the user exactly why
    the check failed instead of a vague "try again later". ``kind`` is one
    of ``network`` (couldn't reach GitHub at all), ``rate_limited``
    (HTTP 403/429), ``http`` (other non-200), ``parse`` (unreadable JSON),
    or ``no_tags`` (repo has no semver tags). Never raises.
    """
    try:
        resp = http_get(_TAGS_URL, timeout=15,
                        headers={'Accept': 'application/vnd.github+json'})
    except Exception as e:
        logger.warning('[Update] Failed to reach GitHub tags API: %s', e)
        return None, {'kind': 'network', 'detail': str(e)[:300]}

    if resp.status_code != 200:
        logger.warning('[Update] GitHub tags API returned %s for %s',
                       resp.status_code, UPDATE_REPO)
        kind = 'rate_limited' if resp.status_code in (403, 429) else 'http'
        return None, {'kind': kind, 'status': resp.status_code,
                      'detail': f'HTTP {resp.status_code} from {_TAGS_URL}'}

    try:
        tags = resp.json()
    except Exception as e:
        logger.warning('[Update] Could not parse GitHub tags JSON: %s', e)
        return None, {'kind': 'parse', 'detail': str(e)[:300]}

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
        return None, {'kind': 'no_tags', 'detail': UPDATE_REPO}

    return ({'tag': best_tag, 'version': '.'.join(str(p) for p in best_ver)},
            None)


def fetch_latest_release() -> Optional[dict]:
    """Fetch the newest semver tag from the official GitHub repo.

    Returns ``{'tag': 'v0.9.3', 'version': '0.9.3'}`` for the highest
    semver tag, or None on any failure (network, parse, empty list).
    Failures are logged, not raised — the caller degrades gracefully.
    Thin wrapper over :func:`_fetch_latest_release_detailed` that drops the
    error detail (callers that need the reason use the detailed variant).
    """
    payload, _err = _fetch_latest_release_detailed()
    return payload


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
    git_available, update_method, dirty, blocking (truncated),
    runtime_changes, error, error_kind, error_detail. ``update_method`` is
    ``'git'`` for a checkout or ``'tarball'`` for a non-git deployment —
    both are updatable, so the UI shows the Apply button either way (only a
    git dirty tree blocks). On a failed release check ``error_kind`` names
    the concrete cause (``network`` / ``rate_limited`` / ``http`` /
    ``parse`` / ``no_tags``) and ``error_detail`` carries the raw reason so
    the UI can tell the user EXACTLY what went wrong. Never raises — every
    failure is captured in the payload.
    """
    cur = current_version()
    _is_git = git_available()
    payload = {
        'current': cur,
        'latest': None,
        'tag': None,
        'update_available': False,
        'git_available': _is_git,
        'update_method': 'git' if _is_git else 'tarball',
        'dirty': False,
        'blocking': [],
        'runtime_changes': 0,
        'repo': UPDATE_REPO,
        'error': None,
        'error_kind': None,
        'error_detail': None,
    }

    latest, err = _fetch_latest_release_detailed()
    if latest is None:
        kind = (err or {}).get('kind') or 'network'
        payload['error_kind'] = kind
        payload['error_detail'] = (err or {}).get('detail') or ''
        # A human-readable summary keyed on the concrete cause. The UI
        # prefers error_kind for its own localized copy, but error stays
        # populated as a sensible English fallback.
        payload['error'] = {
            'network': 'Could not reach GitHub to check for updates '
                       '(network/connection error).',
            'rate_limited': 'GitHub rate-limited the update check. '
                            'Try again in a few minutes.',
            'http': 'GitHub returned an unexpected response to the '
                    'update check.',
            'parse': 'GitHub returned an unreadable response to the '
                     'update check.',
            'no_tags': 'The update repository has no released versions yet.',
        }.get(kind, 'Could not determine the latest release.')
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
    ``_OVERLAY_SKIP_PREFIXES``)."""
    rel = rel.replace(os.sep, '/')
    if rel.startswith('./'):
        rel = rel[2:]
    # Never overwrite an agent-written artifact (.tofu*), at any depth —
    # prefix-based so future artifacts are covered without re-listing.
    if any(is_agent_artifact(seg) for seg in rel.split('/') if seg):
        return True
    return any(rel == p.rstrip('/') or rel.startswith(p)
               for p in _OVERLAY_SKIP_PREFIXES)


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
    import shutil
    import tarfile
    import tempfile
    import time
    from pathlib import Path

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
              'error': None, 'detail': '', 'method': 'tarball',
              'deps_changed': False, 'deps_installed': False,
              'deps_detail': ''}

    url = _TARBALL_URL.format(ref=tag)
    tmp_root = tempfile.mkdtemp(prefix='tofu-update-')
    tar_path = os.path.join(tmp_root, 'release.tar.gz')

    try:
        # ── 1. Download ──────────────────────────────────────────────
        _emit('fetch', 'active')
        with log_context('self_update.tarball_download', logger=logger):
            try:
                with http_stream('GET', url, timeout=_DOWNLOAD_TIMEOUT,
                                 headers={'Accept': 'application/vnd.github+json'}) as resp:
                    if resp.status_code != 200:
                        result['error'] = 'Could not download the release archive.'
                        result['detail'] = f'HTTP {resp.status_code} from {url}'
                        logger.error('[Update] tarball download HTTP %s for %s',
                                     resp.status_code, url)
                        _emit('fetch', 'error', result['detail'])
                        return result
                    total = 0
                    with open(tar_path, 'wb') as fh:
                        for chunk in resp.iter_content(64 * 1024):
                            if chunk:
                                fh.write(chunk)
                                total += len(chunk)
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

        # Overlay every file, backing up replacements first.
        backup_dir = os.path.join(_ROOT, _UPDATE_BACKUP_DIR,
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
                if _overlay_skip(rel):
                    skipped += 1
                    continue
                dest = os.path.join(_ROOT, rel)
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
                                f'{os.path.relpath(backup_dir, _ROOT)}')
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
        # We can't cheaply diff requirements.txt against the prior tree
        # (no git), so if anything changed, install defensively.
        if result['changed']:
            result['deps_changed'] = True
            _emit('deps', 'active')
            dep = _install_requirements()
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
    if git_available():
        return _apply_via_git(progress=progress)

    # No git — fall back to the tarball overlay. Resolve the target tag from
    # the release check (the same source the badge uses).
    logger.info('[Update] no git checkout — using tarball-overlay fallback')
    latest = fetch_latest_release()
    if not latest or not latest.get('tag'):
        old = current_version()
        return {'ok': False, 'old_version': old, 'new_version': old,
                'changed': False, 'needs_restart': False, 'method': 'tarball',
                'error': 'Could not determine the latest release to download.',
                'detail': '', 'deps_changed': False, 'deps_installed': False,
                'deps_detail': ''}
    return _apply_via_tarball(latest['tag'], progress=progress)


__all__ = [
    'UPDATE_REPO', 'UPDATE_REMOTE', 'UPDATE_BRANCH',
    'git_available', 'current_version', 'fetch_latest_release',
    '_fetch_latest_release_detailed',
    'working_tree_status', 'check_for_update', 'apply_update',
]
