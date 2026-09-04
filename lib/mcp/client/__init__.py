# HOT_PATH
"""MCP client hub — facade-preserving package.

Split from the former single-file ``lib/mcp/client.py`` (~2400 lines) into
cohesive submodules. The public import path ``lib.mcp.client`` is UNCHANGED:
this ``__init__`` re-exports (re-binds) every symbol that lived in the old
module so ``from lib.mcp.client import X`` and ``from lib.mcp import get_bridge``
keep working byte-identically.

FACADE CONTRACT (load-bearing — see ``_state._pkg``): every process-global
(the ``get_bridge`` singleton ``_bridge_singleton`` — deliberately NOT named
``_bridge`` because the ``_bridge`` submodule would clobber it on this facade —
the install-job registry
``_install_jobs``, the vendored hot-reload baseline, the snapshot bookkeeping)
lives EXACTLY ONCE in ``_state``; submodules resolve monkeypatchable names
THROUGH this facade module at call time (``sys.modules['lib.mcp.client']``),
so a test's ``monkeypatch.setattr(lib.mcp.client, name, ...)`` is honoured.
``importlib`` / ``subprocess`` are re-exported here because tests patch them
as ``mc.importlib`` / ``mc.subprocess``.
"""

from __future__ import annotations

# Re-exported for test monkeypatching on the facade (mc.importlib / mc.subprocess).
import importlib  # noqa: F401
import subprocess  # noqa: F401

from lib.log import get_logger

logger = get_logger(__name__)

# ── Errors + exception classifiers ───────────────────────────────────────────
from lib.mcp.client._errors import (  # noqa: E402,F401
    _MCP_STDERR_TAIL_BYTES,
    MCPConnectError,
    _unwrap_exception_group,
    _is_transport_dead_error,
    _is_call_timeout_error,
    _read_stderr_tail,
)

# ── Argument schema coercion ─────────────────────────────────────────────────
from lib.mcp.client._coerce import (  # noqa: E402,F401
    _coerce_one,
    _coerce_args_to_schema,
    _extract_annotations,
    _extract_output_schema,
    _extract_read_only_hint,
)

# ── Process-wide singleton state + get_bridge() ──────────────────────────────
from lib.mcp.client._state import (  # noqa: E402,F401
    _pkg,
    get_bridge,
    _vendored_mod,
    _VENDORED_LAUNCHERS,
    _repo_root,
    _install_lock,
    _install_last_error,
    _vendored_mtime,
    _vendored_reload_lock,
    _snapshot_reported,
    _snapshot_lock,
    _install_jobs,
    _install_jobs_lock,
    _bridge_singleton,
    _bridge_lock,
)

# ── Vendored-launcher discovery + prewarm ────────────────────────────────────
from lib.mcp.client._vendor import (  # noqa: E402,F401
    _ensure_writable_caches,
    _NPX_CUTOFF_MARKER,
    _reconcile_npx_cache,
    _npx_rebuild_pending,
    reconcile_for_connect,
    _vendored_path,
    _reload_vendored_if_changed,
    _VENDOR_EXCLUDE_DIRS,
    _vendor_excluded_dir,
    _vendor_excluded_file,
    _vendor_tree_signature,
    _snapshot_stale_reason,
    _auto_vendor_enabled,
    _run_vendor_script,
    _check_snapshot_staleness,
    _find_vendored_source,
    is_vendored_launcher,
    prewarm_vendored_launcher,
    prewarm_all_vendored,
    vendored_launch_argv,
)

# ── Launcher resolution + first-connect auto-install ─────────────────────────
from lib.mcp.client._install import (  # noqa: E402,F401
    _LAUNCHER_HINTS,
    get_install_job,
    start_install_job,
    _resolve_launcher,
    _prepend_interpreter_bin_to_path,
    _launcher_install_hint,
)

# ── The bridge (server handles + MCPBridge orchestrator) ─────────────────────
from lib.mcp.client._bridge import (  # noqa: E402,F401
    _MCPServerHandle,
    MCPBridge,
)

__all__ = [
    'MCPConnectError',
    'MCPBridge',
    '_MCPServerHandle',
    'get_bridge',
    'is_vendored_launcher',
    'prewarm_vendored_launcher',
    'prewarm_all_vendored',
    'get_install_job',
    'start_install_job',
    '_resolve_launcher',
    '_launcher_install_hint',
    'vendored_launch_argv',
    '_find_vendored_source',
    '_reload_vendored_if_changed',
    '_check_snapshot_staleness',
    'reconcile_for_connect',
    '_reconcile_npx_cache',
    '_coerce_args_to_schema',
    '_extract_annotations',
    '_extract_output_schema',
    '_extract_read_only_hint',
    '_install_jobs',
    '_bridge_singleton',
]
