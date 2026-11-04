"""lib/self_update/_status.py — working-tree status and update check.

``_is_runtime_state`` (delegates to lib.runtime.runtime_layout), ``working_tree_status``
(blocking vs. tolerated churn), ``_fetch_latest_version_detailed`` (picks the
release-tags or git-upstream source) and ``check_for_update`` (the full UI
payload).
"""

from __future__ import annotations

import re
import subprocess

from lib.runtime.runtime_layout import is_runtime_state as _rl_is_runtime_state
from lib.self_update._config import UPDATE_REPO, update_source_overrides
from lib.self_update._git import (
    _remote_url,
    _run_git,
    git_available,
    resolve_update_upstream,
)
from lib.self_update._version import (
    _fetch_latest_release_detailed,
    _fetch_upstream_version_detailed,
    _parse_semver,
    current_version,
)

from lib.log import get_logger

logger = get_logger(__name__)


def _facade(name, default):
    """Resolve ``name`` from the ``lib.self_update`` package namespace so tests
    that monkeypatch the facade (e.g. ``su.git_available = ...``,
    ``su.working_tree_status = ...``) transparently affect ``check_for_update``
    after the package split. Falls back to ``default`` (this sub-module's own)."""
    import lib.self_update as _pkg
    return getattr(_pkg, name, default)


def _is_runtime_state(path: str) -> bool:
    return _rl_is_runtime_state(path)


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


# ``github.com/<owner>/<repo>`` as an https/http/git/ssh URL or the scp-like
# ``[user@]github.com:<owner>/<repo>``, with an optional ``.git`` suffix.
_GITHUB_REPO_URL_RE = re.compile(
    r'^(?:(?:https?|git|ssh|git\+ssh|ssh\+git)://(?:[^@/]+@)?github\.com(?::\d+)?/'
    r'|(?:[^@/:]+@)?github\.com:/?)'
    r'(?P<repo>[^/:]+/[^/]+?)(?:\.git)?/?$',
    re.IGNORECASE)


def _is_official_github_url(url: str) -> bool:
    """True when ``url`` names the official repository ``UPDATE_REPO`` on GitHub."""
    match = _GITHUB_REPO_URL_RE.match(url.strip())
    return bool(match) and match.group('repo').lower() == UPDATE_REPO.lower()


def _fetch_latest_version_detailed(git_checkout: bool) -> tuple:
    """``(payload, error)`` from the source this install updates from.

    A git checkout follows ``resolve_update_upstream`` — the remote/branch
    ``_apply_via_git`` pulls. Unless that remote is the official GitHub
    repository, or ``TOFU_UPDATE_REPO`` names a repository explicitly, the
    upstream is fetched and its ``VERSION`` compared, so an internal mirror
    whose every push is a release snapshot reports its own version. The
    release-tags check stays the source for the official repository and for
    non-git installs, which update from release tarballs.
    """
    if git_checkout and update_source_overrides().repo is None:
        upstream = resolve_update_upstream()
        url = _remote_url(upstream.remote)
        if not (url and _is_official_github_url(url)):
            return _fetch_upstream_version_detailed(upstream)
    return _facade('_fetch_latest_release_detailed',
                   _fetch_latest_release_detailed)()


# The endpoint is polled on an interval; the two expensive probes — the
# latest-version lookup (GitHub tags request or upstream ``git fetch``) and
# ``git status`` (subprocess) — change far slower than the poll cadence and,
# on an IO/CPU-starved host, each held the request thread open 2-3 s
# (2026-09-24 SLOW log). Cache both briefly: the *payload* is rebuilt every
# call (current version / dirty flags stay fresh), only the slow probes are
# reused. A new release or a newly-dirtied tree showing up to TTL later is
# harmless for a status poll.
_LATEST_RELEASE_TTL_S = 300.0
_TREE_STATUS_TTL_S = 30.0
_latest_release_cache: dict = {'at': 0.0, 'value': None, 'err': None}
_tree_status_cache: dict = {'at': 0.0, 'value': None}


def _latest_release_cached(git_checkout: bool):
    import time as _time
    now = _time.monotonic()
    if now - _latest_release_cache['at'] < _LATEST_RELEASE_TTL_S:
        return _latest_release_cache['value'], _latest_release_cache['err']
    value, err = _fetch_latest_version_detailed(git_checkout)
    _latest_release_cache.update(
        {'at': now, 'value': value, 'err': err})
    return value, err


def _working_tree_status_cached() -> dict:
    import time as _time
    now = _time.monotonic()
    if now - _tree_status_cache['at'] < _TREE_STATUS_TTL_S:
        return _tree_status_cache['value']
    value = _facade('working_tree_status', working_tree_status)()
    _tree_status_cache.update({'at': now, 'value': value})
    return value


def check_for_update(*, repository=None) -> dict:
    """Assemble the full update-check payload for the UI.

    ``repository`` is this request's ``observe_repository()`` result; with it
    the checkout probe reuses that git process instead of spawning its own.

    Returns a dict with: current, latest, tag, update_available,
    git_available, update_method, dirty, blocking (truncated),
    runtime_changes, error, error_kind, error_detail. ``update_method`` is
    ``'git'`` for a checkout or ``'tarball'`` for a non-git deployment —
    both are updatable, so the UI shows the Apply button either way (only a
    git dirty tree blocks). ``latest`` comes from the checkout's git upstream
    or from release tags (``_fetch_latest_version_detailed``); for a git
    upstream ``tag`` is the fetched commit SHA. On a failed check
    ``error_kind`` names the concrete cause (``network`` / ``rate_limited`` /
    ``http`` / ``parse`` / ``no_tags``) and ``error_detail`` carries the raw
    reason so the UI can tell the user EXACTLY what went wrong. Never raises
    — every failure is captured in the payload.
    """
    cur = _facade('current_version', current_version)()
    _is_git = (_facade('git_available', git_available)() if repository is None
               else git_available(repository))
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

    latest, err = _latest_release_cached(_is_git)
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
        status = _working_tree_status_cached()
        payload['dirty'] = not status['clean']
        payload['blocking'] = status['blocking'][:20]
        payload['runtime_changes'] = status['runtime']

    logger.info('[Update] check: current=%s latest=%s available=%s '
                'git=%s dirty=%s', cur, payload['latest'],
                payload['update_available'], payload['git_available'],
                payload['dirty'])
    return payload
