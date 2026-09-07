"""Tool-round dispatch: the static tool-name → renderer table plus the two
public entry points ``tool_round_label`` (side-effect-free label) and
``_build_tool_round_entry`` (round-entry + tool_start event payload).

Instead of a massive if/elif chain we use a dispatch dict pattern; each
handler returns ``(display_str, extra_fields_dict)``.
"""

from lib.log import get_logger

logger = get_logger(__name__)

from lib.browser.advanced import ADVANCED_BROWSER_TOOL_NAMES
from lib.desktop_tools import DESKTOP_TOOL_NAMES
from lib.scheduler.tool_defs import SCHEDULER_TOOL_NAMES
from lib.memory.tools import MEMORY_TOOL_NAMES
from lib.skills import SKILL_TOOL_NAMES
from lib.tasks_pkg.executor import SWARM_TOOL_NAMES
from lib.tools.browser import BROWSER_TOOL_NAMES, PAGE_PREVIEW_TOOL_NAMES
from lib.tools.code_exec import CODE_EXEC_TOOL_NAMES
from lib.tools.conversation import (
    CONV_REF_TOOL_NAMES,
    INTEGRATION_TOOL_NAMES,
)
from lib.tools.image_edit import IMAGE_EDIT_TOOL_NAMES
from lib.tools.image_gen import IMAGE_GEN_TOOL_NAMES
from lib.tools.project import PROJECT_TOOL_NAMES

from lib.tools.tool_result_artifacts import TOOL_RESULT_ARTIFACT_NAMES
from lib.tasks_pkg.tool_display._renderers import (
    _tool_display_brain,
    _tool_display_artifact,
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
    _tool_display_execute,
    _tool_display_knowledge,
    _tool_display_local_serve,
    _tool_display_memory,
    _tool_display_mcp,
    _tool_display_motion_video,
    _tool_display_produce,
    _tool_display_project,
    _tool_display_scheduler,
    _tool_display_search_settings,
    _tool_display_server_download,
    _tool_display_skills,
    _tool_display_swarm,
    _tool_display_todo,
    _tool_display_tool_search,
    _tool_display_web_search,
)
from lib.tasks_pkg.tool_display._context import enrich_display_args
from lib.tasks_pkg.tool_display._roots import _resolve_tool_root_name


def _tool_attention_kind(task, tool_name):
    """Return stable semantic importance from the request-owned contract.

    Runtime/error state remains a separate frontend concern.  This field says
    whether a settled call is routine observation, an attended interaction,
    or an operation worth keeping exposed.  Unknown contracts fail visible.
    """
    documents = ((task or {}).get('_toolContractDocumentsByName') or {})
    document = documents.get(tool_name) if isinstance(documents, dict) else None
    permission = (str(document.get('permission') or '')
                  if isinstance(document, dict) else '')
    if permission == 'approval':
        return 'interactive'
    if permission == 'read' or tool_name in TOOL_RESULT_ARTIFACT_NAMES:
        return 'routine'
    return 'important'


# ══════════════════════════════════════════════════════════════════════
#  Module-level dispatch table (hoisted from _build_tool_round_entry)
# ══════════════════════════════════════════════════════════════════════
# This dict is built once at module load time instead of being rebuilt on
# every call.  The only runtime-dynamic part is CODE_EXEC_TOOL_NAMES
# which depends on the ``project_enabled`` flag — that is handled inside
# _build_tool_round_entry with a cheap conditional override.

