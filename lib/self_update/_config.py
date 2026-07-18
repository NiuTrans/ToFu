"""lib/self_update/_config.py — shared constants for the updater.

Project root, official-source repo/remote/branch (env-overridable),
GitHub API URLs and timeouts. Kept in one place so every sub-module
imports the SAME values (no drift).
"""

from __future__ import annotations

import os

from lib.log import get_logger

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
#    to these do NOT count as a blocking dirty tree (see module docstring).
#    The classification now lives in lib/runtime_layout.py (the single source
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
