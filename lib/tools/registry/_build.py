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


def _build_search_settings(ctx: ToolContext) -> list[dict]:
    # update_search_settings tunes the pipeline web_search/fetch_url run on,
    # so its wire exposure rides the SAME gate. Registered as its own spec
    # appended at the END of the base phase: inserting it into the search spec
    # put it mid-list (position 2), which broke the cache-stable tool-order ratchet
    # (tests/test_tool_registry.py::TestOrdering).
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
    if ctx.project_enabled and ctx.multiroot_active:
        from lib.tools.project import with_multiroot_hint
        return with_multiroot_hint([READ_FILES_TOOL])
    return [READ_FILES_TOOL]


def _build_inspect_image(ctx: ToolContext) -> list[dict]:
    # inspect_image is ALWAYS on (like read_files) — it re-renders a region
    # of any local image at full resolution so the model can read detail the
    # initial downscale discarded. No project / vision toggle gates it; the
    # dispatch path drops the resulting image for text-only models anyway.
    from lib.tools.image_edit import INSPECT_IMAGE_TOOL
    if ctx.project_enabled and ctx.multiroot_active:
        from lib.tools.project import with_multiroot_hint
        return with_multiroot_hint([INSPECT_IMAGE_TOOL])
    return [INSPECT_IMAGE_TOOL]


def _build_project_result_meta(
    tool_name: str,
    tool_args: dict,
    tool_content: str,
) -> dict:
    """Lazy project-plugin adapter for the core result-metadata seam."""
    from lib.tools.meta import build_project_tool_meta
    return build_project_tool_meta(tool_name, tool_args, tool_content)


def _build_project_or_code_exec(ctx: ToolContext) -> list[dict]:
    # ``project_ready`` is evaluated from the current request, so attaching or
    # detaching Project Brain changes the next assembled tool surface directly.
    from lib.tools.code_exec import CODE_EXEC_TOOL
    from lib.tools.project import project_tools_for_runtime
    project_tools = project_tools_for_runtime()
    if ctx.project_ready:
        if ctx.project_remote:
            # RWA 拍板 3A:同名 schema + 本地执行提示;远程绑定是单一根,
            # multiroot 提示不适用(远程侧永远 root-relative)。
            from lib.tools.project import with_remote_hint
            logger.debug('[Task %s] 🌐 remote worktree bound — project tools '
                         'carry the local-execution hint', ctx.tid)
            return with_remote_hint(project_tools)
        if ctx.multiroot_active:
            from lib.tools.project import with_multiroot_hint
            return with_multiroot_hint(project_tools)
        return list(project_tools)
    return [CODE_EXEC_TOOL]


def _build_browser(ctx: ToolContext) -> list[dict]:
    from lib.browser.queue import get_connected_clients
    if (
        ctx.owner_user_id > 0
        and get_connected_clients(owner_user_id=str(ctx.owner_user_id))
    ):
        from lib.browser.advanced import ADVANCED_BROWSER_TOOLS
        from lib.tools.browser import BROWSER_TOOLS
        tools = list(BROWSER_TOOLS) + list(ADVANCED_BROWSER_TOOLS)
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


def _build_image_gen(ctx: ToolContext) -> list[dict]:
    from lib.tools.image_gen import GENERATE_IMAGE_TOOL
    logger.debug('[Task %s] 🎨 Image generation tool available', ctx.tid)
    return [GENERATE_IMAGE_TOOL]


def _build_motion_video(ctx: ToolContext) -> list[dict]:
    # Motion-video (MG animation) pipeline — gated on a project being
    # attached (the workdir convention lives under the project's .tofu/),
    # same gate as the project tool family.
    if not ctx.project_ready:
        return []
    from lib.tools.motion_video import MOTION_VIDEO_TOOLS
    logger.debug('[Task %s] Motion-video tools enabled (%d)',
                 ctx.tid, len(MOTION_VIDEO_TOOLS))
    return list(MOTION_VIDEO_TOOLS)


