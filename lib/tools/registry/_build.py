"""lib/tools/registry/_build.py — Built-in tool-spec builders + registration.

Each ``_build_*`` reproduces exactly one legacy ``if feature: …`` branch
(including its logging + lazy imports), and :func:`_register_builtins` wires
them into the registry in the canonical, prompt-cache-stable order:

    search → fetch → read_files → inspect_image → project|code_exec →
    browser → desktop → image_gen → conv_ref → human_guidance →
    ⟨base/capability boundary⟩ → memory → skills → todo → scheduler →
    swarm → mcp → custom (always last)

:func:`_register_builtins` is invoked once at package import (from
``lib/tools/registry/__init__.py``) so ``_TOOL_SPECS`` is populated as a
side-effect of importing ``lib.tools.registry`` — the behaviour the monolith
had. Heavy schema imports stay inside the builders (called at request time).
"""

from __future__ import annotations

from collections.abc import Mapping

from lib.log import get_logger

from lib.tools.registry._spec import (
    ToolContext,
    ToolSpec,
    register_tool_spec,
)

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════
#  Built-in spec builders — each reproduces one legacy branch
#  exactly (including its logging).  Heavy imports stay lazy.
# ══════════════════════════════════════════════════════════

def _build_search(ctx: ToolContext) -> list[dict]:
    # 'single' is a retired mode kept as a legacy alias for old conversations
    # — it now behaves like 'multi' (the one-shot SEARCH_TOOL_SINGLE schema
    # was removed). The composer exposure gate decides whether its schema is
    # visible; the builder describes runtime availability only.
    from lib.tools.search import build_search_tool
    return [build_search_tool()]


def _build_retained_agents(ctx: ToolContext) -> list[dict]:
    if not ctx.durable_state_available:
        return []
    from lib.tools.retained_agents import LIST_AGENTS_TOOL, MANAGE_AGENTS_TOOL
    return [LIST_AGENTS_TOOL, MANAGE_AGENTS_TOOL]


def _build_search_settings(ctx: ToolContext) -> list[dict]:
    # update_search_settings tunes the pipeline web_search/fetch_url run on,
    # so its wire exposure rides the SAME gate. Registered as its own spec
    # appended at the END of the base phase: inserting it into the search spec
    # put it mid-list (position 2), which broke the cache-stable tool-order ratchet
    # (tests/tools/test_tool_registry.py::TestOrdering).
    if not ctx.durable_state_available:
        return []
    from lib.tools.search import build_update_search_settings_tool
    return [build_update_search_settings_tool()]


def _build_knowledge(ctx: ToolContext) -> list[dict]:
    # The corpus owns its own persisted enable switch. Crucially, an empty or
    # disabled corpus contributes ZERO schema tokens — no phantom knowledge
    # tool in fresh installs and no invitation for the model to call a tool
    # that cannot return evidence.
    if not ctx.durable_state_available:
        return []
    from lib.knowledge.tool import build_tool
    return build_tool(ctx)


def _handle_knowledge(*args, **kwargs):
    """Lazy dispatch shim; keeps knowledge storage out of registry import."""
    from lib.knowledge.tool import handle_tool
    return handle_tool(*args, **kwargs)


def _build_fetch(ctx: ToolContext) -> list[dict]:
    # Built per call: the schema's ``reason`` param follows the runtime
    # LLM_CONTENT_FILTER_ENABLED flag — a module-level constant would freeze
    # whatever the import-time snapshot saw (same rationale as build_search_tool).
    from lib.tools.search import build_fetch_url_tool
    return [build_fetch_url_tool()]


def _build_browser_download(ctx: ToolContext) -> list[dict]:
    """Append-only explicit server-location download capability."""
    from lib.tools.search import build_browser_download_url_to_server_tool
    return [build_browser_download_url_to_server_tool()]


def _build_read_files(ctx: ToolContext) -> list[dict]:
    # read_files is ALWAYS on — handles project-relative AND absolute local
    # paths (images, PDFs, Office docs, text), so the model can read local
    # content even with no project attached.
    from lib.tools.project import READ_FILES_TOOL
    return [READ_FILES_TOOL]


def _build_inspect_image(ctx: ToolContext) -> list[dict]:
    # inspect_image is ALWAYS on (like read_files) — it re-renders a region
    # of any local image at full resolution so the model can read detail the
    # initial downscale discarded. No project / vision toggle gates it; the
    # dispatch path drops the resulting image for text-only models anyway.
    from lib.tools.image_edit import INSPECT_IMAGE_TOOL
    return [INSPECT_IMAGE_TOOL]


def _build_project_result_meta(
    tool_name: str,
    tool_args: dict,
    tool_content: str,
) -> dict:
    """Lazy project-plugin adapter for the core result-metadata seam."""
    from lib.tools.meta import build_project_tool_meta
    return build_project_tool_meta(tool_name, tool_args, tool_content)


def project_dispatch_tool_schemas(names) -> list[dict]:
    """Copy canonical file declarations for an already admitted dispatch profile."""
    from copy import deepcopy
    from lib.tools.project import READ_FILES_TOOL, PROJECT_TOOL_WRITE_FILE, PROJECT_TOOL_EDIT_FILE
    return [deepcopy(tool) for tool in (
        READ_FILES_TOOL, PROJECT_TOOL_WRITE_FILE, PROJECT_TOOL_EDIT_FILE,
    ) if tool['function']['name'] in names]


def _build_project_or_code_exec(ctx: ToolContext) -> list[dict]:
    # ``project_ready`` is evaluated from the current request, so attaching or
    # detaching Project Brain changes the next assembled tool surface directly.
    from lib.tools.code_exec import CODE_EXEC_TOOL
    from lib.tools.project import (project_tools_for_runtime,
                                   with_command_input_observation_scope)
    project_tools = project_tools_for_runtime()
    if ctx.project_ready:
        # Environment-specific routing guidance belongs in the context
        # composer's trailing user carrier. Tool declarations stay canonical
        # across local/remote and single/multi-root requests.
        tools = list(project_tools)
    else:
        tools = [CODE_EXEC_TOOL]
    return with_command_input_observation_scope(
        tools, evolution=(ctx.cfg or {}).get('evolution'),
        project_ready=ctx.project_ready)


def _build_browser(ctx: ToolContext) -> list[dict]:
    from lib.browser.availability import browser_extension_availability
    if browser_extension_availability(ctx.owner_user_id)['available']:
        from lib.browser.advanced import ADVANCED_BROWSER_TOOLS
        from lib.tools.browser import browser_tools
        tools = browser_tools() + list(ADVANCED_BROWSER_TOOLS)
        logger.debug('[Task %s] Browser extension connected — browser tools '
                     'enabled (%d tools)', ctx.tid, len(tools))
        return tools
    logger.debug('[Task %s] Browser tools unavailable: extension not connected',
                 ctx.tid)
    return []


def _build_desktop(ctx: ToolContext) -> list[dict]:
    from lib.desktop import is_desktop_agent_connected
    if is_desktop_agent_connected():
        from lib.desktop_tools import DESKTOP_TOOLS
        logger.debug('[Task %s] 🖥️ Desktop agent connected — %d desktop tools '
                     'enabled', ctx.tid, len(DESKTOP_TOOLS))
        return list(DESKTOP_TOOLS)
    logger.debug('[Task %s] Desktop tools unavailable: agent not connected',
                 ctx.tid)
    return []
















def _build_page_preview(ctx: ToolContext) -> list[dict]:
    # Server-side rendered page preview (browser_preview_page). Gated on a
    # project being attached — the primary mode renders a project file —
    # and deliberately NOT on browser_enabled / the extension: the render
    # runs in the shared server-side Playwright pool. Pool unavailability
    # is reported by the handler at call time, never here (schema build
    # must not launch Chromium).
    if not ctx.project_ready:
        return []
    from lib.tools.browser import BROWSER_TOOL_PREVIEW_PAGE
    logger.debug('[Task %s] Page-preview tool enabled (server-side render)',
                 ctx.tid)
    return [BROWSER_TOOL_PREVIEW_PAGE]