def _build_display_dispatch_table():
    """Build the static tool-name → handler dispatch table.

    Called once at module load time.  Returns the dict.
    """
    table = {}

    # Direct name matches
    table['web_search'] = _tool_display_web_search
    table['search_knowledge'] = _tool_display_knowledge
    table['search_tools'] = _tool_display_tool_search
    # Hidden from the wire schema but accepted as a robust compatibility call.
    table['execute_tools'] = _tool_display_execute
    table['fetch_url'] = _tool_display_fetch_url
    table['browser_download_url_to_server'] = _tool_display_server_download
    # Persisted rounds created before the browser-prefix migration still render.
    table['download_url_to_server'] = _tool_display_server_download
    table['context_compact'] = _tool_display_compact

    # Progressive MCP bridge tools are built-ins too. Route them through the
    # MCP renderer so discovery shows its query and read/write calls show the
    # underlying namespaced resource instead of a generic bare function name.
    from lib.mcp.progressive import MCP_PROGRESSIVE_TOOL_NAMES
    for name in MCP_PROGRESSIVE_TOOL_NAMES:
        table[name] = _tool_display_mcp

    # Code exec tools — default to project handler (overridden at call
    # time when project is disabled).
    for name in CODE_EXEC_TOOL_NAMES:
        table.setdefault(name, _tool_display_project)

    # Project tools
    for name in PROJECT_TOOL_NAMES:
        table.setdefault(name, _tool_display_project)

    # read_files — global tool (not in PROJECT_TOOL_NAMES), uses same
    #   project-style display rendering (path + line ranges; icon is the
    #   frontend SVG, no emoji prefix).
    table.setdefault('read_files', _tool_display_project)

    # Browser tools (basic + advanced).
    for name in BROWSER_TOOL_NAMES:
        table[name] = _tool_display_browser
    for name in ADVANCED_BROWSER_TOOL_NAMES:
        table[name] = _tool_display_browser
    # Server-side page preview — not in BROWSER_TOOL_NAMES (not an extension
    # tool) but shares the browser-family display.
    for name in PAGE_PREVIEW_TOOL_NAMES:
        table[name] = _tool_display_browser

    # Memory tools
    for name in MEMORY_TOOL_NAMES:
        table[name] = _tool_display_memory

    # Skill tools (load_skill — read-only progressive disclosure)
    for name in SKILL_TOOL_NAMES:
        table[name] = _tool_display_skills
    # Display-only migration shim: old persisted conversations may contain
    # activate_skill calls. It is intentionally absent from tool schemas,
    # execution handlers, and authority catalogs, so it cannot be called.
    table['activate_skill'] = _tool_display_skills

    # Conversation reference tools
    for name in CONV_REF_TOOL_NAMES:
        table[name] = _tool_display_conv_ref

    # Retained isolated-workspace execution controls.
    for name in INTEGRATION_TOOL_NAMES:
        table[name] = _tool_display_brain

    for name in TOOL_RESULT_ARTIFACT_NAMES:
        table[name] = _tool_display_artifact

    # Scheduler tools
    for name in SCHEDULER_TOOL_NAMES:
        table[name] = _tool_display_scheduler

    # Managed local-deployment tools (prepare/deploy/status/list/stop/remove)
    from lib.local_serve.tool_defs import LOCAL_SERVE_TOOL_NAMES
    for name in LOCAL_SERVE_TOOL_NAMES:
        table[name] = _tool_display_local_serve

    # Desktop tools
    for name in DESKTOP_TOOL_NAMES:
        table[name] = _tool_display_desktop

    # Swarm tools
    for name in SWARM_TOOL_NAMES:
        table[name] = _tool_display_swarm

    # Image generation tools
    for name in IMAGE_GEN_TOOL_NAMES:
        table[name] = _tool_display_image_gen

    # Image inspection tool (zoom/rotate/crop viewer)
    for name in IMAGE_EDIT_TOOL_NAMES:
        table[name] = _tool_display_inspect_image

    # Motion-video pipeline tools (env/storyboard/static gates/render/probe/
    # concat/narrate/mux) — friendly labels naming the scene / files, no
    # spurious "unregistered tool" WARNING on every call.
    from lib.tools.motion_video import MOTION_VIDEO_TOOL_NAMES
    for name in MOTION_VIDEO_TOOL_NAMES:
        table[name] = _tool_display_motion_video

    # High-level produce_* tools (topic → video / report / research ideas).
    from lib.tools.produce import PRODUCE_TOOL_NAMES
    for name in PRODUCE_TOOL_NAMES:
        table[name] = _tool_display_produce

    # Search/fetch pipeline settings knob (read with no args, tune with kwargs)
    table['update_search_settings'] = _tool_display_search_settings

    # Human guidance tool
    table['ask_human'] = _tool_display_human_guidance

    # Structured task-checklist tool (todo_write) — friendly progress label,
    # no spurious "unregistered tool" WARNING on every checklist update.
    table['todo_write'] = _tool_display_todo

    return table


