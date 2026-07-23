"""lib/mcp/client/_state.py — process-wide singleton state for the MCP client.

This module is the SINGLE source of truth for every process-global the MCP
client hub owns: the ``get_bridge()`` singleton, the vendored-registry
hot-reload baseline, the auto-install job registry, and the snapshot
staleness bookkeeping. There must be exactly ONE of each of these per
process — a divergent ``_bridge`` would spawn a second MCP bridge; a
divergent ``_install_jobs`` would lose install progress.

Facade contract (IMPORTANT):
    The public import path ``lib.mcp.client`` is a PACKAGE whose ``__init__``
    re-exports (re-binds) every name defined across the submodules. Tests
    monkeypatch names ON THE FACADE (``lib.mcp.client``), e.g.
    ``monkeypatch.setattr(mc, '_install_attempted', set())`` or
    ``monkeypatch.setattr(mc, '_resolve_launcher', fn)``. For those patches
    to be observed, every reader/writer of a patchable name must resolve it
    THROUGH the facade module at call time — never via a stale local
    reference captured at import. ``_pkg()`` returns that facade module.
"""

from __future__ import annotations

import os
import sys
import threading

from lib.log import get_logger

logger = get_logger(__name__)


def _pkg():
    """Return the package facade module (``lib.mcp.client``).

    Every access to a monkeypatchable module-level name (state dict, lock,
    or function) MUST go through this so a test's
    ``monkeypatch.setattr(lib.mcp.client, name, ...)`` is honoured — the
    facade's ``__init__`` re-binds each name, so the facade attribute is the
    authoritative live value, not the submodule's own binding.
    """
    return sys.modules['lib.mcp.client']


# ── Vendored registry (shared, mutated in place) ─────────
#
# The registry + repo_root live in the tiny, dependency-free
# ``lib/mcp/vendored`` module so the release-time vendoring script can read
# the same source of truth WITHOUT importing this heavy package. The
# underscore aliases below preserve the historical internal names used
# throughout this package (and monkeypatched by tests).
from lib.mcp import vendored as _vendored_mod  # noqa: E402
from lib.mcp.vendored import VENDORED_LAUNCHERS as _VENDORED_LAUNCHERS  # noqa: E402
from lib.mcp.vendored import repo_root as _repo_root  # noqa: E402


# ── First-connect auto-install guard/registry ────────────
#
# Guard so a failing install is attempted at most once per process per
# command — otherwise every reconnect sweep would re-run pip. Protected by
# ``_install_lock``.
_install_attempted: set[str] = set()
_install_lock = threading.Lock()
# Per-command serialization so two concurrent callers (e.g. the startup
# pre-warm racing a user "Install" click) never run ``pip install`` of the
# SAME source at once — the second waits, then sees ``_install_attempted`` and
# just re-resolves. The registry dict itself is guarded by ``_install_lock``.
_install_cmd_locks: dict[str, threading.Lock] = {}
# Last auto-install failure reason per command, surfaced in the connect error
# so the user sees WHY zero-touch install failed (pip stderr / no source /
# timeout) instead of a generic "not on PATH" dead-end. Set under _install_lock.
_install_last_error: dict[str, str] = {}


# ── Hot-reload of the vendored registry ──────────────────
#
# ``vendored.py`` is imported once at startup, so a NEW row added to
# ``VENDORED_LAUNCHERS`` while Tofu is already running is invisible until a
# restart. ``_reload_vendored_if_changed`` re-reads the file when its mtime
# changes and MERGES new/changed rows into the LIVE dict (identity preserved).
_vendored_mtime: float = 0.0
_vendored_reload_lock = threading.Lock()

# Baseline the mtime at import time so an edit made BETWEEN process start and
# the first connect is still detected (mtime then strictly advances).
try:
    _vp = getattr(_vendored_mod, '__file__', '') or ''
    _vendored_mtime = os.path.getmtime(_vp) if _vp else 0.0
except OSError as _e:
    logger.debug('[MCP] vendored.py baseline stat failed (%s) — defaulting mtime=0', _e)
    _vendored_mtime = 0.0


# ── Vendored-snapshot staleness bookkeeping ──────────────
#
# Last drift state we LOGGED per command ('' = fresh), so we warn once per
# distinct drift instead of on every reconnect. Protected by _snapshot_lock.
_snapshot_reported: dict[str, str] = {}
_snapshot_lock = threading.Lock()


# ── Async install jobs (route returns immediately; UI polls) ──────────
#
# State is a tiny in-process dict (single-process server). Each entry::
#   {state: 'installing'|'ready'|'error', detail: str, started: float, ended: float}
_install_jobs: dict[str, dict] = {}
_install_jobs_lock = threading.Lock()


# ── get_bridge() singleton ───────────────────────────────
# NOTE: this global MUST NOT be named ``_bridge`` — the package also has a
# submodule ``lib/mcp/client/_bridge.py``, and Python binds every imported
# submodule as an attribute of its parent package. A global named ``_bridge``
# is therefore silently CLOBBERED by the ``_bridge`` submodule object on the
# facade, so ``get_bridge()`` would return the MODULE (truthy, not None) and
# ``bridge.list_servers()`` raises ``module ... has no attribute 'list_servers'``.
# The singleton lives under a distinct name to keep it disjoint from any
# submodule name.
_bridge_singleton = None  # type: ignore[assignment]  # MCPBridge | None
_bridge_lock = threading.Lock()


def get_bridge():
    """Get the global MCPBridge singleton (lazy-initialized)."""
    pkg = _pkg()
    if pkg._bridge_singleton is not None:
        return pkg._bridge_singleton
    with pkg._bridge_lock:
        if pkg._bridge_singleton is None:
            # Import lazily to avoid an import cycle at package-init time and
            # to resolve the class through the facade (so it's the same object
            # regardless of import ordering).
            pkg._bridge_singleton = pkg.MCPBridge()
    return pkg._bridge_singleton