def _build_conv_ref(ctx: ToolContext) -> list[dict]:
    # CONV_REF_TOOLS = [list_conversations, get_conversation] — BOTH are
    # read-only (discover siblings + open one). Register them in two cases:
    #   (a) the user @-mentioned a conversation (the classic explicit path), OR
    #   (b) we're in project mode — the always-on cross-conv digest
    #       (Context Composer) names sibling conversations for ambient
    #       awareness, so the model must be ABLE to open a surfaced sibling
    #       rather than being told about phantom tools. Gating only on
    #       has_conv_ref meant the digest header advertised tools absent from
    #       the schema on a plain project turn (the conv_tools_available
    #       branch). Registering them in project mode closes that gap.
    # Both branches require at least one base tool (current_count > 0): with no
    # tools at all there's no schema to extend.
    #
    # Project Brain is signal-driven and contributes no tool schemas.  The
    # adjacent project_integration spec owns the two retained execution tools.
    if not ctx.durable_state_available or ctx.current_count <= 0:
        return []
    if ctx.has_conv_ref or (ctx.project_enabled and ctx.project_path):
        from lib.tools.conversation import CONV_REF_TOOLS
        logger.debug('[Task %s] 💬 conv_ref tools enabled (has_conv_ref=%s '
                     'project=%s)', ctx.tid, ctx.has_conv_ref,
                     bool(ctx.project_enabled and ctx.project_path))
        return list(CONV_REF_TOOLS)
    return []


def _build_project_integration(ctx: ToolContext) -> list[dict]:
    """Expose only execution controls for an existing isolated workspace."""
    if not ctx.durable_state_available or ctx.current_count <= 0:
        return []
    if ctx.project_enabled and ctx.project_path:
        from lib.tools.conversation import INTEGRATION_TOOLS
        from lib.conversations.project_messages import TOOLS
        return list(INTEGRATION_TOOLS) + list(TOOLS)
    return []


def canonical_human_guidance_schema() -> dict:
    """Return the registry-owned canonical ``ask_human`` wire schema.

    Core callers use this registry seam instead of importing the concrete
    human-guidance plugin module directly.
    """
    from lib.tools.human_guidance import ASK_HUMAN_TOOL
    return ASK_HUMAN_TOOL


def _build_coordinator(ctx: ToolContext) -> list[dict]:
    from lib.project_dispatch.admission import KEY, repository_for
    from lib.tasks_pkg.coordinator_policy import coordinator_scope
    if KEY in ctx.cfg:
        # Catalog assembly has a config, not the executing task/account. Resolve
        # its immutable grant here; the assembler and every call check the real
        # physical task separately. Never construct a synthetic human task.
        binding = ctx.cfg[KEY]
        row = repository_for(binding).validate(binding)
        if (row['request'].get('profile', {}).get('mode') != 'coordinator'
                or binding['owner'] != ctx.owner_user_id or binding['recipientId'] != ctx.conv_id):
            return []
    elif not ctx.owner_user_id or not coordinator_scope({
            '_userId': ctx.owner_user_id, 'convId': ctx.conv_id,
            'config': ctx.cfg}, ctx.project_path):
        return []
    from lib.tools.coordinator import COORDINATOR_TOOLS
    return list(COORDINATOR_TOOLS)


def _build_human_guidance(ctx: ToolContext) -> list[dict]:
    if ctx.current_count > 0:
        logger.debug('[Task %s] 🙋 Human guidance (ask_human) tool available',
                     ctx.tid)
        return [canonical_human_guidance_schema()]
    return []


def _build_memory(ctx: ToolContext) -> list[dict]:
    # Memory tools and memory context are one user-facing capability: the
    # Memory switch controls both. They still require a real base tool.
    # ``ctx.lean`` is a retained seam (chat_mode.is_lean_mode, currently always
    # False after the air/pro merge) for a future auto-retract-tools feature
    # that would ship only the base search/fetch/read tools on a simple turn.
    if (not ctx.durable_state_available or ctx.lean or not ctx.has_base_tools
            or not bool(ctx.cfg.get('memoryEnabled', True))):
        return []
    from copy import deepcopy
    from lib.memory.tools import ALL_MEMORY_TOOLS
    # Project availability is runtime context, not a reason to rewrite the
    # declaration. The composer tells no-project turns to use global scope;
    # the handler remains the authority boundary.
    return deepcopy(ALL_MEMORY_TOOLS)


def _build_skills(ctx: ToolContext) -> list[dict]:
    # Compact read/discovery tools stay direct: installed-skill routing should
    # not pay an extra gateway round before load_skill can amplify the task.
    if ctx.lean or not ctx.has_base_tools:
        return []
    from lib.skills import SKILL_READ_TOOLS
    return list(SKILL_READ_TOOLS)


def _build_skill_install(ctx: ToolContext) -> list[dict]:
    # The safety-sensitive mutator stays in the immutable executable catalog
    # but is Tool-Search deferred on larger surfaces. Small surfaces retain it
    # directly because an extra discovery round would cost more than its schema.
    if ctx.lean or not ctx.has_base_tools:
        return []
    from lib.skills import SKILL_INSTALL_TOOLS
    return list(SKILL_INSTALL_TOOLS)


def _build_todo(ctx: ToolContext) -> list[dict]:
    # Structured task checklist (todo_write). Attaches whenever ANY base tool
    # exists — it's a lightweight, always-useful progress tracker that also
    # feeds the continuation enforcer, so it needs no user-facing toggle
    # (mirrors the memory-tools attachment rule). A pure-chat turn with no
    # tools does not get it (nothing to track). ``ctx.lean`` is a retained seam
    # (always False today; see _build_memory) for a future auto-retract.
    if ctx.lean or not ctx.has_base_tools:
        return []
    from lib.tools.todo import TODO_WRITE_TOOL
    return [TODO_WRITE_TOOL]




def _build_scheduler(ctx: ToolContext) -> list[dict]:
    # Scheduler tools are a DEFAULT capability (like memory / todo): they
    # attach whenever ANY base tool exists, NOT gated on a user toggle. The
    # scheduler_enabled flag survives on the ToolContext for back-compat but no
    # longer controls tool exposure — there is no composer toggle anymore.
    # ``ctx.lean`` is a retained seam (always False today; see _build_memory)
    # for a future auto-retract.
    if (not ctx.durable_state_available
            or ctx.lean or not ctx.has_base_tools):
        return []
    from lib.scheduler.tool_defs import SCHEDULER_TOOLS
    logger.debug('[Task %s] ⏰ Scheduler tools enabled (%d tools)',
                 ctx.tid, len(SCHEDULER_TOOLS))
    return [tool for tool in SCHEDULER_TOOLS
            if tool["function"]["name"] != "timer_create"]


def _build_timer_watcher(ctx: ToolContext) -> list[dict]:
    """Keep the readiness handoff discoverable without a catalog search."""
    if not ctx.durable_state_available or ctx.lean or not ctx.has_base_tools:
        return []
    from lib.scheduler.tool_defs import TIMER_TOOL_CREATE
    return [TIMER_TOOL_CREATE]


def _build_tool_effect_inspection(ctx: ToolContext) -> list[dict]:
    if not ctx.durable_state_available or not ctx.conv_id:
        return []
    from lib.tools.tool_effect_inspection import TOOL_EFFECT_INSPECTION
    return [TOOL_EFFECT_INSPECTION.provider_schema()]


def _build_tool_result_artifacts(ctx: ToolContext) -> list[dict]:
    if not ctx.durable_state_available:
        return []
    from lib.context_experiment_flags import normalize_context_experiment_flags
    if normalize_context_experiment_flags(
            ctx.cfg)["tools"]["resultEnvelope"] != "v2":
        return []
    from lib.tools.tool_result_artifacts import build_tool_result_artifact_tools
    return build_tool_result_artifact_tools()


def _build_working_facts(ctx: ToolContext) -> list[dict]:
    if not ctx.durable_state_available:
        return []
    from lib.tools.working_facts import WORKING_FACT_CONTRACTS
    return [contract.provider_schema() for contract in WORKING_FACT_CONTRACTS]


def _handle_working_facts(*args, **kwargs):
    from lib.tasks_pkg.handlers.working_facts import handle_working_facts
    return handle_working_facts(*args, **kwargs)


def _build_swarm(ctx: ToolContext) -> list[dict]:
    # NOT gated on has_base_tools — a bare-conversation research swarm is a
    # valid use case (mirrors the read_files decoupling). await_agents is
    # retired from the emitted surface (unified ``poll`` absorbs it) but
    # stays registered for conversation-latched schemas.
    from lib.swarm.tools import (
        GET_AGENT_RESULT_TOOL,
        SPAWN_AGENTS_TOOL,
    )

    from lib.swarm.message_contract import SEND_AGENT_MESSAGE_TOOL, GET_AGENT_MESSAGES_TOOL
    from lib.tasks_pkg.approval_delegate import SET_APPROVAL_DELEGATE_TOOL
    logger.debug('[Task %s] 🐝 Async swarm enabled — spawn_agents / '
                 'get_agent_result (project_enabled=%s)',
                 ctx.tid, ctx.project_enabled)
    return [SPAWN_AGENTS_TOOL, GET_AGENT_RESULT_TOOL,
            SET_APPROVAL_DELEGATE_TOOL, SEND_AGENT_MESSAGE_TOOL, GET_AGENT_MESSAGES_TOOL]


