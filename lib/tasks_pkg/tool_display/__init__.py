"""Tool-call display helpers — build tool-round entries and tool_start events.

Extracted from ``orchestrator.py`` to keep the main run-loop module focused on
orchestration logic.  The public entry-point is :func:`_build_tool_round_entry`;
the per-tool ``_tool_display_*`` helpers are internal to this package.

This ``__init__`` is a pure re-export facade — all implementations live in the
sub-modules (``_renderers``, ``_mcp``, ``_roots``, ``_dispatch``) so that every
``from lib.tasks_pkg.tool_display import X`` keeps working unchanged.
"""

from lib.log import get_logger

logger = get_logger(__name__)


# ── Per-tool renderers + shared helpers (._renderers) ─────────────────
from lib.tasks_pkg.tool_display._renderers import (  # noqa: E402,F401
    _persisted_read_labels,
    _short_url,
    _tool_display_brain,
    _tool_display_browser,
    _tool_display_code_exec,
    _tool_display_compact,
    _tool_display_conv_ref,
    _tool_display_desktop,
    _tool_display_fetch_url,
    _tool_display_generic,
    _tool_display_human_guidance,
    _tool_display_image_gen,
    _tool_display_inspect_image,
    _tool_display_mcp,
    _tool_display_memory,
    _tool_display_project,
    _tool_display_scheduler,
    _tool_display_swarm,
    _tool_display_todo,
    _tool_display_web_search,
)


# ── MCP-specific helpers (._mcp) ──────────────────────────────────────
from lib.tasks_pkg.tool_display._mcp import (  # noqa: E402,F401
    _KM_DOC_RE,
    _MCP_CONTAINER_KEYS,
    _MCP_RESOURCE_KEYS,
    _MCP_SEG_MAX,
    _doc_cid,
    _mcp_arg_suffix,
    _mcp_batch_paths,
    _mcp_links,
    _render_mcp_arg,
    _resolve_doc_title,
    _resolve_project_name,
    _short_doc_id,
    _short_job_id,
    _short_project_id,
    compose_mcp_display,
)


# ── Workspace-root resolution (._roots) ───────────────────────────────
from lib.tasks_pkg.tool_display._roots import (  # noqa: E402,F401
    _FS_TOOLS_FOR_ROOT_PILL,
    _extract_first_path_arg,
    _resolve_tool_root_name,
    _split_rootname_prefix,
)


# ── Dispatch table + public entry points (._dispatch) ─────────────────
from lib.tasks_pkg.tool_display._dispatch import (  # noqa: E402,F401
    _TOOL_DISPLAY_DISPATCH,
    _build_display_dispatch_table,
    _build_tool_round_entry,
    tool_round_label,
)
