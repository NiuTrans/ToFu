"""lib/runtime_layout.py — Single source of truth for the install-tree
runtime-state ⇄ shippable-code boundary.

Sibling of :mod:`lib.agent_artifacts`. Where ``agent_artifacts`` enumerates the
**project-local** artifacts the assistant writes into a *user's* project (the
``.tofu*`` prefix family), THIS module enumerates the paths under the **Tofu
install root itself** that are mutable user / runtime state rather than
shippable source:

  * ``data/``            — databases, per-project config, fetched-file staging.
  * ``logs/``            — application logs (contain queries / personal content).
  * ``uploads/``         — user-uploaded images.
  * ``outputs/``         — eval / smoke-test output (transient).
  * ``overleaf_cache/``  — fetched Overleaf projects (regenerable cache).
  * ``lib/.project_sessions/`` — per-project undo/redo history (pre-image blobs).
  * ``.tofu/`` (+ any ``.tofu*``) — the assistant's own memories / file-history
    in the install tree, delegated to :func:`lib.agent_artifacts.is_agent_artifact`.

Why this exists
---------------
The classification *"which install-tree paths are mutable state an update must
NEVER clobber"* was historically **hand-duplicated** across three consumers
that had to be kept in sync by hand:

  * ``lib/self_update.py`` — ``_RUNTIME_STATE_PREFIXES`` (dirty-tree classify)
    and ``_OVERLAY_SKIP_PREFIXES`` (tarball-overlay skip).
  * ``export.py``          — the runtime-state subset of ``*_EXCLUDE_DIRS``.
  * ``.gitignore``         — ``data/`` / ``logs/`` / ``uploads/`` / … entries.

Drift between them is exactly what makes an auto-update fragile: forget an
entry in the update skip-list and a whole-tree overlay clobbers the user's DB;
forget it in ``.gitignore`` and the state leaks into version control. This
module fixes that the same way ``agent_artifacts`` fixed the ``.tofu*`` family:
define the boundary ONCE here, and have every consumer derive from it. Adding a
new runtime dir next year is a one-line edit to :data:`INSTALL_STATE`, and every
consumer picks it up with no further code change.

Scope note — this is the "what is mutable state" axis ONLY. It is deliberately
NOT the export sanitizer's "what is shippable in a public release" axis: dirs
like ``sundries/`` / ``benchmarks/`` / ``debug/`` / ``promo/`` are excluded from
exports because they are not part of the product, not because they are user
state. Those stay in ``export.py`` and are NOT unified here — conflating the two
axes would be the wrong abstraction.

No ``lib.*`` imports beyond :mod:`lib.agent_artifacts` (itself stdlib-only), so
any producer or consumer can import this without an import cycle.
"""

from __future__ import annotations

from collections import namedtuple
from pathlib import Path

from lib.agent_artifacts import (
    GITIGNORE_PATTERN as _ARTIFACT_GITIGNORE,
    is_agent_artifact,
)

_INSTALL_ROOT = str(Path(__file__).resolve().parent.parent)


def install_root() -> str:
    """Return the source/bundle root containing ``lib/`` and ``server.py``."""
    return _INSTALL_ROOT

__all__ = [
    'INSTALL_STATE',
    'install_root',
    'RUNTIME_STATE_PREFIXES',
    'OVERLAY_SKIP_PREFIXES',
    'is_runtime_state',
    'is_overlay_skipped',
    'gitignore_lines',
]

# A single runtime-state entry.
#   prefix       — project-root-relative, '/'-separated. A trailing '/' marks a
#                  directory (matched as ``rel == 'data' or rel.startswith('data/')``);
#                  no trailing slash marks a literal filename-prefix.
#   category     — 'data' | 'logs' | 'uploads' | 'cache' | 'output' | 'build'.
#   tracked      — whether the path is (or historically was) git-tracked. Purely
#                  informational today; consumers key on membership, not this.
#   comment      — human-readable rationale, reused as the ``.gitignore`` comment.
RuntimeEntry = namedtuple('RuntimeEntry', 'prefix category tracked comment')