def _build_produce(ctx: ToolContext) -> list[dict]:
    # High-level "topic → finished video" tool. Deliberately NOT project-gated
    # (owner 拍板 #2: "say one sentence and get a film" cannot require an
    # attached project) — topic jobs render under the server data dir. Its
    # schema exposure rides the Web Search composer preference; execution does
    # not, because web_search itself remains available to the grounded recipe.
    from lib.tools.produce import (EDIT_SLIDES_TOOL, PRODUCE_REPORT_TOOL,
                                   PRODUCE_RESEARCH_TOOL, PRODUCE_SLIDES_TOOL,
                                   PRODUCE_VIDEO_TOOL)
    logger.debug('[Task %s] produce_video/produce_report/produce_research/'
                 'produce_slides tools enabled', ctx.tid)
    # Appended LAST so the existing video/report prefix stays byte-stable for
    # the prompt cache (the ordering contract in this module's docstring).
    return [PRODUCE_VIDEO_TOOL, PRODUCE_REPORT_TOOL, PRODUCE_RESEARCH_TOOL,
            PRODUCE_SLIDES_TOOL, EDIT_SLIDES_TOOL]


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
        return list(INTEGRATION_TOOLS)
    return []


def canonical_human_guidance_schema() -> dict:
    """Return the registry-owned canonical ``ask_human`` wire schema.

    Core callers use this registry seam instead of importing the concrete
    human-guidance plugin module directly.
    """
    from lib.tools.human_guidance import ASK_HUMAN_TOOL
    return ASK_HUMAN_TOOL


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
    tools = deepcopy(ALL_MEMORY_TOOLS)
    if not (ctx.project_enabled and ctx.project_path):
        # Memory remains useful in a non-project conversation, but a project
        # target is physically impossible there. The old static schema still
        # advertised project as the default, so a perfectly valid omitted
        # ``scope`` became a guaranteed ``project_path required`` tool error.
        # Constrain the model contract at assembly time; the handler keeps the
        # same context-sensitive default as a defensive boundary.
        for tool in tools:
            fn = tool.get('function') or {}
            if fn.get('name') not in ('create_memory', 'merge_memories'):
                continue
            scope = (((fn.get('parameters') or {}).get('properties') or {})
                     .get('scope'))
            if isinstance(scope, dict):
                scope['enum'] = ['global']
                scope['description'] = (
                    "Store in global memory. This conversation has no active "
                    "project, so project scope is unavailable. Default: global")
    return tools


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


def _build_local_serve(ctx: ToolContext) -> list[dict]:
    # Managed local deployment (install + run an inference engine for a
    # user-supplied model path) is a DEFAULT capability like the scheduler:
    # it attaches whenever any base tool exists. It is discoverable through
    # Tool Search rather than eagerly wired, so idle turns pay nothing.
    # TOFU_LOCAL_SERVE=0 is the retraction switch.
    if (not ctx.durable_state_available
            or ctx.lean or not ctx.has_base_tools):
        return []
    import os
    if os.environ.get('TOFU_LOCAL_SERVE', '1').strip().lower() in (
            '0', 'false', 'no', 'off'):
        return []
    from lib.local_serve.tool_defs import LOCAL_SERVE_TOOLS
    logger.debug('[Task %s] 🖥️ Local-serve tools enabled (%d tools)',
                 ctx.tid, len(LOCAL_SERVE_TOOLS))
    return list(LOCAL_SERVE_TOOLS)


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
    return list(SCHEDULER_TOOLS)


def _build_tool_result_artifacts(ctx: ToolContext) -> list[dict]:
    if not ctx.durable_state_available:
        return []
    from lib.context_experiment_flags import normalize_context_experiment_flags
    if normalize_context_experiment_flags(
            ctx.cfg)["tools"]["resultEnvelope"] != "v2":
        return []
    from lib.tools.tool_result_artifacts import build_tool_result_artifact_tools
    return build_tool_result_artifact_tools()


def _build_swarm(ctx: ToolContext) -> list[dict]:
    # NOT gated on has_base_tools — a bare-conversation research swarm is a
    # valid use case (mirrors the read_files decoupling).
    from lib.swarm.tools import (
        AWAIT_AGENTS_TOOL,
        GET_AGENT_RESULT_TOOL,
        SPAWN_AGENTS_TOOL,
    )
    logger.debug('[Task %s] 🐝 Async swarm enabled — spawn_agents / '
                 'await_agents / get_agent_result (project_enabled=%s)',
                 ctx.tid, ctx.project_enabled)
    return [SPAWN_AGENTS_TOOL, AWAIT_AGENTS_TOOL, GET_AGENT_RESULT_TOOL]


