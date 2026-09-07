# HOT_PATH
"""Tool-execution phase labels + known-tool-name resolution.

Human-readable labels for tool-exec phase events, the ``tool_label`` helper
(with MCP fallback), the per-turn known-tool-name set, and the
``emit_tool_exec_phase`` event emitter.
"""

from __future__ import annotations

from typing import Any

from lib.agent_core.events import Phase, build_phase
from lib.log import get_logger
from lib.protocols import TaskEventSink
from lib.tasks_pkg.executor import tool_registry
from lib.tasks_pkg.manager import append_event

logger = get_logger(__name__)


# ── Human-readable labels for tool-execution phase events ──────────────
# NO emoji (owner directive 2026-08-03): the frontend phase row renders its
# own SVG iconography, so an emoji in the label is a second, inconsistent
# icon source. Labels are the ENGLISH fallback for headless / non-i18n
# clients — i18n clients compose the localized label from the structured
# tool names shipped alongside (phase.tools / phase.toolContextTools).
# Name the act the tool actually performs: apply_diff PATCHES files.
_TOOL_EXEC_LABELS = {
    'web_search':   'Searching the web',
    'search_knowledge': 'Searching local knowledge',
    'fetch_url':    'Fetching pages',
    'read_files':   'Reading files',
    'list_dir':     'Listing directory',
    'grep_search':  'Searching code',
    'find_files':   'Finding files',
    'write_file':   'Writing files',
    'edit_file':    'Editing files',
    'apply_diff':   'Patching files',
    'apply_diffs':  'Patching files',
    'insert_content':'Inserting content',
    'insert_contents':'Inserting content',
    'code_exec':    'Running code',
    'bash_exec':    'Running command',
    'create_memory': 'Saving memory',
    'search_skills': 'Searching skills',
    'load_skill': 'Loading skill',
    'read_skill_resource': 'Reading skill resource',
    'request_skill_install': 'Installing skill',
    'ask_human': 'Asking for your input',
}


def tool_label(tn: str) -> str:
    """Get a human-readable label for a tool name, with MCP fallback.

    Used by both ``emit_tool_exec_phase`` and ``orchestrator._emit_tool_round_phase``
    to produce consistent labels.
    """
    label = _TOOL_EXEC_LABELS.get(tn)
    if label:
        return label
    from lib.mcp.types import MCP_TOOL_PREFIX, parse_namespaced_name
    if tn.startswith(MCP_TOOL_PREFIX):
        parsed = parse_namespaced_name(tn)
        if parsed:
            return f'{parsed[0]}/{parsed[1]}'
    return tn


def _known_tool_names(task: dict[str, Any]) -> set[str]:
    """Build the set of tool names that are REAL for this task's turn.

    The source of truth is ``task['_executable_tool_catalog']`` — every tool
    genuinely available to this task after environment, tenant and connection
    policy. ``_tool_schema`` is only a visibility projection and may omit
    searchable tools, so it is never an authorization boundary.

    Falls back to the global registry's exact names ∪ custom-env names when
    no schema was attached (e.g. a unit test that calls parse_tool_calls
    directly), so classification degrades safely instead of flagging
    everything.
    """
    names: set[str] = set()
    has_authority_catalog = '_executable_tool_catalog' in task
    if has_authority_catalog:
        # An explicit empty catalog means zero authority.  It must not fall
        # back to a stale conversation-latched visibility schema after the
        # user disables every tool.
        catalog = task.get('_executable_tool_catalog') or []
    else:
        catalog = task.get('_tool_schema') or []
    for t in catalog:
        if isinstance(t, dict):
            fn = t.get('function') or {}
            n = fn.get('name')
            if n:
                names.add(n)
    # Per-request custom tools (their schemas are normally already in
    # _tool_schema, but union them in directly so classification is correct
    # even if the snapshot predates a late-attached env).
    env = task.get('_tool_env')
    if env is not None:
        try:
            for s in (env.schemas or []):
                if isinstance(s, dict):
                    n = (s.get('function') or {}).get('name')
                    if n:
                        names.add(n)
        except Exception as e:
            logger.debug('[tool_dispatch] tool_env name union skipped: %s', e)
    # Gateway names are server-owned capabilities.  They are admitted only
    # when assembly explicitly enabled them for this task; model-provided
    # descriptions or schemas cannot add to this set.
    names |= set(task.get('_tool_gateway_names') or ())
    if names or has_authority_catalog:
        return names
    # ── Fallback: no schema snapshot — use the global registry ──
    try:
        names |= set(tool_registry._exact.keys())
        for name_set, _ in tool_registry._sets:
            names |= set(name_set)
    except Exception as e:
        logger.debug('[tool_dispatch] registry name harvest skipped: %s', e)
    # The registry also contains the ``execute_tools`` handler. Local Tool
    # Search declares it on the wire; schema-less recovery still admits it.
    # Nested calls are independently authorized against the task catalog.
    return names


def emit_tool_exec_phase(
    task: dict,
    parsed_tcs: list,
    *,
    event_sink: TaskEventSink | None = None,
) -> None:
    """Emit a ``phase`` event indicating which tools are about to execute.

    Builds a human-readable summary using :data:`_TOOL_EXEC_LABELS` and
    sends it as a ``tool_exec`` phase event.

    Parameters
    ----------
    task : dict
        Live task dict — event is appended.
    parsed_tcs : list[tuple]
        The parsed tool-call tuples from :func:`parse_tool_calls`.
    event_sink : TaskEventSink, optional
        Optional :class:`~lib.protocols.TaskEventSink` for dependency injection.
        When provided, ``event_sink.append_event()`` is used instead of the
        concrete ``lib.tasks_pkg.manager.append_event`` import.  Pass a mock
        for testing.  ``None`` (default) falls back to the concrete import.
    """
    tool_names_list = [item[1] for item in parsed_tcs]
    unique_tool_names = list(dict.fromkeys(tool_names_list))
    n = len(parsed_tcs)

    if n == 1:
        detail = tool_label(unique_tool_names[0])
    else:
        labeled = [tool_label(tn) for tn in unique_tool_names]
        detail = f'Executing {n} tools: {", ".join(labeled)}'

    _append = event_sink.append_event if event_sink is not None else append_event
    _append(task, build_phase(
        Phase.TOOL_EXEC,
        detail=detail,
        tools=tool_names_list,
    ))
