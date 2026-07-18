"""lib/self_update/ — In-place self-update via ``git pull --ff-only``.

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

This module was split into a package (``lib/self_update/``) for
maintainability; this ``__init__`` re-exports every public symbol so all
existing ``from lib.self_update import X`` call sites keep working
byte-identically.

Sub-modules:
  _config        — project root, repo/remote/branch, URLs, timeouts
  _git           — git executable resolution, raw runners, HEAD sha
  _version       — semver parse, current version, release discovery
  _status        — runtime-state classification, working tree, check
  _requirements  — requirements.txt diff + pip install
  _apply         — git-pull / tarball-overlay strategies + dispatcher
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)

# HTTP helpers used by the updater — re-exported so the historical
# ``lib.self_update.http_get`` / ``lib.self_update.http_stream`` attributes
# (present when this was a single module) remain readable AND monkeypatchable
# by consumers / tests after the package split.
from lib.http_client import http_get, http_stream  # noqa: F401

# ── Shared constants ──
from lib.self_update._config import (
    UPDATE_BRANCH,
    UPDATE_REMOTE,
    UPDATE_REPO,
    _DOWNLOAD_TIMEOUT,
    _GIT_TIMEOUT,
    _PIP_TIMEOUT,
    _REQUIREMENTS,
    _ROOT,
    _TAGS_URL,
    _TARBALL_URL,
    _UPDATE_BACKUP_DIR,
)

# ── git helpers ──
from lib.self_update._git import (
    _git_exe,
    _head_sha,
    _run_git,
    _run_git_streaming,
    git_available,
)

# ── version / release discovery ──
from lib.self_update._version import (
    _fetch_latest_release_detailed,
    _parse_semver,
    current_version,
    fetch_latest_release,
)

# ── working-tree status / update check ──
from lib.self_update._status import (
    _is_runtime_state,
    check_for_update,
    working_tree_status,
)

# ── dependency handling ──
from lib.self_update._requirements import (
    _install_requirements,
    _requirements_changed,
)

# ── apply strategies + dispatcher ──
from lib.self_update._apply import (
    _apply_via_git,
    _apply_via_tarball,
    _overlay_skip,
    apply_update,
)

# Re-exported from lib.runtime_layout (single source of truth) so existing
# call sites / tests that reference the update skip-lists keep working
# (byte-identical to the historical literals).
from lib.runtime_layout import (  # noqa: F401
    OVERLAY_SKIP_PREFIXES as _OVERLAY_SKIP_PREFIXES,
)
from lib.runtime_layout import (  # noqa: F401
    RUNTIME_STATE_PREFIXES as _RUNTIME_STATE_PREFIXES,
)

__all__ = [
    'UPDATE_REPO', 'UPDATE_REMOTE', 'UPDATE_BRANCH',
    'git_available', 'current_version', 'fetch_latest_release',
    '_fetch_latest_release_detailed',
    'working_tree_status', 'check_for_update', 'apply_update',
    # Re-exported from lib.runtime_layout (single source of truth) so existing
    # call sites / tests that reference the update skip-lists keep working.
    '_RUNTIME_STATE_PREFIXES', '_OVERLAY_SKIP_PREFIXES',
    # Additional symbols kept importable for consumers / tests.
    '_overlay_skip',
    # Sub-module internals also re-exported for completeness.
    '_git_exe', '_run_git', '_run_git_streaming', '_head_sha', '_parse_semver', '_is_runtime_state',
    '_requirements_changed', '_install_requirements',
    '_apply_via_git', '_apply_via_tarball',
    # Config constants.
    '_ROOT', '_TAGS_URL', '_TARBALL_URL', '_GIT_TIMEOUT', '_PIP_TIMEOUT',
    '_REQUIREMENTS', '_DOWNLOAD_TIMEOUT', '_UPDATE_BACKUP_DIR',
    # HTTP helpers (re-exported for patchability).
    'http_get', 'http_stream',
]