def _freeze_empty_mcp_wire(ctx: ToolContext) -> None:
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
    if exposure in ('progressive', 'wrapper', 'legacy', 'inline', 'all', 'full'):
        return
    try:
        from lib.mcp.tool_search import (
            freeze_wire_definitions,
            mcp_selection_scope_id,
        )
        scope = mcp_selection_scope_id(
            task_id=getattr(ctx, 'task_id', ''),
            conv_id=getattr(ctx, 'conv_id', ''),
            owner_user_id=getattr(ctx, 'owner_user_id', 0))
        ctx.cfg['_mcpSelectionScopeId'] = scope
        freeze_wire_definitions(scope, [])
    except Exception as exc:
        logger.debug('[Task %s] MCP empty-wire freeze failed: %s', ctx.tid, exc)

def _build_mcp(ctx: ToolContext) -> list[dict]:
    # Bridge to external MCP servers — schemas fetched dynamically at request
    # time.  Default: enabled.  Benchmarks may pass mcpEnabled=False.
    if not ctx.cfg.get('mcpEnabled', True):
        logger.debug('[Task %s] MCP disabled via mcpEnabled=false', ctx.tid)
        return []
    try:
        from lib.mcp import get_bridge
        bridge = get_bridge()
        if bridge.connected:
            mcp_tools = bridge.get_openai_tool_defs()
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

                    # Retrieval intent belongs to the latest user turn. System
                    # instructions and long project journals contain broad
                    # words such as "paper", "login", and "commit" that used
                    # to select unrelated MCP schemas before the actual task.
                    query = ''
                    for message in reversed(
                            getattr(ctx, 'messages', None) or []):
                        if (isinstance(message, dict)
                                and message.get('role') == 'user'):
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
                    except Exception as search_exc:
                        # Discovery is an optimization, never an availability
                        # gate. A corrupt index or unexpected metadata must
                        # fail open to the server-cached allowed catalog.
                        logger.warning('[Task %s] MCP pre-request search '
                                       'failed: %s; exposing full allowed '
                                       'catalog', ctx.tid, search_exc,
                                       exc_info=True)
                        active = list(mcp_tools)
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
    except Exception as e:
        logger.debug('[Task %s] MCP bridge not available: %s', ctx.tid, e)
    _freeze_empty_mcp_wire(ctx)
    return []


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


