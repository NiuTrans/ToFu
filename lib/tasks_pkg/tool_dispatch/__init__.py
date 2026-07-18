# HOT_PATH
"""Tool dispatch — parsing, labelling, approval-gating, and parallel execution.

Extracted from the inner loop of ``orchestrator.run_task`` to isolate the
tool-execution pipeline.  The two public entry-points are:

- :func:`parse_tool_calls` — parse raw ``tool_calls`` from the assistant
  message into a structured list with JSON repair.
- :func:`execute_tool_pipeline` — run the full approval → parallel-dispatch
  → result-append pipeline.

This package is a FACADE: every public symbol that historically lived in
``lib/tasks_pkg/tool_dispatch.py`` is re-exported here so that
``from lib.tasks_pkg.tool_dispatch import X`` keeps working byte-identically.
Implementations live in the sub-modules listed below; the import path is
unchanged.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


# ── Names re-exported for legacy monkeypatch compatibility ────────────
# Historically these lived as module-level names in the single-file
# ``tool_dispatch.py``; tests patch ``tool_dispatch.append_event`` etc. on the
# package. The sub-modules resolve these through the facade at call time (see
# ``_heartbeat._emit_tool_heartbeat`` / ``_flags._task_partitions``), so a
# patch here is honoured byte-identically to the pre-split behaviour.
from lib.agent_core.events import EventType, build_event  # noqa: E402,F401
from lib.tasks_pkg.manager import append_event  # noqa: E402,F401


# ── Harness self-repair UI surfacing ─────────────────────────────────
from lib.tasks_pkg.tool_dispatch._repair import (  # noqa: E402,F401
    _REPAIR_PATTERN_LABELS,
    _apply_repair_to_round,
    _build_repair_summary,
)


# ── Tool partitions + dedup cache ─────────────────────────────────────
from lib.tasks_pkg.tool_dispatch._flags import (  # noqa: E402,F401
    _IDEMPOTENT_TOOLS,
    _IDEMPOTENT_TOOLS_BASE,
    _PROJECT_CACHEABLE_TOOLS,
    _WRITE_TOOLS,
    _WRITE_TOOLS_BASE,
    _build_cache_hit_meta,
    _invalidate_project_cache,
    _make_cache_key,
    _registry_tool_flags,
    _safe_count_tokens,
    _task_partitions,
    _unpack_cache_entry,
)


# ── Tool-exec phase labels + known-tool-name resolution ───────────────
from lib.tasks_pkg.tool_dispatch._labels import (  # noqa: E402,F401
    _TOOL_EXEC_LABELS,
    _known_tool_names,
    emit_tool_exec_phase,
    tool_label,
)


# ── Tool-call parsing ─────────────────────────────────────────────────
from lib.tasks_pkg.tool_dispatch._parse import parse_tool_calls  # noqa: E402,F401


# ── Long-tool heartbeat + serial dispatch config + pooled exec ────────
from lib.tasks_pkg.tool_dispatch._heartbeat import (  # noqa: E402,F401
    _SERIAL_BLOCKING_TOOLS,
    _emit_tool_heartbeat,
    _execute_tool_one_pooled,
    _start_tool_heartbeat,
)


# ── Write-approval gating ─────────────────────────────────────────────
from lib.tasks_pkg.tool_dispatch._approval import (  # noqa: E402,F401
    _APPROVAL_META_ENRICHERS,
    _approval_meta_apply_diff,
    _approval_meta_apply_diffs,
    _approval_meta_create_project,
    _approval_meta_insert_content,
    _approval_meta_insert_contents,
    _approval_meta_run_command,
    _approval_meta_write_file,
    _handle_approval,
)


# ── Execution pipeline ────────────────────────────────────────────────
from lib.tasks_pkg.tool_dispatch._pipeline import (  # noqa: E402,F401
    _append_screenshot_message,
    execute_tool_pipeline,
)