def _build_background(ctx: ToolContext) -> list[dict]:
    # poll/kill are the control plane for background work: they attach
    # whenever the model can START background work (run_command via project
    # or code_exec, swarm via spawn_agents), and are deliberately NOT
    # retracted by the lean seam — retracting them while keeping
    # run_command(background=true) would strand live handles behind a
    # dead-end pointer.
    if not ctx.durable_state_available:
        return []
    has_swarm = any(
        tool.get('function', {}).get('name') == 'spawn_agents'
        for tool in ctx.executable_tool_catalog)
    if not (ctx.project_ready or ctx.code_exec_enabled
            or ctx.has_base_tools or has_swarm):
        return []
    from lib.tools.poll import KILL_TOOL, POLL_TOOL
    return [POLL_TOOL, KILL_TOOL]


def _freeze_empty_mcp_wire(ctx: ToolContext) -> list[dict] | None:
    """Freeze an empty MCP wire for this conversation's selection scope.

    The tools array opens every provider request, so a server that connects
    mid-conversation must not enter it — the composer surfaces those schemas
    in a per-turn tail block instead (``execute_tools`` still reaches them
    through the authority catalog). Freezing at the scope's first assembly
    keeps the wire byte-stable from turn one. Explicit non-adaptive exposure
    modes (progressive/inline) manage their own wire and are left alone.
    """
    exposure = str(ctx.cfg.get(
        'mcpToolExposure', 'auto') or 'auto').strip().lower()
    if exposure in ('progressive', 'wrapper', 'legacy', 'inline', 'all',
                    'full', 'delegate'):
        return
    from lib.mcp.tool_search import freeze_wire_definitions
    return freeze_wire_definitions(ctx.cfg['_mcpSelectionScopeId'], [])


def _mcp_read_only_by_name(tools: list[dict], snapshot) -> dict[str, bool]:
    """Bind read recovery to unique live metadata for the assembled schema."""
    definitions = {
        (tool.get('function') or {}).get('name'): tool for tool in tools}
    read_only = {}
    for row in snapshot:
        if not isinstance(row, Mapping):
            continue
        name = row.get('namespaced_name')
        if not isinstance(name, str) or name not in definitions:
            continue
        if name in read_only:
            # Ambiguous declarations cannot establish repeatability.
            read_only[name] = False
            continue
        read_only[name] = (
            row.get('read_only_hint') is True
            and row.get('openai_def') == definitions[name])
    return read_only


def _build_mcp(ctx: ToolContext) -> list[dict]:
    # Clear prior assembly grants even when a reused context is disconnected.
    ctx.cfg['_mcpAllowedToolCatalog'] = []
    ctx.cfg['_mcpToolReadOnlyByName'] = {}
    ctx.cfg['_mcpToolSearchTextByName'] = {}
    ctx.cfg['_mcpActiveToolNames'] = []
    if ctx.owner_user_id < 1 or not ctx.cfg.get('mcpEnabled', True):
        return []
    from lib.mcp.tool_search import frozen_wire_definitions, mcp_selection_scope_id
    scope = mcp_selection_scope_id(
        task_id=getattr(ctx, 'task_id', ''), conv_id=getattr(ctx, 'conv_id', ''),
        owner_user_id=ctx.owner_user_id, storage_free=ctx.cfg.get('_storageFreeRuntime') is True)
    ctx.cfg['_mcpSelectionScopeId'] = scope
    exposure = str(ctx.cfg.get('mcpToolExposure', 'auto') or 'auto').strip().lower()
    adaptive = exposure not in (
        'progressive', 'wrapper', 'legacy', 'inline', 'all', 'full', 'delegate')
    frozen = frozen_wire_definitions(scope) if adaptive else None
    active = _build_mcp_live(ctx, frozen, adaptive=adaptive)
    ctx.cfg['_mcpActiveToolNames'] = [
        str((tool.get('function') or {}).get('name') or '') for tool in active]
    return active


def _build_mcp_live(
        ctx: ToolContext, frozen: list[dict] | None, *, adaptive: bool) -> list[dict]:
    from lib.agent_core.tool_errors import MCPDeclarationError
    try:
        from lib.mcp import get_bridge
        bridge = get_bridge(ctx.owner_user_id)
        if bridge.connected:
            # The bridge cache is process-shared. Give each task an isolated
            # catalog before any provider projection can add/remove fields.
            from copy import deepcopy
            mcp_tools = deepcopy(bridge.get_openai_tool_defs())
            ctx.cfg['_mcpAllowedToolCatalog'] = mcp_tools
            if mcp_tools:
                catalog_fingerprint = ''
                try:
                    catalog_fingerprint, snapshot = (
                        bridge.get_tool_catalog_projection())
                except AttributeError as exc:
                    logger.debug('[Tools] bridge catalog projection unavailable: %s',
                                 exc)
                    try:
                        snapshot = bridge.get_tool_catalog_snapshot()
                    except AttributeError as snapshot_exc:
                        logger.debug('[Tools] bridge catalog snapshot unavailable: %s',
                                     snapshot_exc)
                        # Compatibility for lightweight third-party/fake bridges.
                        snapshot = [{
                            'openai_def': tool,
                            'namespaced_name': (
                                (tool.get('function') or {}).get('name') or ''),
                            'meta': {},
                        } for tool in mcp_tools]
                # The cached full list is the allowed upper bound and becomes
                # part of the task authority catalog even when only a small
                # native-schema subset is visible on the initial wire.
                ctx.cfg['_mcpAllowedToolCatalog'] = mcp_tools
                ctx.cfg['_mcpToolReadOnlyByName'] = _mcp_read_only_by_name(
                    mcp_tools, snapshot)
                # Keep rich MCP discovery metadata beside the schemas. It is
                # consumed by local retrieval only and must never be copied
                # into the provider-visible function definitions.
                try:
                    _search_text_by_name = (
                        bridge.get_tool_catalog_search_text_projection())
                except AttributeError:
                    from lib.mcp.tool_search import catalog_search_text_by_name
                    _search_text_by_name = catalog_search_text_by_name(snapshot)
                ctx.cfg['_mcpToolSearchTextByName'] = _search_text_by_name

                exposure = str(ctx.cfg.get(
                    'mcpToolExposure', 'auto') or 'auto').strip().lower()
                if exposure == 'delegate':
                    # Delegate mode: zero MCP schemas on the orchestrator's
                    # own wire. The full allowed catalog set above stays the
                    # execution authority, so search_tools/execute_tools still
                    # reach every MCP tool and spawn_agents sub-agents inherit
                    # the complete catalog through ``all_tools`` (the parent
                    # executable catalog, not the wire subset). The composer
                    # tail block ``mcp_tools_delta`` advertises names+hints
                    # cheaply each turn. No selection freeze is written: the
                    # mode is a per-assembly decision so an explicit mid-
                    # conversation exposure switch never fights stale freeze
                    # state.
                    from lib.mcp.tool_search import mcp_selection_scope_id
                    ctx.cfg['_mcpSelectionScopeId'] = mcp_selection_scope_id(
                        task_id=getattr(ctx, 'task_id', ''),
                        conv_id=getattr(ctx, 'conv_id', ''),
                        owner_user_id=getattr(ctx, 'owner_user_id', 0),
                        storage_free=ctx.cfg.get('_storageFreeRuntime') is True)
                    ctx.cfg['_mcpActiveToolNames'] = []
                    logger.info('[Task %s] MCP delegate exposure: 0 of %d '
                                'schemas on wire; authority catalog retained '
                                'for execute_tools and sub-agent delegation',
                                ctx.tid, len(mcp_tools))
                    return []
                if exposure in ('progressive', 'wrapper', 'legacy'):
                    # Explicit backwards-compatibility only. Auto mode now
                    # preselects native schemas and never asks the model to
                    # discover through a generic invoke wrapper.
                    from lib.mcp.progressive import MCP_PROGRESSIVE_TOOL_DEFS
                    ctx.cfg['_mcpActiveToolNames'] = [
                        str((tool.get('function') or {}).get('name') or '')
                        for tool in MCP_PROGRESSIVE_TOOL_DEFS]
                    return list(MCP_PROGRESSIVE_TOOL_DEFS)
                if exposure not in ('inline', 'all', 'full'):
                    if frozen is not None:
                        return frozen
                    def _message_text(message):
                        content = message.get('content') \
                            if isinstance(message, dict) else ''
                        if isinstance(content, str):
                            return content
                        if isinstance(content, list):
                            return ' '.join(
                                str(block.get('text') or '')
                                for block in content if isinstance(block, dict))
                        return ''

                    # Source identity, not transport role, selects intent.
                    # Engine notes may travel as user messages; explicit human
                    # steering remains eligible through the shared semantics.
                    from lib.tasks_pkg.message_semantics import is_human_message
                    query = ''
                    for message in reversed(
                            getattr(ctx, 'messages', None) or []):
                        if is_human_message(message):
                            query = _message_text(message)[-8_000:]
                            break
                    from lib.mcp.tool_search import (
                        mcp_selection_scope_id,
                        recent_conversation_mcp_tool_names,
                        select_active_mcp_tools,
                    )
                    selection_scope_id = mcp_selection_scope_id(
                        task_id=getattr(ctx, 'task_id', ''),
                        conv_id=getattr(ctx, 'conv_id', ''),
                        owner_user_id=getattr(ctx, 'owner_user_id', 0),
                        storage_free=ctx.cfg.get('_storageFreeRuntime') is True,
                    )
                    ctx.cfg['_mcpSelectionScopeId'] = selection_scope_id
                    configured_used = list(
                        ctx.cfg.get('_mcpUsedToolNames') or [])
                    historical_used = recent_conversation_mcp_tool_names(
                        getattr(ctx, 'messages', None),
                        limit=ctx.cfg.get('mcpActiveToolLimit', 8),
                    )
                    used_names = list(dict.fromkeys(
                        [*configured_used, *historical_used]))
                    try:
                        active = select_active_mcp_tools(
                            snapshot, task_id=getattr(ctx, 'task_id', ''),
                            selection_scope_id=selection_scope_id,
                            query=query, used_names=used_names,
                            limit=ctx.cfg.get('mcpActiveToolLimit', 8),
                            catalog_fingerprint=catalog_fingerprint)
                    except MCPDeclarationError:
                        raise
                    except Exception as search_exc:
                        raise MCPDeclarationError(
                            'MCP initial declaration selection failed') from search_exc
                    ctx.cfg['_mcpActiveToolNames'] = [
                        str((tool.get('function') or {}).get('name') or '')
                        for tool in active]
                    logger.info('[Task %s] MCP pre-request Tool Search: %d '
                                'allowed -> %d active native schemas (%d servers)',
                                ctx.tid, len(mcp_tools), len(active),
                                bridge.server_count)
                    return active
                ctx.cfg['_mcpActiveToolNames'] = [
                    str((tool.get('function') or {}).get('name') or '')
                    for tool in mcp_tools]
                logger.info('[Task %s] MCP tools loaded inline: %d from %d servers',
                            ctx.tid, len(mcp_tools), bridge.server_count)
                return list(mcp_tools)
    except MCPDeclarationError:
        raise
    except Exception as exc:
        # Assembly may have published a partial catalog before the outage.
        ctx.cfg['_mcpAllowedToolCatalog'] = []
        ctx.cfg['_mcpToolReadOnlyByName'] = {}
        ctx.cfg['_mcpToolSearchTextByName'] = {}
        ctx.cfg['_mcpActiveToolNames'] = []
        if adaptive and frozen is None:
            raise MCPDeclarationError('MCP catalog assembly failed') from exc
        if frozen is None:
            # Explicit exposure modes do not select durable declarations.
            logger.warning('[Task %s] MCP catalog unavailable; continuing '
                           'without MCP tools: %s', ctx.tid, exc)
            return []
        logger.warning('[Task %s] MCP catalog unavailable; retaining durable '
                       'declarations, not cached execution grants: %s', ctx.tid, exc)
    if frozen is not None:
        return frozen
    empty = _freeze_empty_mcp_wire(ctx)
    return empty if empty is not None else []


