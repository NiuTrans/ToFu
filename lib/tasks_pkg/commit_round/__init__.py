"""Per-round file-history commit + modified-file derivation.

Extracted from ``lib/tasks_pkg/orchestrator.py`` (2026-06-24) — a
self-contained, daemon-thread-decoupled file-history concern with no
coupling to the orchestration loop beyond the task dict + event helpers.

  - ``derive_round_modified_files`` — build a round's authoritative file-change
    list from the per-root modifications journal (conversation-isolated via
    taskId stamping). Called by ``_finalize_and_emit_done``.
  - ``_spawn_async_commit_round`` / ``_run_commit_round_async`` — run
    ``file_history.make_snapshot`` in a daemon thread so the snapshot persist
    can't block ``persist_task_result`` → ``_dispatch_queued_message``; emit a
    ``round_committed`` SSE event + enrich ``modifiedFileList`` with
    opaque-writer (code_exec / MCP) side-effects the journal misses.
  - ``_patch_assistant_message_with_git`` — persist the snapshotId onto the
    conversation's assistant message after the SSE reader may have closed.
  - ``_spawn_async_profile_consolidation`` / ``_run_profile_consolidation_async``
    / ``_patch_assistant_message_with_prefs`` — the layer-3 memory-profile
    preference-consolidation daemon (same decoupling pattern).

This module is a **facade-preserving package**: the ~600-line monolith was
split into cohesive sub-modules (``_derive``, ``_commit``, ``_profile``) but
the import path ``lib.tasks_pkg.commit_round`` is UNCHANGED and every symbol a
consumer/test imports is re-exported here verbatim.  ``orchestrator`` imports
``derive_round_modified_files`` + ``_spawn_async_commit_round`` +
``_spawn_async_profile_consolidation`` + ``_run_commit_round_async`` back, so
its internal call sites are unchanged.

CRITICAL: the actual ``make_snapshot`` lives in ``lib.file_history`` — this
package only DRIVES it from a daemon thread; it never redefines it.

``append_event`` is re-exported at facade scope (the monolith exposed it at
module top level) so that ``monkeypatch.setattr(commit_round, 'append_event',
...)`` steers the consolidation daemon body, which resolves it through this
facade at call time.  ``EventType`` / ``build_event`` are likewise re-exported.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)

# ── Top-level import surface the monolith exposed as ``commit_round.X`` ──
from lib.agent_core.events import EventType, build_event  # noqa: F401
from lib.tasks_pkg.manager import append_event  # noqa: F401

# ── Sub-module symbols (re-exported verbatim — import path preserved) ──
from lib.tasks_pkg.commit_round._derive import (  # noqa: F401
    derive_round_modified_files,
)
from lib.tasks_pkg.commit_round._commit import (  # noqa: F401
    _spawn_async_commit_round,
    _run_commit_round_async,
    _patch_assistant_message_with_git,
)
from lib.tasks_pkg.commit_round._profile import (  # noqa: F401
    _spawn_async_profile_consolidation,
    _run_profile_consolidation_async,
    _patch_assistant_message_with_prefs,
)

__all__ = [
    'derive_round_modified_files',
    '_spawn_async_commit_round',
    '_run_commit_round_async',
    '_patch_assistant_message_with_git',
    '_spawn_async_profile_consolidation',
    '_run_profile_consolidation_async',
    '_patch_assistant_message_with_prefs',
    'append_event',
    'EventType',
    'build_event',
]