# ── THE registry. Edit HERE to add a new install-tree runtime-state path. ──
# NOTE: ``.tofu/`` is intentionally ABSENT from this list — it is covered by
# ``lib.agent_artifacts.is_agent_artifact`` (the ``.tofu*`` prefix family), which
# ``is_runtime_state`` / ``is_overlay_skipped`` consult separately so the two
# registries compose without duplicating the prefix.
INSTALL_STATE = (
    RuntimeEntry('data/', 'data', False,
                 'databases (*.db), per-project config, fetched-file staging'),
    RuntimeEntry('logs/', 'logs', False,
                 'application logs (contain queries / personal content)'),
    RuntimeEntry('uploads/', 'uploads', False,
                 'user-uploaded images'),
    RuntimeEntry('outputs/', 'output', False,
                 'eval / smoke-test output (transient)'),
    RuntimeEntry('overleaf_cache/', 'cache', False,
                 'fetched Overleaf projects (regenerable cache)'),
    # Nested under lib/ (not a top-level dir): the per-project undo/redo store
    # written live by lib/project_mod/modifications.py. Its resolved path comes
    # from runtime_paths.project_sessions_root() — in-tree here, or
    # data/project_sessions (covered by the data/ entry) when relocated. The
    # multi-segment prefix is matched anchored by _matches (startswith), and its
    # basename '.project_sessions' is what export.py prunes on.
    RuntimeEntry('lib/.project_sessions/', 'data', False,
                 'per-project undo/redo history — <session>/modifications.json '
                 'pre-image blobs of edited files (personal conversation content)'),
)

# Ordered tuple of the raw prefixes — the drop-in replacement for
# ``self_update._RUNTIME_STATE_PREFIXES``. Preserves the historical ordering
# (``.tofu/`` first) so the derived value is byte-identical to the old literal.
RUNTIME_STATE_PREFIXES = ('.tofu/',) + tuple(e.prefix for e in INSTALL_STATE)

# Paths NEVER overwritten by a whole-tree overlay but which are NOT user
# "runtime state" in the classify sense: VCS metadata, virtualenvs, bytecode
# caches, and the updater's own backup dir. Kept distinct from
# RUNTIME_STATE_PREFIXES so ``is_runtime_state`` (dirty-tree tolerance) does not
# wrongly treat e.g. a ``.git/`` change as tolerable churn.
_VCS_BUILD_PREFIXES = (
    '.git/', '.venv/', 'venv/', 'node_modules/', '__pycache__/',
    '.update_backup/',
)

# The drop-in replacement for ``self_update._OVERLAY_SKIP_PREFIXES``.
OVERLAY_SKIP_PREFIXES = RUNTIME_STATE_PREFIXES + _VCS_BUILD_PREFIXES


def _normalize(rel: str) -> str:
    """Normalize an install-root-relative path to '/'-separated, no leading './'.

    Mirrors the normalization ``self_update._overlay_skip`` applied inline
    (a literal ``./`` strip — NOT ``lstrip('./')``, which is a char-set that
    would corrupt a leading ``.tofu``).
    """
    rel = rel.replace('\\', '/')
    if rel.startswith('./'):
        rel = rel[2:]
    return rel


def _matches(rel: str, prefixes) -> bool:
    """True if ``rel`` equals a dir prefix (minus trailing '/') or is under it,
    or starts with a filename-prefix entry (no trailing '/')."""
    for p in prefixes:
        if p.endswith('/'):
            if rel == p.rstrip('/') or rel.startswith(p):
                return True
        elif rel.startswith(p):
            return True
    return False


def is_runtime_state(rel: str) -> bool:
    """True if ``rel`` (install-root-relative) is mutable runtime/user state.

    Changes confined to these paths do NOT count as a blocking dirty tree for
    an in-place update, and a whole-tree overlay preserves them. Also returns
    True for any ``.tofu*`` agent artifact (delegated to
    :func:`lib.agent_artifacts.is_agent_artifact`, prefix-matched at any depth).
    """
    rel = _normalize(rel)
    if not rel:
        return False
    if any(is_agent_artifact(seg) for seg in rel.split('/') if seg):
        return True
    return _matches(rel, RUNTIME_STATE_PREFIXES)


def is_overlay_skipped(rel: str) -> bool:
    """True if ``rel`` must NOT be overwritten by a whole-tree (tarball) overlay.

    Superset of :func:`is_runtime_state` that additionally skips VCS metadata,
    virtualenvs, bytecode caches, and the updater's backup dir.
    """
    rel = _normalize(rel)
    if not rel:
        return False
    if any(is_agent_artifact(seg) for seg in rel.split('/') if seg):
        return True
    return _matches(rel, OVERLAY_SKIP_PREFIXES)


def gitignore_lines() -> list[str]:
    """Render the runtime-state block for a generated ``.gitignore``.

    One commented entry per :data:`INSTALL_STATE` dir, plus the single
    ``.tofu*`` glob that covers every present/future agent artifact. Consumers
    that generate ignore files should emit this block rather than re-listing
    the paths, so the ignore set can never drift from the update skip-list.
    """
    lines: list[str] = []
    for e in INSTALL_STATE:
        lines.append(f'# {e.comment}')
        # Directory entries render directly; filename-prefix entries gain '*'.
        lines.append(e.prefix if e.prefix.endswith('/') else e.prefix + '*')
    lines.append('# assistant memories / file-history / trash (any .tofu* artifact)')
    lines.append(_ARTIFACT_GITIGNORE)
    return lines