def _observe_evolution_source_input(task, wire_body):
    if not task.get('_evolutionSourcePendingReads'):
        return None
    from lib.evolution.source_tools import prepare_model_input
    return prepare_model_input(task, wire_body)


def _build_evolution_artifact(ctx: ToolContext) -> list[dict]:
    if not ctx.cfg.get('evolution'):
        return []
    from lib.evolution.artifact_evidence import build
    return build(ctx)


def _build_evolution_review(ctx: ToolContext) -> list[dict]:
    if not ctx.cfg.get('evolution'):
        return []
    from lib.evolution.review_delivery import build
    return build(ctx)


def _build_evolution_source(ctx: ToolContext) -> list[dict]:
    if not ctx.cfg.get('evolution'):
        return []
    from lib.evolution.source_tools import build
    return build(ctx)


def _build_evolution_teach(ctx: ToolContext) -> list[dict]:
    binding = ctx.cfg.get('evolution') or {}
    if not binding.get('goal_id') or binding.get('stage') == 'reviewing':
        return []
    from lib.evolution.coordination import tool_definition
    from lib.evolution.refetch import REINDEX_TOOL_DEFINITION
    from lib.evolution.exploration_tools import tool_definition as exploration_definition
    if binding.get('stage') == 'coordinator':
        from lib.evolution.library import material_tool_definition
        return [tool_definition(), material_tool_definition(),
                REINDEX_TOOL_DEFINITION]
    from lib.evolution.teach import TEACH_TOOL_DEFINITION
    result = [TEACH_TOOL_DEFINITION]
    if binding.get('run_id') and binding.get('stage') == 'executing':
        from lib.evolution.library import material_tool_definition
        result.extend([material_tool_definition(), tool_definition(),
                       REINDEX_TOOL_DEFINITION, exploration_definition()])
    return result


def _handle_evolution_teach(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
                           cfg, project_path, project_enabled, all_tools=None):
    from lib.tasks_pkg.executor import _finalize_tool_round
    if fn_name == 'evolution_source':
        from lib.evolution.source_tools import handle as handler
    elif fn_name == 'evolution_review':
        from lib.evolution.review_delivery import handle as handler
    elif fn_name == 'evolution_artifact':
        from lib.evolution.artifact_evidence import handle as handler
    elif fn_name == 'evolution_coordinate':
        from lib.evolution.coordination import handle_coordinate as handler
    elif fn_name == 'evolution_material':
        from lib.evolution.library import handle_material as handler
    elif fn_name == 'evolution_explore':
        from lib.evolution.exploration_tools import handle_explore as handler
    elif fn_name == 'evolution_reindex':
        from lib.evolution.tool_handler import handle_evolution_reindex as handler
    else:
        from lib.evolution.tool_handler import handle_evolution_teach as handler
    entries = handler(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
                      cfg, project_path, project_enabled, all_tools)
    _finalize_tool_round(task, rn, round_entry, entries)
    # Model text: ``entries`` is the frontend display contract — source/
    # fetched/badge are UI chrome, and snippets may already carry a JSON
    # payload (material list, coordinate result). Re-serializing the dicts
    # would wrap that payload in a second JSON encoding (every quote escaped)
    # around ~85 bytes of constant keys. Project title+snippet directly.
    parts = []
    for entry in entries:
        if not isinstance(entry, dict):
            parts.append(str(entry))
            continue
        title = str(entry.get('title') or '')
        snippet = str(entry.get('snippet') or '')
        parts.append(f'{title}: {snippet}' if title and snippet
                     else title or snippet)
    return tc_id, '\n'.join(part for part in parts if part) or 'ok', False


def _build_custom(ctx: ToolContext) -> list[dict]:
    # Per-request custom tools brought by a headless /api/v1/agent/run caller.
    # The route validates + mints a ToolEnvironment, stashes its clean schemas
    # on cfg['_customToolSchemas'], and attaches the env as task['_tool_env']
    # (whose handlers the executor resolves before the global registry).
    # Registered LAST so the cache-stable built-in ordering is untouched.
    schemas = ctx.cfg.get('_customToolSchemas')
    if not schemas or not isinstance(schemas, list):
        return []
    logger.info('[Task %s] 🧩 Custom tools injected: %d', ctx.tid, len(schemas))
    return list(schemas)