def _register_builtins() -> None:
    """Register the built-in tool specs in canonical (cache-stable) order."""
    from lib.tools.tool_result_artifacts import TOOL_RESULT_ARTIFACT_CONTRACTS

    builtins = [
        # ── base phase (counted toward has_real_tools) ──
        ToolSpec('search', _build_search, phase='base',
                 provides=frozenset({'web_search'}),
                 idempotent_tools=frozenset({'web_search'}),
                 category='search', description='Web search',
                 gate='输入框 → 搜索模式（联网/multi）',
                 exposure_gate=lambda ctx: ctx.search_mode in (
                     'single', 'multi')),
        ToolSpec('fetch', _build_fetch, phase='base',
                 provides=frozenset({'fetch_url'}),
                 idempotent_tools=frozenset({'fetch_url'}),
                 programmatic_tools=frozenset({'fetch_url'}),
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
                 provides=frozenset({
                     'grep_search', 'find_files',
                     'write_file', 'edit_file', 'apply_diff', 'apply_diffs',
                     'insert_content', 'insert_contents',
                     'run_command',
                 }),
                 write_tools=frozenset({
                     'write_file', 'edit_file', 'apply_diff', 'apply_diffs',
                     'insert_content', 'insert_contents',
                     'run_command',
                 }),
                 idempotent_tools=frozenset({
                     'grep_search', 'find_files',
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
                     'browser_research_page', 'browser_devtools',
                     'browser_close_tab', 'browser_click', 'browser_type',
                     'browser_press_key', 'browser_execute_js',
                     'browser_screenshot', 'browser_get_cookies',
                     'browser_get_history', 'browser_fill_form',
                     'browser_menu_click',
                 }),
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
                     'browser_research_page', 'browser_devtools',
                     'browser_fill_form', 'browser_menu_click',
                     'browser_close_tab',
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
        ToolSpec('image_gen', _build_image_gen, phase='base',
                 provides=frozenset({'generate_image'}),
                 discovery_policy='searchable',
                 search_hints={
                     'generate_image': (
                         'create edit image picture cover poster illustration '
                         '生成图片 编辑图片 封面图 海报 插画 配图'),
                 },
                 category='image', description='Image generation and editing',
                 gate='设置 → 显示 → 图像生成开关',
                 exposure_gate=lambda ctx: ctx.image_gen_enabled,
                 pin_on_exposure=True),
        ToolSpec('motion_video', _build_motion_video, phase='base',
                 provides=frozenset({
                     'motion_video_env_check', 'motion_video_storyboard_check',
                     'motion_video_check', 'motion_video_render',
                     'motion_video_probe', 'motion_video_concat',
                     'motion_video_narrate', 'motion_video_mux',
                 }),
                 write_tools=frozenset({
                     'motion_video_render', 'motion_video_concat',
                     'motion_video_narrate', 'motion_video_mux',
                 }),
                 idempotent_tools=frozenset({
                     'motion_video_env_check', 'motion_video_storyboard_check',
                     'motion_video_check', 'motion_video_probe',
                 }),
                 # These inspect mutable project files or host capabilities.
                 cacheable_tools=frozenset(),
                 unchanged_receipt_tools=frozenset({
                     'motion_video_env_check', 'motion_video_storyboard_check',
                     'motion_video_check', 'motion_video_probe',
                 }),
                 discovery_policy='searchable',
                 category='video',
                 description='Motion video (MG animation) generation',
                 gate='挂载项目后可用'),
        ToolSpec('produce', _build_produce, phase='base',
                 provides=frozenset({'produce_video', 'produce_report',
                                     'produce_research', 'produce_slides',
                                     'edit_slides'}),
                 discovery_policy='searchable',
                 search_hints={
                     'produce_video': (
                         'make finished video film clip short video '
                         '制作视频 生成视频 做个视频 科普视频 短视频 宣传片'),
                     'produce_report': 'write complete report 生成报告',
                     'produce_research': 'deep research study 深度研究',
                     'produce_slides': (
                         'deck presentation powerpoint ppt pptx slides keynote '
                         '演示文稿 幻灯片 课件 路演 做PPT'),
                     'edit_slides': 'revise presentation deck 编辑幻灯片',
                 },
                 category='video',
                 description=(
                     'High-level topic → finished video / editable slides / '
                     'report / research'),
                 gate='搜索开启后可用',
                 exposure_gate=lambda ctx: (
                     ctx.search_mode in ('single', 'multi')
                     or ctx.search_enabled)),
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
                 }),
                 write_tools=frozenset(),
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
                 exposure_gate=lambda ctx: ctx.search_mode in (
                     'single', 'multi')),
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
        # ── capability phase ──
        ToolSpec('memory', _build_memory, phase='capability',
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
                 search_hints={
                     'request_skill_install': (
                         'install verified catalog skill 安装技能'),
                 },
                 discovery_policy='searchable',
                 category='skills',
                 description='Verified skill catalog installation',
                 gate='有任意基础工具；始终需要真人确认'),
        ToolSpec('todo', _build_todo, phase='capability',
                 provides=frozenset({'todo_write'}),
                 category='task', description='Structured task checklist',
                 gate='常开（有任意基础工具即挂载）'),
        ToolSpec('local_serve', _build_local_serve, phase='capability',
                 provides=frozenset({
                     'local_serve_prepare', 'local_serve_deploy',
                     'local_serve_status', 'local_serve_list',
                     'local_serve_stop', 'local_serve_remove',
                 }),
                 # deploy spawns a background install+server; remove destroys
                 # the registration — both ALWAYS need a human click (receipt
                 # enforced again in the handler). stop kills a running server
                 # but is trivially reversible (deploy starts it again), so it
                 # is an ordinary write gated only in Manual mode.
                 write_tools=frozenset({
                     'local_serve_deploy', 'local_serve_stop',
                     'local_serve_remove',
                 }),
                 confirmation_tools=frozenset({
                     'local_serve_deploy', 'local_serve_remove',
                 }),
                 idempotent_tools=frozenset({
                     'local_serve_prepare', 'local_serve_list',
                 }),
                 # status reflects a live server; prepare re-probes hardware.
                 cacheable_tools=frozenset(),
                 unchanged_receipt_tools=frozenset({'local_serve_list'}),
                 programmatic_tools=frozenset({'local_serve_list'}),
                 discovery_policy='searchable',
                 search_hints={
                     'local_serve_prepare': (
                         'inspect local model path hardware plan deploy '
                         '本地模型 路径 检查 部署方案'),
                     'local_serve_deploy': (
                         'install engine start local model server vllm sglang '
                         'ollama llamacpp 部署 启动 本地模型 安装'),
                     'local_serve_status': (
                         'check deployment progress log 部署进度 状态 日志'),
                     'local_serve_list': 'list local deployments 本地部署 列表',
                     'local_serve_stop': (
                         'stop local model server 停止 本地服务'),
                     'local_serve_remove': (
                         'remove unregister local deployment 移除 注销 本地部署'),
                 },
                 category='local_serve',
                 description='Managed local model deployment '
                             '(vLLM/SGLang/Ollama/llama.cpp)',
                 gate='常开（有任意基础工具即挂载；TOFU_LOCAL_SERVE=0 摘除）'),
        ToolSpec('scheduler', _build_scheduler, phase='capability',
                 provides=frozenset({
                     'schedule_create', 'schedule_list', 'schedule_manage',
                     'timer_create', 'timer_manage', 'await_task',
                 }),
                 # These persist state that OUTLIVES the turn (cron jobs,
                 # polling watchers) and can execute shell/python on a
                 # schedule — approval-eligible + serial. schedule_list /
                 # await_task are pure reads and stay parallel.
                 write_tools=frozenset({
                     'schedule_create', 'schedule_manage',
                     'timer_create', 'timer_manage',
                 }),
                 idempotent_tools=frozenset({'schedule_list'}),
                 # Create/manage calls and the scheduler worker mutate this.
                 cacheable_tools=frozenset(),
                 unchanged_receipt_tools=frozenset({'schedule_list'}),
                 programmatic_tools=frozenset({'schedule_list'}),
                 discovery_policy='searchable',
                 search_hints={
                     'schedule_create': (
                         'create recurring reminder cron scheduled agent '
                         '创建定时任务 提醒'),
                     'schedule_list': 'show scheduled jobs reminders 查看定时任务',
                     'schedule_manage': (
                         'cancel stop disable remove scheduled job reminder '
                         '取消提醒 停止定时任务'),
                     'timer_create': 'countdown timer remind later 创建计时器',
                     'timer_manage': 'cancel stop timer 取消计时器',
                     'await_task': 'wait poll background task 等待后台任务',
                 },
                 category='scheduler', description='Scheduler / proactive agent tools',
                 gate='常开（有任意基础工具即挂载）'),
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
        ToolSpec('swarm', _build_swarm, phase='capability',
                 # provides lists every name this family has a handler for on
                 # the MAIN dispatch registry (@tool_registry.tool_set over
                 # SWARM_TOOL_NAMES), which is wider than what build() puts in
                 # the master schema:
                 #   * spawn/await/get_agent_result — in the master schema
                 #   * store/read/list_artifact(s)  — NOT in the master schema;
                 #     they are injected into SUB-AGENTS only
                 #     (SubAgent._inject_artifact_tools) and executed inside the
                 #     sub-agent's own loop. Declared anyway because the handler
                 #     IS reachable on the main registry, so an undeclared name
                 #     would be invisible to the partition tables and to the
                 #     custom-tool collision check in lib/tools/tool_env.py.
                 provides=frozenset({
                     'spawn_agents', 'await_agents', 'get_agent_result',
                     'store_artifact', 'read_artifact', 'list_artifacts',
                 }),
                 # store_artifact is deliberately NOT in write_tools. It writes
                 # to an in-process, per-run ArtifactStore (thread-safe under
                 # its own lock, TTL-expiring, lost on process exit) — it
                 # touches no filesystem, no network, and nothing that outlives
                 # the run. Approval-prompting it would be pure noise, and the
                 # serial-dispatch half of the partition buys nothing over the
                 # store's own lock. This is a deliberate departure from the
                 # "every state-changing tool is partitioned" rule, recorded
                 # here so it reads as a decision rather than an omission.
                 idempotent_tools=frozenset({'list_artifacts'}),
                 # Sub-agents can publish artifacts between two list calls.
                 cacheable_tools=frozenset(),
                 unchanged_receipt_tools=frozenset({
                     'get_agent_result', 'list_artifacts',
                 }),
                 category='swarm', description='Async multi-agent swarm',
                 gate='常开（默认工具，无开关）'),
        ToolSpec('mcp', _build_mcp, phase='capability',
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
                 provides=frozenset({'search_tools', 'execute_tools'}),
                 idempotent_tools=frozenset({'search_tools'}),
                 category='tools',
                 description='Local tool discovery and stable execution gateway',
                 gate='工具搜索启用时出现'),
        # ── per-request custom tools (always last; handlers are task-local) ──
        ToolSpec('custom', _build_custom, phase='capability',
                 category='custom',
                 description='Per-request custom tools (handlers via task[_tool_env])',
                 gate='API /api/v1/agent/run 请求自带 tools 时出现'),
    ]
    for spec in builtins:
        register_tool_spec(spec)
