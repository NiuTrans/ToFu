"""lib/self_update/_status.py — working-tree status and update check.

``_is_runtime_state`` (delegates to lib.runtime_layout), ``working_tree_status``
(blocking vs. tolerated churn) and ``check_for_update`` (the full UI payload).
"""

from __future__ import annotations

import subprocess

from lib.runtime_layout import is_runtime_state as _rl_is_runtime_state
from lib.self_update._config import UPDATE_REPO
from lib.self_update._git import _run_git, git_available
from lib.self_update._version import (
    _fetch_latest_release_detailed,
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
    cur = _facade('current_version', current_version)()
    _is_git = _facade('git_available', git_available)()
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

    latest, err = _facade('_fetch_latest_release_detailed',
                          _fetch_latest_release_detailed)()
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
        status = _facade('working_tree_status', working_tree_status)()
        payload['dirty'] = not status['clean']
        payload['blocking'] = status['blocking'][:20]
        payload['runtime_changes'] = status['runtime']

    logger.info('[Update] check: current=%s latest=%s available=%s '
                'git=%s dirty=%s', cur, payload['latest'],
                payload['update_available'], payload['git_available'],
                payload['dirty'])
    return payload
