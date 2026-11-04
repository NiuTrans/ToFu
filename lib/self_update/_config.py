"""lib/self_update/_config.py — shared constants for the updater.

Project root, update-source settings (official repo, explicit env
overrides, no-upstream fallback), GitHub API URLs and timeouts. Kept in one
place so every sub-module imports the SAME values (no drift).
"""

from __future__ import annotations

import os
from typing import NamedTuple, Optional

from lib.log import get_logger
from lib.runtime.runtime_layout import install_root

logger = get_logger(__name__)

# ── Project root (contains lib/, static/, server.py) ──
_ROOT = install_root()

# ── Update source ──
class UpdateSourceOverrides(NamedTuple):
    """Explicit TOFU_UPDATE_REPO / _REMOTE / _BRANCH values; None when unset."""

    repo: Optional[str]
    remote: Optional[str]
    branch: Optional[str]


def update_source_overrides() -> UpdateSourceOverrides:
    """Read the explicit update-source settings from the environment.

    An explicit repo keeps a git checkout on that repo's release-tags check;
    an explicit remote/branch replaces the matching half of the checkout's
    own upstream (``_git.resolve_update_upstream``). Read per call so every
    check and apply sees the current process environment.
    """
    return UpdateSourceOverrides(
        repo=os.environ.get('TOFU_UPDATE_REPO') or None,
        remote=os.environ.get('TOFU_UPDATE_REMOTE') or None,
        branch=os.environ.get('TOFU_UPDATE_BRANCH') or None,
    )


# GitHub repository whose release tags and tarballs are the official source.
UPDATE_REPO = update_source_overrides().repo or 'rangehow/ToFu'
# Followed only by a git checkout without an upstream (detached HEAD or a
# branch that tracks nothing).
_FALLBACK_REMOTE = 'origin'
_FALLBACK_BRANCH = 'main'

_TAGS_URL = f'https://api.github.com/repos/{UPDATE_REPO}/tags'
_GIT_TIMEOUT = 120  # seconds — fetch/pull on a slow corp network
_PIP_TIMEOUT = 600  # seconds — pip install can be slow on a fresh env
_REQUIREMENTS = 'requirements.txt'

# ── Tracked paths that legitimately mutate at runtime. Changes confined
#    to these do NOT count as a blocking dirty tree (see module docstring).
#    The classification now lives in lib/runtime/runtime_layout.py (the single source
#    of truth shared with export.py + .gitignore generation) so the update
#    skip-list can never drift from the ignore/export sets; the names are
#    re-exported here (byte-identical to the historical literals) for the few
#    call sites and tests that reference them directly. ──

# ── Tarball-overlay fallback (non-git deployments) ──
# Exported copies and zip downloads have no .git, so ``git pull`` is
# impossible. For those we download the release tarball and overlay tracked
# source onto the project root (see _apply_via_tarball).
_TARBALL_URL = f'https://api.github.com/repos/{UPDATE_REPO}/tarball/{{ref}}'
_DOWNLOAD_TIMEOUT = 300  # seconds — release tarball on a slow corp network
_UPDATE_BACKUP_DIR = '.update_backup'  # per-run backups of replaced files
# Paths NEVER overwritten by a tarball overlay: user/runtime state that is
# either gitignored (absent from the tarball) or git-tracked yet mutated
# locally (.tofu/ memories+skills), plus VCS metadata / venvs / the updater's
# own backup dir. Sourced from lib/runtime_layout (imported above) so the
# overlay skip-list stays in lock-step with the dirty-tree classifier and the
# export / .gitignore sets. Overwriting these would clobber the user's data.
