"""Single source of truth for the assistant's project-local artifacts.

The assistant writes a handful of hidden directories / files **into the
user's project directory** (NOT into the Tofu install) as it works:

  * ``.tofu/``          — file-history copy-backups (``.tofu/file-history``)
                          and memories / skills (``.tofu/skills``); written by
                          ``lib/file_history/store.py`` and ``lib/memory/storage.py``.
  * ``.tofu_trash/``    — recoverable-delete bin (the ``rm`` → trash shim);
                          written by ``lib/project_mod/run_command.py``.
  * ``.tofu_sandbox/``  — restricted-run PATH shims; written by
                          ``lib/project_mod/portable_sandbox.py``.
  * ``.tofu_env.json``  — per-host conda-env marker (absolute paths inside);
                          written near ``server.py`` / ``bootstrap.py``.

None of these are source — they are per-developer / per-host working state.
Many independent mechanisms must recognise them as "agent junk, not project
code": ``.gitignore`` generation, the three-level export sanitizer, the
self-update preserve/skip lists, the MCP vendor-copy excludes, and search /
scan skipping. Historically each of those kept its OWN hardcoded list of
names, so adding a NEW artifact meant editing ~5 files — and forgetting one
silently leaked the artifact (committed to git, copied into exports, counted
as a dirty tree blocking updates).

★ THE CONVENTION (this is what makes the whole thing future-proof):
  **Every project-local artifact the assistant writes MUST be named with the
  ``.tofu`` prefix.** Given that single rule, consumers can recognise present
  AND future artifacts mechanically — by prefix — instead of enumerating
  exact names. A new ``.tofu_<whatever>`` added next year is handled by every
  consumer automatically, with no code change, as long as it follows the
  prefix.

This module has NO ``lib.*`` imports on purpose (stdlib only) so any producer
or consumer can import it without risking an import cycle.
"""

from __future__ import annotations

import os

# The reserved prefix. Anything a basename starting with this is treated as an
# agent-written project-local artifact by every consumer below.
ARTIFACT_PREFIX = '.tofu'

# ── Canonical names (import these in producers so a name is defined ONCE) ──
# Consumers should prefer ``is_agent_artifact()`` over these literals; the
# constants exist for the few call sites that genuinely need an exact name
# (e.g. building a specific path).
FILE_HISTORY_ROOT_DIR = '.tofu'        # houses file-history/ and skills/
TRASH_DIR = '.tofu_trash'
SANDBOX_DIR = '.tofu_sandbox'
ENV_MARKER_FILE = '.tofu_env.json'
# Rolling, bounded personal-preference profile ("what the assistant knows
# about you"). Distinct from the searched task-lesson store: this single
# file is small, always-injected, and refined in place. Normally lives in the
# server data dir (<data>/memories/.tofu_user_profile.md) — the .tofu prefix
# means that if it ever lands inside a project tree it is still recognised as
# agent junk (gitignored, export-stripped) by every consumer. See
# lib/memory/user_profile.py.
USER_PROFILE_FILE = '.tofu_user_profile.md'
# Staged, not-yet-confirmed preference proposals from the consolidation pass
# (propose-then-confirm gate). JSON list; lives next to the profile.
USER_PROFILE_PENDING_FILE = '.tofu_user_profile_pending.json'
# Live cross-conversation presence registry ("who is working in this project
# right now"). Lives UNDER the existing ``.tofu/`` dir (so the ``.tofu*``
# gitignore glob + every artifact consumer already cover it without change):
# ``<root>/.tofu/presence/registry.json``. The authoritative copy is in-memory
# alongside the push hub (single-server contract); this file is the
# crash-recoverable + human-inspectable write-through mirror. Regenerable
# runtime state — safe to delete (a stale entry is reaped on the next server
# startup reconciliation / sweep). Written by ``lib/presence/registry.py``.
PRESENCE_SUBDIR = 'presence'          # under FILE_HISTORY_ROOT_DIR (.tofu/)
PRESENCE_REGISTRY_FILE = 'registry.json'
# Legacy per-conversation git worktree isolation state dir. The isolation
# feature was REMOVED; this constant is retained only so the ``.tofu*`` gitignore
# glob + artifact consumers still recognise any stale ``.tofu_worktrees/`` dirs
# left on disk as agent junk (safe to delete).
WORKTREES_DIR = '.tofu_worktrees'

# Explicit set of the artifacts known TODAY — useful for documentation,
# tests, and consumers that want to enumerate rather than prefix-match.
KNOWN_ARTIFACT_NAMES = (
    FILE_HISTORY_ROOT_DIR,
    TRASH_DIR,
    SANDBOX_DIR,
    ENV_MARKER_FILE,
    USER_PROFILE_FILE,
    USER_PROFILE_PENDING_FILE,
)

# A single ``.gitignore`` pattern that covers every CURRENT and FUTURE
# ``.tofu``-prefixed artifact. gitignore patterns with no embedded slash match
# at any depth, so this also catches artifacts created in sub-directories
# (e.g. a trash bin under a nested run_command cwd). One line, never needs
# editing when a new ``.tofu_*`` artifact is introduced.
GITIGNORE_PATTERN = '.tofu*'


def is_agent_artifact(name: str) -> bool:
    """True if ``name`` (a bare basename) is an agent-written artifact.

    Prefix-based by design: matches ``.tofu`` itself and anything starting
    with ``.tofu`` (``.tofu_trash``, ``.tofu_sandbox``, ``.tofu_env.json``,
    and any future ``.tofu_*``). Pass a basename, not a full path — callers
    walking a tree already have ``entry.name`` / ``os.path.basename``.
    """
    if not name:
        return False
    base = os.path.basename(name.rstrip('/\\')) or name
    return base == ARTIFACT_PREFIX or base.startswith(ARTIFACT_PREFIX)


__all__ = [
    'ARTIFACT_PREFIX',
    'FILE_HISTORY_ROOT_DIR', 'TRASH_DIR', 'SANDBOX_DIR', 'ENV_MARKER_FILE',
    'USER_PROFILE_FILE', 'USER_PROFILE_PENDING_FILE',
    'PRESENCE_SUBDIR', 'PRESENCE_REGISTRY_FILE',
    'WORKTREES_DIR',
    'KNOWN_ARTIFACT_NAMES', 'GITIGNORE_PATTERN',
    'is_agent_artifact',
]
