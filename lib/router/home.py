"""Owner-scoped router home: the Git repository the Global Router coordinates.

Responsibility: locate and idempotently create the owner's router home under
Tofu's canonical writable data directory (``lib.runtime.runtime_paths``). The
home is an ordinary local Git repository with at least one commit, so the
existing project guards accept it as a project root and Workers of the Global
Router run in isolated worktrees of it, exactly like project Workers.

Entry points: ``router_home_location(user_id)`` (pure path, no effects),
``router_home_path(user_id)`` (creates and returns the home) and
``is_router_home(user_id, path)``.

Creation writes README.md and .gitignore, runs ``git init`` (which makes the
home its own repository top level even when the data directory lies inside a
source checkout), then reuses ``prepare_managed_repository`` for the baseline
commit, generated AGENTS.md guidance and shared-data policy. Existing files and
history are never modified. Dependencies: runtime_paths, git_runtime,
git_checkpoint_policy, integration_control (per-root lock),
project_mod.managed_repository.
"""
from __future__ import annotations

import os
import subprocess
import tempfile

from lib.conversations.project_identity import normalize_project_path
from lib.identity import require_user_id

ROUTER_HOME_DIRECTORY = 'router'
_README = '''# Global Router home

Tofu created this repository for one owner's Global Router (docs/ROUTER.md).
Workers that the router delegates for non-project work, such as summaries,
drafts and investigations, run in isolated worktrees of this repository, and
their reviewed results are published here. Projects keep their own
repositories; the router reaches them through their Project Routers.

Do not store credentials here.
'''
_GITIGNORE = '''# Tofu Global Router home. Runtime, dependency and cache trees are never versioned.
.tofu*/
node_modules/
__pycache__/
.venv/
'''


class RouterHomeUnavailable(RuntimeError):
    """The router home could not be prepared; the message states the remedy."""


def router_home_root() -> str:
    """Directory that holds every owner's router home."""
    from lib.runtime.runtime_paths import data_root
    return os.path.join(data_root(), ROUTER_HOME_DIRECTORY)


def router_home_location(user_id: int) -> str:
    """Canonical (real, normalized) home path of one owner; creates nothing."""
    owner = require_user_id(user_id, context='router home')
    return normalize_project_path(os.path.realpath(os.path.join(router_home_root(), str(owner))))


def is_router_home(user_id: int, path: str) -> bool:
    """Whether ``path`` is exactly this owner's router home identity."""
    if not isinstance(path, str) or not path.strip():
        return False
    return normalize_project_path(path.strip()) == router_home_location(user_id)


def _write_once(path: str, content: str) -> None:
    if os.path.lexists(path):
        return
    directory = os.path.dirname(path)
    handle, temporary = tempfile.mkstemp(prefix='.tofu_router_', dir=directory)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as stream:
            stream.write(content)
        # A concurrent creator writes identical bytes; whichever lands first wins.
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def _git(home: str, *args: str) -> subprocess.CompletedProcess:
    """Local metadata commands only; the baseline commit runs in the managed owner."""
    from lib.git_checkpoint_policy import git_policy_environment
    from lib.git_runtime import git_executable
    return subprocess.run([git_executable(), '-c', 'core.fsmonitor=false', '-C', home, *args],
                          capture_output=True, text=True, check=False,
                          env=git_policy_environment())


def _has_commit(home: str) -> bool:
    return _git(home, 'rev-parse', '--verify', '--quiet', 'HEAD^{commit}').returncode == 0


def router_home_path(user_id: int) -> str:
    """Create (idempotently) and return the owner's router home repository."""
    home = router_home_location(user_id)
    os.makedirs(home, exist_ok=True)
    if os.path.isdir(os.path.join(home, '.git')) and _has_commit(home):
        return home
    # The same cross-process per-root lock as managed repository preparation:
    # concurrent `git init` runs race on template and config lock files.
    from lib.integration_control import _repo_lock
    with _repo_lock(home):
        _write_once(os.path.join(home, 'README.md'), _README)
        _write_once(os.path.join(home, '.gitignore'), _GITIGNORE)
        if not os.path.lexists(os.path.join(home, '.git')):
            initialized = _git(home, 'init', '-q')
            if initialized.returncode:
                raise RouterHomeUnavailable(
                    f'Cannot initialize the router home {home}: {initialized.stderr.strip()}')
            _git(home, 'symbolic-ref', 'HEAD', 'refs/heads/main')
    from lib.project_mod.managed_repository import (
        ManagedRepositoryUnavailable, prepare_managed_repository,
    )
    try:
        prepare_managed_repository(home, refresh=False)
    except ManagedRepositoryUnavailable as error:
        raise RouterHomeUnavailable(f'{error.reason} {error.remedy}') from error
    if not _has_commit(home):
        raise RouterHomeUnavailable(f'The router home {home} has no baseline commit.')
    return home


__all__ = ['ROUTER_HOME_DIRECTORY', 'RouterHomeUnavailable', 'is_router_home',
           'router_home_location', 'router_home_path', 'router_home_root']