def build_builtin_specs() -> list[ToolSpec]:
    """Register the built-in tool specs in canonical (cache-stable) order."""

    from lib.tools.rich_response import build_spec as build_rich_spec
    from lib.tools.tool_effect_inspection import TOOL_EFFECT_INSPECTION
    from lib.tools.tool_result_artifacts import TOOL_RESULT_ARTIFACT_CONTRACTS
    from lib.tools.working_facts import WORKING_FACT_CONTRACTS
    from lib.tools.gateway import GATEWAY_TOOL_NAMES

    builtins = [
        # ── base phase (counted toward has_real_tools) ──
        ToolSpec('search', _build_search, phase='base',
                 provides=frozenset({'web_search'}),
                 idempotent_tools=frozenset({'web_search'}),
                 timeout_ms_by_name={'web_search': 60_000},
                 category='search', description='Web search',
                 gate='输入框 → 搜索模式（联网/multi）',
                 exposure_gate=lambda ctx: ctx.search_mode in (
                     'single', 'multi')),
        ToolSpec('fetch', _build_fetch, phase='base',
                 provides=frozenset({'fetch_url'}),
                 idempotent_tools=frozenset({'fetch_url'}),
                 programmatic_tools=frozenset({'fetch_url'}),
                 timeout_ms_by_name={'fetch_url': 60_000},
                 script_safe=True,
                 category='search', description='Fetch a URL',
                 gate='搜索开启或抓取开关（默认开）',
                 exposure_gate=lambda ctx: (
                     ctx.fetch_enabled or ctx.search_enabled)),
        ToolSpec('read_files', _build_read_files, phase='base',
                 provides=frozenset({'read_files'}),
                 idempotent_tools=frozenset({'read_files'}),
                 programmatic_tools=frozenset({'read_files'}),
                 result_recovery_by_name={'read_files': 'source'},
                 script_safe=True,
                 category='project', description='Read local files',
                 gate='常开（无需项目）'),
        ToolSpec('inspect_image', _build_inspect_image, phase='base',
                 provides=frozenset({'inspect_image'}),
                 idempotent_tools=frozenset({'inspect_image'}),
                 category='project', description='Zoom/rotate/crop image viewer',
                 gate='常开（无需项目）'),
        ToolSpec('project', _build_project_or_code_exec, phase='base',
                 compatibility_tools=frozenset({'apply_diff', 'apply_diffs', 'insert_content', 'insert_contents', 'check_background_command', 'kill_background_command'}),
                 provides=frozenset({
                     'grep_search', 'find_files',
                     'write_file', 'edit_file', 'apply_diff', 'apply_diffs',
                     'insert_content', 'insert_contents',
                     'run_command',
                     'check_background_command', 'kill_background_command',
                     'repo_map',
                 }),
                 write_tools=frozenset({
                     'write_file', 'edit_file', 'apply_diff', 'apply_diffs',
                     'insert_content', 'insert_contents',
                     'run_command', 'kill_background_command',
                 }),
                 idempotent_tools=frozenset({
                     'grep_search', 'find_files',
                     'check_background_command', 'repo_map',
                 }),
                 programmatic_tools=frozenset({
                     'grep_search', 'find_files',
                 }),
                 search_hints={
                     'grep_search': (
                         'symbol references usages occurrences who calls '
                         'code content search 查找引用 谁调用了'),
                     'find_files': (
                         'locate filenames glob config files 查找文件 配置文件'),
                     'write_file': 'create overwrite save file 写入 保存文件',
                     'edit_file': (
                         'edit insert before after replace patch source code '
                         '修改代码 插入内容 替换'),
                     'apply_diff': (
                         'edit change modify fix implementation source code '
                         '修改代码 修复实现'),
                     'apply_diffs': 'edit modify fix several files 批量修改代码',
                     'insert_content': 'insert append text into file 插入内容',
                     'insert_contents': 'insert append several files 批量插入',
                     'run_command': 'shell terminal execute command 运行命令',
                     'check_background_command': (
                         'check background command output status 后台命令状态'),
                     'kill_background_command': (
                         'kill stop background command process 停止后台进程'),
                     'repo_map': 'repository map symbols outline 代码地图 符号大纲',
                 },
                 category='project', description='Project file tools / code exec',
                 gate='挂载项目（输入框 → 项目）或开启代码执行',
                 exposure_gate=lambda ctx: (
                     ctx.project_ready or ctx.code_exec_enabled),
                 result_meta_builder=_build_project_result_meta),
        ToolSpec('browser', _build_browser, phase='base',
                 # 15 names = BROWSER_TOOLS (13) + ADVANCED_BROWSER_TOOLS (2).
                 # v2 (): the ten retired names are NOT
                 # declared — their tool_registry registration shrank with
                 # BROWSER_TOOL_NAMES, so there is no handler left to declare
                 # (their display formatters remain for history rendering).
                 provides=frozenset({
                     'browser_navigate', 'browser_read_page', 'browser_list_tabs',
                     'browser_research_page', 'browser_devtools', 'browser_site',
                     'browser_close_tab', 'browser_click', 'browser_type',
                     'browser_press_key', 'browser_execute_js',
                     'browser_screenshot', 'browser_get_cookies',
                     'browser_get_history', 'browser_fill_form',
                     'browser_menu_click',
                     'browser_check', 'browser_select_option',
                     'browser_scroll',
                 }),
                 # search_hints: private retrieval aliases/intents (ranking
                 # weight ×3, never serialized into the wire schema). Without
                 # them, terse wire descriptions let BM25 mis-rank core intents
                 # ("click a button" → get_cookies, "read the page" →
                 # list_tabs, "go to a url" → list_tabs). Each action verb is
                 # pinned to its tool; browser_fill_form yields click/button
                 # terms to browser_click. NOTE: _builtin_specs.py mirrors this
                 # ToolSpec for structure tests — keep them in sync.
                 search_hints={
                     'browser_click': (
                         'click press button link tap element click on '
                         'single one 点击 按钮 链接 点击元素 单击'),
                     'browser_navigate': (
                         'navigate go to open website url visit load page '
                         '打开网址 访问 跳转 进入网站'),
                     'browser_read_page': (
                         'read see view page content text what is on extract '
                         '看页面 读取内容 页面文字 提取'),
                     'browser_type': (
                         'type enter input text field write 输入 填写 打字'),
                     'browser_fill_form': (
                         'fill form multiple fields several at once upload '
                         'file attach batch 填表 表单 上传文件 批量填写 多个字段'),
                     'browser_press_key': (
                         'press key enter escape tab keyboard shortcut hotkey '
                         '按键 回车 快捷键'),
                     'browser_check': (
                         'check uncheck checkbox radio toggle switch '
                         '勾选 复选框 单选 开关'),
                     'browser_select_option': (
                         'select option dropdown combobox pick choose '
                         '下拉选择 选项 下拉框'),
                     'browser_menu_click': (
                         'menu right click context hover dropdown submenu '
                         '菜单 右键 悬停 子菜单'),
                     'browser_screenshot': (
                         'screenshot capture image picture snap 截图 截屏'),
                     'browser_list_tabs': (
                         'list tabs open tabs current tab 标签页 列出标签'),
                     'browser_close_tab': 'close tab 关闭标签页',
                     'browser_execute_js': (
                         'run javascript execute js code evaluate script '
                         '执行js 运行脚本'),
                     'browser_scroll': (
                         'scroll down up top bottom page reveal more content '
                         '滚动 下拉 翻页 到底部 查看更多'),
                 },
                 # These DRIVE the user's real browser session, so they belong
                 # in the serial write partition + behind the Manual approval
                 # gate (_pipeline.py derives needs_approval from it). Until
                 # this was declared, browser_execute_js could run arbitrary JS
                 # in the user's page with no prompt, from the parallel pool.
                 # NOTE: this makes them SERIAL — a deliberate behaviour change;
                 # concurrent clicks on one page were never actually safe.
                 write_tools=frozenset({
                     'browser_navigate', 'browser_click', 'browser_type',
                     'browser_press_key', 'browser_execute_js',
                     'browser_research_page', 'browser_devtools', 'browser_site',
                     'browser_fill_form', 'browser_menu_click',
                     'browser_close_tab',
                     'browser_check', 'browser_select_option',
                 }),
                 # Read-only observers stay parallel-safe AND cacheable within
                 # a task. browser_read_page/screenshot are deliberately NOT
                 # idempotent — the page changes under us between calls.
                 idempotent_tools=frozenset({
                     'browser_list_tabs',
                 }),
                 # Read-only/idempotent does not mean snapshot-stable. Tabs,
                 # page DOM, history and cookies may change between identical
                 # calls, so this live family opts out of same-task reuse.
                 cacheable_tools=frozenset(),
                 discovery_policy='searchable',
                 category='browser', description='Browser automation tools',
                 gate='安装并连接浏览器扩展（设置 → 网络）',
                 exposure_gate=lambda ctx: ctx.browser_enabled,
                 pin_on_exposure=True),
        ToolSpec('desktop', _build_desktop, phase='base',
                 # provides = LLM 可见的 10 个(desktop_move_file 刻意不
                 # 暴露,见 lib/desktop_tools.py;它仍列在 write_tools 里)。
                 provides=frozenset({
                     'desktop_list_files', 'desktop_read_file',
                     'desktop_write_file',
                     'desktop_open_file', 'desktop_open_app',
                     'desktop_run_command', 'desktop_screenshot',
                     'desktop_gui_action', 'desktop_clipboard',
                     'desktop_system_info',
                 }),
                 # 约束③:desktop 写/执行工具进串行写分区 + Manual 批准门 ——
                 # 此前未声明,既进并行派发池(竞态)又绕过批准门。
                 # desktop_system_info 豁免(其 kill 分支由 agent 侧参数级
                 # exec 门把守);GUI/screenshot 走 allow_gui 层,不进写分区。
                 write_tools=frozenset({
                     'desktop_write_file', 'desktop_move_file',
                     'desktop_run_command', 'desktop_open_app',
                     'desktop_open_file',
                 }),
                 search_hints={
                     'desktop_screenshot': (
                         'capture display monitor current screen desktop '
                         '截屏 屏幕 桌面画面'),
                     'desktop_clipboard': 'copy paste clipboard 剪贴板 复制 粘贴',
                     'desktop_list_files': 'list computer files 查看电脑文件',
                     'desktop_read_file': 'read computer file 读取电脑文件',
                     'desktop_write_file': 'write save computer file 保存电脑文件',
                     'desktop_run_command': 'run local command shell 执行本地命令',
                     'desktop_open_app': 'launch application 打开应用',
                     'desktop_open_file': 'open local file 打开文件',
                     'desktop_gui_action': 'mouse keyboard click type 鼠标 键盘',
                     'desktop_system_info': 'computer system information 系统信息',
                 },
                 discovery_policy='searchable',
                 category='desktop', description='Desktop agent tools',
                 gate='连接桌面 agent（设置 → 设备）',
                 exposure_gate=lambda ctx: ctx.desktop_enabled,
                 pin_on_exposure=True),
        # End of base phase: appending HERE keeps every earlier prefix
        # byte-stable for the prompt cache (the produce note above).
        ToolSpec('page_preview', _build_page_preview, phase='base',
                 provides=frozenset({'browser_preview_page'}),
                 discovery_policy='searchable',
                 search_hints={
                     'browser_preview_page': (
                         'render preview test html webpage frontend headless '
                         'chromium screenshot 真实浏览器 渲染网页 页面预览 '
                         '看看效果 前端界面'),
                 },
                 category='browser',
                 description='Server-side rendered page preview',
                 gate='挂载项目后可用'),
        ToolSpec('conv_ref', _build_conv_ref, phase='base',
                 provides=frozenset({'list_conversations', 'get_conversation'}),
                 write_tools=frozenset(),
                 idempotent_tools=frozenset({'list_conversations',
                                             'get_conversation'}),
                 # Sibling turns can advance while this long task is running.
                 cacheable_tools=frozenset(),
                 unchanged_receipt_tools=frozenset({
                     'list_conversations', 'get_conversation',
                 }),
                 programmatic_tools=frozenset({
                     'list_conversations', 'get_conversation',
                 }),
                 # Eager: the per-turn sibling-conversation digest names these
                 # two by function name when they are registered — deferring
                 # them re-creates the phantom-tool gap the project-mode
                 # branch was added to close.
                 category='conversation', description='Conversation reference tools',
                 gate='项目模式 或 @ 提及一个会话'),
        # Project Brain contributes zero schemas.  These two tools belong to
        # the isolated execution pipeline and are bound to the automatic work
        # ID; they do not read or mutate Board/Feed/Charter state.
        ToolSpec('project_integration', _build_project_integration, phase='base',
                 provides=frozenset({
                     'integration_checkpoint', 'integration_submit',
                     'send_project_message', 'get_project_messages', 'consider_project_message',
                 }),
                 write_tools=frozenset({'send_project_message', 'consider_project_message'}),
                 discovery_policy='searchable',
                 search_hints={
                     'integration_checkpoint': (
                         'snapshot isolated worktree progress milestone '
                         '保存隔离工作区检查点'),
                     'integration_submit': (
                         'submit isolated work for human review merge queue '
                         '提交隔离任务人工审查'),
                 },
                 category='conversation',
                 description='Isolated project integration execution controls',
                 gate='项目模式'),
        ToolSpec('coordinator', _build_coordinator, phase='base',
                 category='coordination',
                 description='Independent worker conversations and authorized dispatch',
                 gate='项目中绑定的 Coordinator 对话',
                 provides=frozenset({'create_conversation', 'dispatch_project_assignment',
                                    'get_project_assignment', 'report_to_user', 'delegate_project_work', 'await_project_workers', 'review_project_work', 'get_project_workers', 'cancel_project_work', 'finish_project_request'}),
                 write_tools=frozenset({'create_conversation', 'dispatch_project_assignment', 'report_to_user', 'delegate_project_work', 'review_project_work', 'cancel_project_work', 'finish_project_request'}),
                 idempotent_tools=frozenset({'get_project_assignment', 'report_to_user', 'delegate_project_work', 'await_project_workers', 'review_project_work', 'get_project_workers', 'cancel_project_work', 'finish_project_request'}),
                 timeout_ms_by_name={'await_project_workers': 1_805_000},
                 cacheable_tools=frozenset()),
        ToolSpec('human_guidance', _build_human_guidance, phase='base',
                 provides=frozenset({'ask_human'}),
                 category='human', description='Ask the human for guidance',
                 gate='输入框 → 人类指导开关',
                 exposure_gate=lambda ctx: ctx.human_guidance_enabled),
        # update_search_settings — appended at the END of the base phase so
        # every earlier tool's position stays byte-stable for the prompt
        # cache (the "appending HERE" rule this module's header documents).
        # It mutates server-GLOBAL config every conversation feels, so it is
        # approval-gated + serial like the other state-changing tools.
        ToolSpec('search_settings', _build_search_settings, phase='base',
                 provides=frozenset({'update_search_settings'}),
                 write_tools=frozenset({'update_search_settings'}),
                 discovery_policy='searchable',
                 category='search', description='Search/fetch pipeline settings',
                 gate='输入框 → 搜索模式（联网/multi）',
                 exposure_gate=lambda ctx: ctx.cfg.get('searchSettingsEnabled') is True),
        # Local knowledge is appended at the END of the base phase so existing
        # tool positions remain prompt-cache-stable when it is available.
        ToolSpec('knowledge', _build_knowledge, phase='base',
                 provides=frozenset({'search_knowledge'}),
                 idempotent_tools=frozenset({'search_knowledge'}),
                 programmatic_tools=frozenset({'search_knowledge'}),
                 discovery_policy='searchable', script_safe=True,
                 category='knowledge',
                 description='Enabled local knowledge base',
                 gate='本地知识库存在且已开启',
                 handler=_handle_knowledge,
                 catalog_active_only=True),
        # Explicit server-file semantics are append-only so every established
        # hot tool keeps its prompt-cache position. It rides the same runtime
        # availability gate as fetch_url but has its own stable contract/name.
        ToolSpec('browser_download', _build_browser_download, phase='base',
                 provides=frozenset({'browser_download_url_to_server'}),
                 cacheable_tools=frozenset(),
                 search_hints={
                     'browser_download_url_to_server': (
                         'download_url_to_server download save copy export fetch '
                         'archive zip install unzip latest file to server project '
                         'logged-in browser link button cookies intranet SSO '
                         '下载 保存 拷贝 复制 导出 安装 解压 最新版 服务器 本地 '
                         '内网 登录 文件 压缩包 链接 按钮'
                     ),
                 },
                 discovery_policy='eager',
                 native_route_groups=frozenset({
                     'search', 'fetch', 'browser', 'download'}),
                 category='browser',
                 description='Download a URL or page link to server staging',
                 gate='抓取开关（默认开）或连接浏览器扩展',
                 exposure_gate=lambda ctx: (
                     ctx.fetch_enabled or ctx.search_enabled
                     or ctx.search_mode in ('single', 'multi')
                     or ctx.browser_enabled)),
        # paper_build is appended at the END of the base phase so every
        # earlier tool keeps its prompt-cache position. It is NOT a write
        # tool: compilation only produces bounded cache artifacts under the
        # build service's supervision (identical in kind to produce_*
        # rendering), and approval-gating it would break the
        # edit→compile→fix loop that is its entire purpose. Results change
        # with source files, so it opts out of same-task result reuse.
        # Track C: PID-anchored citations. Search/audit are read-only network
        # lookups; paper_cite_insert mutates .bib/.tex through the journaled
        # write tools, so it rides the serial write partition + Manual
        # approval like paper_import_arxiv. Results change with the
        # literature indexes and source files, so nothing is cacheable.
        # paper_quality follows paper_build at the END of the base phase.
        # All three tools are read-only: the gate only reads the live source
        # tree and retained build artifacts, the dossier is deterministic
        # text, and the scaffold is a text proposal — no write partition,
        # no approval gate, no same-task result reuse.
        # ── capability phase ──
        ToolSpec('memory', _build_memory, phase='capability',
                 discovery_policy_by_name={name: 'searchable' for name in ('update_memory', 'delete_memory', 'merge_memories')},
                 provides=frozenset({
                     'search_memories', 'create_memory', 'update_memory',
                     'delete_memory', 'merge_memories',
                 }),
                 write_tools=frozenset({
                     'create_memory', 'update_memory',
                     'delete_memory', 'merge_memories',
                 }),
                 idempotent_tools=frozenset({'search_memories'}),
                 # Memory CRUD can change the same query inside one task.
                 cacheable_tools=frozenset(),
                 unchanged_receipt_tools=frozenset({'search_memories'}),
                 programmatic_tools=frozenset({'search_memories'}),
                 search_hints={
                     'search_memories': (
                         'recall remember previous decision past conversation '
                         '回忆 找回之前决定 拍板'),
                     'create_memory': 'remember save durable fact 记住 保存记忆',
                     'update_memory': 'correct revise saved memory 修改记忆',
                     'delete_memory': 'forget remove saved memory 忘记 删除记忆',
                     'merge_memories': 'combine duplicate memories 合并记忆',
                 },
                 category='memory', description='Memory CRUD tools',
                 gate='Memory 开关开启且有任意基础工具'),
        ToolSpec('skills', _build_skills, phase='capability',
                 provides=frozenset({
                     'search_skills', 'load_skill', 'read_skill_resource',
                 }),
                 idempotent_tools=frozenset({
                     'search_skills', 'load_skill', 'read_skill_resource',
                 }),
                 search_hints={
                     'search_skills': (
                         'find install discover workflow skill 技能 查找 安装'),
                     'load_skill': 'load workflow instructions 加载技能说明',
                     'read_skill_resource': (
                         'read skill reference script resource 技能资源'),
                 },
                 category='skills',
                 description='Bounded skill discovery and disclosure',
                 gate='有任意基础工具'),
        ToolSpec('skill_install', _build_skill_install, phase='capability',
                 provides=frozenset({'request_skill_install'}),
                 write_tools=frozenset({'request_skill_install'}),
                 confirmation_tools=frozenset({'request_skill_install'}),
                 auto_approval_tools=frozenset({'request_skill_install'}),
                 search_hints={
                     'request_skill_install': (
                         'install verified catalog skill 安装技能'),
                 },
                 discovery_policy='searchable',
                 category='skills',
                 description='Verified skill catalog installation',
                 gate='有任意基础工具；Auto 自动安装已验证包，Manual 真人确认'),
        ToolSpec('todo', _build_todo, phase='capability',
                 discovery_policy='searchable',
                 provides=frozenset({'todo_write'}),
                 category='task', description='Structured task checklist',
                 gate='常开（有任意基础工具即挂载）'),
        ToolSpec('scheduler', _build_scheduler, phase='capability',
                 compatibility_tools=frozenset({'await_task'}),
                 provides=frozenset({
                     'schedule_create', 'schedule_list', 'schedule_manage',
                     'timer_manage', 'await_task',
                 }),
                 # These persist state that OUTLIVES the turn (cron jobs,
                 # polling watchers) and can execute shell/python on a
                 # schedule — approval-eligible + serial. schedule_list /
                 # await_task are pure reads and stay parallel.
                 write_tools=frozenset({
                     'schedule_create', 'schedule_manage',
                     'timer_manage',
                 }),
                 idempotent_tools=frozenset({'schedule_list'}),
                 # Create/manage calls and the scheduler worker mutate this.
                 cacheable_tools=frozenset(),
                 unchanged_receipt_tools=frozenset({'schedule_list'}),
                 programmatic_tools=frozenset({'schedule_list'}),
                 discovery_policy='searchable',
                 search_hints={
                     'schedule_create': (
                         'create recurring reminder cron once: calendar clock '
                         'scheduled agent countdown daily weekly '
                         '创建定时任务 闹钟 循环 提醒 倒计时 计时器'),
                     'schedule_list': 'show scheduled jobs reminders 查看定时任务',
                     'schedule_manage': (
                         'cancel stop disable remove scheduled job reminder '
                         '取消提醒 停止定时任务'),
                     'timer_create': (
                         'wait until ready poll watcher condition satisfied '
                         'CI build download file exists '
                         '创建条件监视 条件满足 等到就绪 轮询 监视'),
                     'timer_manage': 'cancel stop timer 取消计时器',
                     'await_task': 'wait poll background task 等待后台任务',
                 },
                 category='scheduler', description='Scheduler / proactive agent tools',
                 gate='常开（有任意基础工具即挂载）'),
        ToolSpec('timer_watcher', _build_timer_watcher, phase='capability',
                 provides=frozenset({'timer_create'}),
                 direct_wire_tools=frozenset({'timer_create'}),
                 write_tools=frozenset({'timer_create'}),
                 cacheable_tools=frozenset(), discovery_policy='searchable',
                 category='scheduler', description='Durable readiness continuation',
                 gate='常开（有基础工具和持久存储即挂载）'),
        ToolSpec('tool_effect_inspection', _build_tool_effect_inspection, phase='capability',
                 provides=frozenset({'inspect_tool_effect'}),
                 idempotent_tools=frozenset({'inspect_tool_effect'}),
                 cacheable_tools=frozenset(),
                 programmatic_tools=frozenset({'inspect_tool_effect'}),
                 contracts=(TOOL_EFFECT_INSPECTION,),
                 discovery_policy='eager', catalog_active_only=True, script_safe=True,
                 timeout_ms_by_name={'inspect_tool_effect': 30_000},
                 category='general', description='Inspect interrupted file publication',
                 gate='会话持久存储可用',
                 search_hints={'inspect_tool_effect': 'recover unknown write outcome 核实写入 恢复未知操作'}),
        ToolSpec(
            'tool_result_artifacts', _build_tool_result_artifacts,
            phase='capability',
            provides=frozenset({'read_tool_artifact', 'search_tool_artifact'}),
            idempotent_tools=frozenset({
                'read_tool_artifact', 'search_tool_artifact'}),
            programmatic_tools=frozenset({
                'read_tool_artifact', 'search_tool_artifact'}),
            # A V2 envelope can instruct the model to continue only if the
            # continuation function is already callable on that same wire
            # turn. Keeping it behind Tool Search created a dead-end pointer:
            # models repeatedly reissued the source read because the recovery
            # tool named by the result was absent from their visible surface.
            discovery_policy='eager',
            script_safe=True,
            catalog_active_only=True,
            search_hints={
                'read_tool_artifact': 'continue large result range cursor 继续读取',
                'search_tool_artifact': 'search prior tool result 搜索工具结果',
            },
            category='artifacts',
            description='Bounded continuation for large tool results',
            gate='tools.resultEnvelope=v2',
            contracts=TOOL_RESULT_ARTIFACT_CONTRACTS,
        ),
        ToolSpec('working_facts', _build_working_facts, phase='capability',
                 provides=frozenset({'read_working_facts', 'record_working_fact'}),
                 write_tools=frozenset({'record_working_fact'}),
                 idempotent_tools=frozenset({'read_working_facts'}), cacheable_tools=frozenset(),
                 programmatic_tools=frozenset({'read_working_facts'}),
                 contracts=WORKING_FACT_CONTRACTS, discovery_policy='searchable', script_safe=True,
                 catalog_active_only=True, handler=_handle_working_facts,
                 category='memory', description='Conversation working facts with versioned sources',
                 gate='持久会话存储可用'),
        ToolSpec('swarm', _build_swarm, phase='capability',
                 compatibility_tools=frozenset({'await_agents'}),
                 contextual_tools=frozenset({'store_artifact', 'read_artifact', 'list_artifacts'}),
                 discovery_policy_by_name={name: 'searchable' for name in ('send_agent_message', 'get_agent_messages', 'set_approval_delegate')},
                 # provides lists every name this family has a handler for on
                 # the MAIN dispatch registry (@tool_registry.tool_set over
                 # SWARM_TOOL_NAMES), which is wider than what build() puts in
                 # the master schema:
                 #   * spawn_agents/get_agent_result — in the master schema
                 #   * await_agents — legacy dispatch only; poll owns waiting
                 #   * store/read/list_artifact(s)  — NOT in the master schema;
                 #     they are injected into SUB-AGENTS only
                 #     (SubAgent._inject_artifact_tools) and executed inside the
                 #     sub-agent's own loop. Declared anyway because the handler
                 #     IS reachable on the main registry, so an undeclared name
                 #     would be invisible to the partition tables and to the
                 #     custom-tool collision check in lib/tools/tool_env.py.
                 provides=frozenset({
                     'spawn_agents', 'await_agents', 'get_agent_result',
                     'send_agent_message', 'get_agent_messages',
                     'store_artifact', 'read_artifact', 'list_artifacts',
                     'set_approval_delegate',
                 }),
                 write_tools=frozenset({'send_agent_message'}),
                 search_hints={
                     'send_agent_message': 'send message running child subagent steer context 给子agent发消息 补充指令',
                     'get_agent_messages': 'child message input receipt delivery confirmation 子agent 消息 回执',
                 },
                 # store_artifact is deliberately NOT in write_tools. It writes
                 # to an in-process, per-run ArtifactStore (thread-safe under
                 # its own lock, TTL-expiring, lost on process exit) — it
                 # touches no filesystem, no network, and nothing that outlives
                 # the run. Approval-prompting it would be pure noise, and the
                 # serial-dispatch half of the partition buys nothing over the
                 # store's own lock. This is a deliberate departure from the
                 # "every state-changing tool is partitioned" rule, recorded
                 # here so it reads as a decision rather than an omission.
                 # set_approval_delegate is likewise NOT in write_tools: it
                 # registers a task-scoped adjudication policy in the task
                 # config (no filesystem/network side effect), and gating the
                 # approval-policy tool behind approval would be recursive.
                 idempotent_tools=frozenset({'list_artifacts'}),
                 # Sub-agents can publish artifacts between two list calls.
                 cacheable_tools=frozenset(),
                 unchanged_receipt_tools=frozenset({
                     'get_agent_result', 'list_artifacts',
                 }),
                 category='swarm', description='Async multi-agent swarm',
                 gate='常开（默认工具，无开关）'),
        ToolSpec('mcp', _build_mcp, phase='capability',
                 compatibility_tools=frozenset({'search_mcp_tools', 'call_mcp_read_tool', 'call_mcp_write_tool'}),
                 provides=frozenset({
                     'search_mcp_tools', 'call_mcp_read_tool',
                     'call_mcp_write_tool',
                 }),
                 write_tools=frozenset({'call_mcp_write_tool'}),
                 idempotent_tools=frozenset({'search_mcp_tools'}),
                 discovery_policy='searchable',
                 category='mcp', description='External MCP-server tools',
                 gate='设置 → MCP（默认开，需已连接服务器）'),
        # The discovery schema is injected at the final wire boundary
        # according to the resolved provider/search strategy, not during
        # ordinary registry assembly. Declaring it here keeps ownership,
        # idempotency and custom-tool collision checks authoritative.
        # ``execute_tools`` is declared for ownership/collision/admission and
        # is injected only alongside local Tool Search at the wire boundary;
        # nested calls remain bounded by the executable task catalog.
        ToolSpec('tool_gateway', lambda _ctx: [], phase='capability',
                 provides=GATEWAY_TOOL_NAMES,
                 idempotent_tools=frozenset({'search_tools'}),
                 category='tools',
                 description='Local tool discovery and stable execution gateway',
                 gate='工具搜索启用时出现'),
        ToolSpec('evolution', _build_evolution_teach, phase='capability',
                 discovery_policy='searchable',
                 direct_wire_tools=frozenset({'evolution_coordinate'}),
                 search_hints={
                     'evolution_explore': 'scientific experiment protocol preregister observations 科研 实验 预注册',
                     'evolution_material': 'research source library materials 文献 材料库',
                     'evolution_teach': 'propose verified reusable skill capability 教学 技能',
                     'evolution_reindex': 'refresh source index retrieval 重建索引',
                 },
                 provides=frozenset({'evolution_teach', 'evolution_material', 'evolution_coordinate', 'evolution_reindex', 'evolution_explore'}),
                 write_tools=frozenset({'evolution_teach', 'evolution_material', 'evolution_coordinate', 'evolution_reindex', 'evolution_explore'}),
                 category='evolution', description='Goal coordination, source library and verified capability proposals',
                 gate='目标绑定任务；审查阶段无写工具',
                 exposure_gate=lambda ctx: bool((ctx.cfg.get('evolution') or {}).get('goal_id')),
                 handler=_handle_evolution_teach),
        # ── unified async handles: append-only position before custom ──
        ToolSpec('background', _build_background, phase='capability',
                 provides=frozenset({'poll', 'kill'}),
                 write_tools=frozenset({'kill'}),
                 idempotent_tools=frozenset({'poll'}),
                 # Background state changes constantly; results are never
                 # cacheable across rounds.
                 cacheable_tools=frozenset(),
                 # Lifecycle control is never a discovery-only capability.
                 pin_on_exposure=True,
                 search_hints={
                     'poll': ('check wait background command agent task status '
                              'result 后台 命令 任务 状态 结果 等待'),
                     'kill': ('kill stop background command agent process '
                              '停止 终止 后台 进程 任务'),
                 },
                 category='background',
                 description='Unified async handles (poll / kill)',
                 gate='Durable runtime with base, shell, or swarm capability'),
        ToolSpec('retained_agents', _build_retained_agents,
                 phase='capability',
                 provides=frozenset({'list_agents', 'manage_agents'}),
                 write_tools=frozenset({'manage_agents'}),
                 discovery_policy='searchable',
                 search_hints={'list_agents': (
                     'show inspect persistent team agents memory roster '
                     '查看 团队 智能体 记忆'),
                     'manage_agents': (
                     'persistent reusable team agent colleague remember '
                     'create assign reset archive long-term subagent '
                     '常驻 长期 子智能体 团队 记忆 派工')},
                 category='swarm', description='Reusable team agents',
                 gate='Durable application runtime only',
                 exposure_gate=lambda ctx: ctx.durable_state_available),
        ToolSpec('evolution_artifact', _build_evolution_artifact,
                 phase='capability', provides=frozenset({'evolution_artifact'}),
                 direct_wire_tools=frozenset({'evolution_artifact'}),
                 write_tools=frozenset({'evolution_artifact'}), cacheable_tools=frozenset(),
                 category='evolution', description='Freeze explicitly declared project artifacts',
                 gate='当前项目执行者声明生成物快照，独立评审只读原文',
                 exposure_gate=lambda ctx: bool((ctx.cfg.get('evolution') or {}).get('run_id'))
                     and (ctx.cfg.get('evolution') or {}).get('stage') == 'executing'
                     and bool(ctx.cfg.get('projectEnabled')),
                 handler=_handle_evolution_teach),
        ToolSpec('evolution_review', _build_evolution_review,
                 phase='capability', provides=frozenset({'evolution_review'}),
                 direct_wire_tools=frozenset({'evolution_review'}), cacheable_tools=frozenset(),
                 category='evolution', description='Submit the independent frozen-source review',
                 gate='当前只读评审提交判断，原生校验仍决定验收',
                 exposure_gate=lambda ctx: bool((ctx.cfg.get('evolution') or {}).get('run_id'))
                     and (ctx.cfg.get('evolution') or {}).get('stage') == 'reviewing',
                 handler=_handle_evolution_teach),
        ToolSpec('evolution_source', _build_evolution_source,
                 phase='capability', provides=frozenset({'evolution_source'}),
                 idempotent_tools=frozenset({'evolution_source'}), cacheable_tools=frozenset(),
                 result_recovery_by_name={'evolution_source': 'source'},
                 model_input_observer=_observe_evolution_source_input,
                 category='evolution', description='Read immutable experiment originals',
                 gate='当前执行者或只读评审的归档原文',
                 exposure_gate=lambda ctx: bool((ctx.cfg.get('evolution') or {}).get('run_id'))
                     and (ctx.cfg.get('evolution') or {}).get('stage') in ('executing', 'reviewing'),
                 handler=_handle_evolution_teach),
        build_rich_spec(),
        # ── per-request custom tools (always last; handlers are task-local) ──
        ToolSpec('custom', _build_custom, phase='capability',
                 category='custom',
                 description='Per-request custom tools (handlers via task[_tool_env])',
                 gate='API /api/v1/agent/run 请求自带 tools 时出现'),
    ]
    return builtins


def _register_builtins() -> None:
    """Install the canonical built-in declarations once."""
    for spec in build_builtin_specs():
        register_tool_spec(spec)