# Hoisted constant — built once at import time.
_TOOL_DISPLAY_DISPATCH = _build_display_dispatch_table()


def tool_round_label(fn_name, fn_args):
    """Return the human-readable tool-round label chat would render for a call.

    Public, side-effect-free entry point over the same ``_tool_display_*``
    dispatch table the chat orchestrator uses, so secondary agent surfaces
    (paper report / Q&A) get IDENTICAL, string/dict-safe labels — including
    the multi-line batch rendering (``N searches:\\n• …``) and the empty-list
    guards — instead of reimplementing them. Prefers the richer
    ``_display_query`` (multi-line) over the compact form when the handler
    supplies one.

    Args:
        fn_name: Tool name.
        fn_args: The DECODED + repaired arguments dict (run it through
            ``lib.tool_input_repair.parse_and_repair_tool_args`` first).

    Returns:
        The display string. Falls back to the tool name on any handler error.
    """
    handler = _TOOL_DISPLAY_DISPATCH.get(fn_name, _tool_display_generic)
    try:
        display_query, extra = handler(fn_name, fn_args, '', '')
    except Exception as e:
        logger.warning('[ToolDisplay] tool_round_label handler for %s raised: %s', fn_name, e)
        return fn_name
    return extra.get('_display_query', display_query)


def _build_tool_round_entry(fn_name, fn_args, tc_id, tc_args_str, tool_round_num,
                             project_enabled, conv_id=None, task=None):
    """Build a tool-round entry and tool_start event payload for a tool call.

    Uses a module-level dispatch table (``_TOOL_DISPLAY_DISPATCH``) instead of
    rebuilding a dict on every call.  The only runtime override is for
    CODE_EXEC_TOOL_NAMES when ``project_enabled`` is False — those get
    redirected to ``_tool_display_code_exec``.

    When ``conv_id`` is supplied and the tool is a filesystem tool in a
    multi-root workspace, attaches ``_toolRoot`` to both the round entry
    and the SSE event so the frontend can render a ``rootname:`` pill.

    Returns (new_tool_round_num, round_entry, event_payload).
    """
    # ── Runtime override: code-exec tools display differently when project
    #    mode is off (standalone code execution vs. project tool).
    if not project_enabled and fn_name in CODE_EXEC_TOOL_NAMES:
        handler = _tool_display_code_exec
    else:
        handler = _TOOL_DISPLAY_DISPATCH.get(fn_name, _tool_display_generic)

    # Readability enrichment: resolve machine handles (conversation ids,
    # tab ids) into human titles on a throwaway args COPY for the renderer.
    # The original fn_args continues to execution/persistence untouched.
    display_args = fn_args
    if task is not None:
        try:
            display_args = enrich_display_args(
                fn_name, fn_args, conv_id=conv_id, task=task)
        except Exception as e:
            logger.debug('[ToolDisplay] arg enrichment failed for %s: %s',
                         fn_name, e)

    try:
        display_query, extra = handler(fn_name, display_args, tc_id, tc_args_str)
    except Exception as e:
        logger.warning('[ToolDisplay] handler for %s raised: %s', fn_name, e)
        display_query = fn_name
        extra = {'toolName': fn_name}


    # Continuation tools (read/search_tool_artifact) label their target as
    # "tool-result:<hash>" — a content digest that answers none of the
    # user's questions. When the spill site registered the artifact's origin
    # on this task, relabel the row with the SOURCE round + tool display
    # BEFORE the round is ever announced, so the live row, the persisted
    # round, and every replay are all born with the readable label.
    if task is not None and fn_name in TOOL_RESULT_ARTIFACT_NAMES:
        try:
            from lib.tool_result_artifacts import (
                artifact_provenance, continuation_display_label,
                continuation_origin_meta)
            _origin = artifact_provenance(
                task, str((fn_args or {}).get('artifact_ref') or ''))
            if not _origin:
                # Batch form carries no top-level ref — each searches[]/
                # reads[] item has its own. The common batch searches ONE
                # spill with several patterns, so the first resolvable item
                # origin labels the row; a mixed-spill batch keeps the first
                # match (per-item chips would not fit the one-line row).
                _items = ((fn_args or {}).get('searches')
                          or (fn_args or {}).get('reads'))
                if isinstance(_items, list):
                    for _item in _items:
                        if not isinstance(_item, dict):
                            continue
                        _origin = artifact_provenance(
                            task, str(_item.get('artifact_ref') or ''))
                        if _origin:
                            break
            if _origin:
                _origin_args = fn_args if isinstance(fn_args, dict) else {}
                display_query = continuation_display_label(
                    fn_name, _origin_args, _origin)
                # Structured twin of the flat label, riding the round entry +
                # tool_start event exactly like ``_toolRoot``: the frontend
                # renders an origin chip (``R54 compacted``) before the source
                # label instead of stacking two "Read" verbs. Recovery-rebuilt
                # rounds lose this key and fall back to parsing the flat label.
                _origin_meta = continuation_origin_meta(
                    fn_name, _origin_args, _origin)
                if _origin_meta:
                    extra['_artifactOrigin'] = {
                        k: v for k, v in _origin_meta.items()
                        if v is not None and v != ''}
                    if _origin_meta.get('sourceToolCallId'):
                        extra['parentToolCallId'] = str(
                            _origin_meta['sourceToolCallId'])
        except Exception as _origin_err:
            logger.debug('[ToolDisplay] artifact origin relabel failed for '
                         '%s: %s', fn_name, _origin_err)
    tool_round_num += 1
    rn = tool_round_num

    # Start clock (). Stamped HERE — the instant the round is
    #   announced — so a still-running tool can render a truthful "running for
    #   38s" from a SERVER clock. A client-side stopwatch cannot: it re-mints on
    #   every paint and on every reconnect, washing a long call into looking
    #   fresh (the bug `_pmAdoptServerClocks` exists to prevent in the media
    #   tabs). Carried onto every later frame for this round by
    #   `_finalize_tool_round`, so the row is self-describing even on a cold
    #   replay that never saw this tool_start.
    from lib.agent_core.events import now_ms
    _t_start = now_ms()
    # Build round_entry
    round_entry = {
        'roundNum': rn,
        'query': display_query,
        'results': None,
        'status': 'searching',
        'toolCallId': tc_id,
        'toolArgs': tc_args_str,
        'tStart': _t_start,
        'attentionKind': _tool_attention_kind(task, fn_name),
    }
    round_entry.update(extra)

    # Build tool_start event — same fields + type.
    # Routed through build_event so `emittedAt` is stamped at the ONE chokepoint
    # (lib/agent_core/events.build_event). A second stamper here would drift
    # from the one every other tool frame uses and make the transport segment
    # incomparable — the whole point of having a single constructor.
    from lib.agent_core.events import EventType, build_event
    event = build_event(
        EventType.TOOL_START,
        roundNum=rn,
        query=extra.get('_display_query', display_query),
        toolCallId=tc_id,
        toolArgs=tc_args_str,
        tStart=_t_start,
        attentionKind=round_entry['attentionKind'],
    )
    # Copy relevant extra fields into event (toolName, _swarm, etc.)
    for k, v in extra.items():
        if not k.startswith('_display_'):
            event[k] = v

    # ── Multi-root workspace pill: attach the workspace-root name the
    #    tool call resolves to, so the frontend can render a
    #    ``rootname:`` prefix on the tool-call line. Only meaningful for
    #    filesystem tools, and only when more than one root is registered.
    try:
        root_name = _resolve_tool_root_name(fn_name, fn_args, conv_id=conv_id)
    except Exception as e:
        logger.debug('[ToolDisplay] _resolve_tool_root_name failed for %s: %s',
                     fn_name, e)
        root_name = ''
    if root_name:
        round_entry['_toolRoot'] = root_name
        event['_toolRoot'] = root_name

    return tool_round_num, round_entry, event
