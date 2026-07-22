"""lib/tools/registry/_build.py — Built-in tool-spec builders + registration.

Each ``_build_*`` reproduces exactly one legacy ``if feature: …`` branch
(including its logging + lazy imports), and :func:`_register_builtins` wires
them into the registry in the canonical, prompt-cache-stable order:

    search → fetch → read_files → inspect_image → project|code_exec →
    browser → desktop → image_gen → conv_ref → human_guidance →
    ⟨base/capability boundary⟩ → memory → todo → scheduler → swarm → mcp →
    custom (always last)

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
    # was removed). Only 'off' yields no search tool.
    from lib.tools import SEARCH_TOOL_MULTI
    if ctx.search_mode in ('single', 'multi'):
        return [SEARCH_TOOL_MULTI]
    return []


def _build_fetch(ctx: ToolContext) -> list[dict]:
    from lib.tools import FETCH_URL_TOOL
    if ctx.fetch_enabled or ctx.search_enabled:
        return [FETCH_URL_TOOL]
    return []


def _build_read_files(ctx: ToolContext) -> list[dict]:
    # read_files is ALWAYS on — handles project-relative AND absolute local
    # paths (images, PDFs, Office docs, text), so the model can read local
    # content even with no project attached.
    from lib.tools import READ_FILES_TOOL
    if ctx.project_enabled and ctx.multiroot_active:
        from lib.tools.project import with_multiroot_hint
        return with_multiroot_hint([READ_FILES_TOOL])
    return [READ_FILES_TOOL]


def _build_inspect_image(ctx: ToolContext) -> list[dict]:
    # inspect_image is ALWAYS on (like read_files) — it re-renders a region
    # of any local image at full resolution so the model can read detail the
    # initial downscale discarded. No project / vision toggle gates it; the
    # dispatch path drops the resulting image for text-only models anyway.
    from lib.tools import INSPECT_IMAGE_TOOL
    if ctx.project_enabled and ctx.multiroot_active:
        from lib.tools.project import with_multiroot_hint
        return with_multiroot_hint([INSPECT_IMAGE_TOOL])
    return [INSPECT_IMAGE_TOOL]


def _build_project_or_code_exec(ctx: ToolContext) -> list[dict]:
    # ``project_ready`` (not the raw ``project_enabled``) so that attaching a
    # project mid-conversation clears the tool-schema latch on the OFF→ON
    # transition — otherwise a conversation whose first turn had no project
    # would freeze a no-project snapshot and never regain run_command / the
    # write tools even after a project is attached. See ToolContext.project_ready.
    from lib.tools import CODE_EXEC_TOOL, PROJECT_TOOLS
    if ctx.project_ready:
        if ctx.multiroot_active:
            from lib.tools.project import with_multiroot_hint
            return with_multiroot_hint(PROJECT_TOOLS)
        return list(PROJECT_TOOLS)
    if ctx.code_exec_enabled:
        return [CODE_EXEC_TOOL]
    return []


def _build_browser(ctx: ToolContext) -> list[dict]:
    if not ctx.browser_enabled:
        return []
    from lib.browser import is_extension_connected
    if is_extension_connected():
        from lib.browser.advanced import ADVANCED_BROWSER_TOOLS
        from lib.tools import BROWSER_TOOLS
        tools = list(BROWSER_TOOLS) + list(ADVANCED_BROWSER_TOOLS)
        logger.debug('[Task %s] Browser extension connected — browser tools '
                     'enabled (%d tools)', ctx.tid, len(tools))
        return tools
    logger.warning('[Task %s] Browser requested but extension not connected',
                   ctx.tid)
    return []


def _build_desktop(ctx: ToolContext) -> list[dict]:
    if not ctx.desktop_enabled:
        return []
    from lib.desktop import is_desktop_agent_connected
    if is_desktop_agent_connected():
        from lib.desktop_tools import DESKTOP_TOOLS
        logger.debug('[Task %s] 🖥️ Desktop agent connected — %d desktop tools '
                     'enabled', ctx.tid, len(DESKTOP_TOOLS))
        return list(DESKTOP_TOOLS)
    logger.warning('[Task %s] Desktop requested but agent not connected',
                   ctx.tid)
    return []


def _build_image_gen(ctx: ToolContext) -> list[dict]:
    if not ctx.image_gen_enabled:
        return []
    from lib.tools.image_gen import GENERATE_IMAGE_TOOL
    logger.debug('[Task %s] 🎨 Image generation tool enabled', ctx.tid)
    return [GENERATE_IMAGE_TOOL]


def _build_conv_ref(ctx: ToolContext) -> list[dict]:
    # CONV_REF_TOOLS = [list_conversations, get_conversation] — BOTH are
    # read-only (discover siblings + open one). Register them in two cases:
    #   (a) the user @-mentioned a conversation (the classic explicit path), OR
    #   (b) we're in project mode — the always-on cross-conv digest
    #       (system_context.py ★4.4) names sibling conversations for ambient
    #       awareness, so the model must be ABLE to open a surfaced sibling
    #       rather than being told about phantom tools. Gating only on
    #       has_conv_ref meant the digest header advertised tools absent from
    #       the schema on a plain project turn (the conv_tools_available
    #       branch). Registering them in project mode closes that gap.
    # Both branches require at least one base tool (current_count > 0): with no
    # tools at all there's no schema to extend.
    if ctx.current_count <= 0:
        return []
    if ctx.has_conv_ref or (ctx.project_enabled and ctx.project_path):
        from lib.tools import CONV_REF_TOOLS
        logger.debug('[Task %s] 💬 conv_ref tools enabled (has_conv_ref=%s '
                     'project=%s)', ctx.tid, ctx.has_conv_ref,
                     bool(ctx.project_enabled and ctx.project_path))
        tools = list(CONV_REF_TOOLS)
        # Project Charter tools (Pillar #2): the shared north star. Only in
        # project mode (a charter is per-project) — read + propose. Commit is
        # human-gated and is NEVER exposed as an agent tool.
        if ctx.project_enabled and ctx.project_path:
            from lib.tools import BOARD_TOOLS, CHARTER_TOOLS, PEER_TOOLS
            tools += list(CHARTER_TOOLS)
            # Project Board tools (Pillar #3): the coordination board — the
            # mechanism that makes conversations auto-coordinate (claim/avoid
            # duplicating). Project-scoped, same gate.
            tools += list(BOARD_TOOLS)
            # Project Peer tools (Pillar #6): cross-conversation communication
            # — live peer status + advisory messaging + advisory/gated
            # intervention. Same project gate; registered on every project turn
            # so the model can coordinate without the phantom-tool trap.
            tools += list(PEER_TOOLS)
        return tools
    return []


def _build_human_guidance(ctx: ToolContext) -> list[dict]:
    if ctx.human_guidance_enabled and ctx.current_count > 0:
        from lib.tools.human_guidance import ASK_HUMAN_TOOL
        logger.info('[Task %s] 🙋 Human guidance (ask_human) tool enabled',
                    ctx.tid)
        return [ASK_HUMAN_TOOL]
    if ctx.human_guidance_enabled:
        logger.debug('[Task %s] 🙋 Human guidance requested but no base tools '
                     '— skipped', ctx.tid)
    return []


def _build_memory(ctx: ToolContext) -> list[dict]:
    # Memory tools attach whenever ANY real tool exists.  Note: this is gated
    # on has_base_tools, NOT on memoryEnabled — the memoryEnabled flag only
    # controls the system-prompt memory instructions (see system_context.py).
    # ``ctx.lean`` is a retained seam (chat_mode.is_lean_mode, currently always
    # False after the air/pro merge) for a future auto-retract-tools feature
    # that would ship only the base search/fetch/read tools on a simple turn.
    if ctx.lean or not ctx.has_base_tools:
        return []
    from lib.memory import ALL_MEMORY_TOOLS
    return list(ALL_MEMORY_TOOLS)


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
    if ctx.lean or not ctx.has_base_tools:
        return []
    from lib.scheduler.tool_defs import SCHEDULER_TOOLS
    logger.debug('[Task %s] ⏰ Scheduler tools enabled (%d tools)',
                 ctx.tid, len(SCHEDULER_TOOLS))
    return list(SCHEDULER_TOOLS)


def _build_swarm(ctx: ToolContext) -> list[dict]:
    # NOT gated on has_base_tools — a bare-conversation research swarm is a
    # valid use case (mirrors the read_files decoupling).
    if not ctx.swarm_enabled:
        return []
    from lib.swarm.tools import (
        AWAIT_AGENTS_TOOL,
        GET_AGENT_RESULT_TOOL,
        SPAWN_AGENTS_TOOL,
    )
    logger.debug('[Task %s] 🐝 Async swarm enabled — spawn_agents / '
                 'await_agents / get_agent_result (project_enabled=%s)',
                 ctx.tid, ctx.project_enabled)
    return [SPAWN_AGENTS_TOOL, AWAIT_AGENTS_TOOL, GET_AGENT_RESULT_TOOL]


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
                logger.info('[Task %s] 🔌 MCP tools loaded: %d from %d servers',
                            ctx.tid, len(mcp_tools), bridge.server_count)
                return list(mcp_tools)
    except Exception as e:
        logger.debug('[Task %s] MCP bridge not available: %s', ctx.tid, e)
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
    builtins = [
        # ── base phase (counted toward has_real_tools) ──
        ToolSpec('search', _build_search, phase='base',
                 provides=frozenset({'web_search'}),
                 idempotent_tools=frozenset({'web_search'}),
                 category='search', description='Web search'),
        ToolSpec('fetch', _build_fetch, phase='base',
                 provides=frozenset({'fetch_url'}),
                 idempotent_tools=frozenset({'fetch_url'}),
                 category='search', description='Fetch a URL'),
        ToolSpec('read_files', _build_read_files, phase='base',
                 provides=frozenset({'read_files'}),
                 idempotent_tools=frozenset({'read_files'}),
                 category='project', description='Read local files'),
        ToolSpec('inspect_image', _build_inspect_image, phase='base',
                 provides=frozenset({'inspect_image'}),
                 idempotent_tools=frozenset({'inspect_image'}),
                 category='project', description='Zoom/rotate/crop image viewer'),
        ToolSpec('project', _build_project_or_code_exec, phase='base',
                 provides=frozenset({
                     'list_dir', 'grep_search', 'find_files',
                     'write_file', 'apply_diff', 'apply_diffs',
                     'insert_content', 'insert_contents',
                     'create_project', 'run_command',
                 }),
                 write_tools=frozenset({
                     'write_file', 'apply_diff', 'apply_diffs',
                     'insert_content', 'insert_contents',
                     'create_project', 'run_command',
                 }),
                 idempotent_tools=frozenset({
                     'list_dir', 'grep_search', 'find_files',
                 }),
                 category='project', description='Project file tools / code exec'),
        ToolSpec('browser', _build_browser, phase='base',
                 category='browser', description='Browser automation tools'),
        ToolSpec('desktop', _build_desktop, phase='base',
                 category='desktop', description='Desktop agent tools'),
        ToolSpec('image_gen', _build_image_gen, phase='base',
                 provides=frozenset({'generate_image'}),
                 category='image', description='Image generation'),
        ToolSpec('conv_ref', _build_conv_ref, phase='base',
                 provides=frozenset({'list_conversations', 'get_conversation',
                                     'project_charter_read', 'project_charter_propose',
                                     'project_board_read', 'project_board_post',
                                     'project_board_claim', 'project_board_complete',
                                     'project_board_block',
                                     'project_peer_status', 'project_feed_read',
                                     'project_message', 'project_intervene'}),
                 idempotent_tools=frozenset({'list_conversations', 'get_conversation',
                                             'project_charter_read', 'project_board_read',
                                             'project_peer_status', 'project_feed_read'}),
                 category='conversation', description='Conversation reference tools'),
        ToolSpec('human_guidance', _build_human_guidance, phase='base',
                 provides=frozenset({'ask_human'}),
                 category='human', description='Ask the human for guidance'),
        # ── capability phase ──
        ToolSpec('memory', _build_memory, phase='capability',
                 write_tools=frozenset({
                     'create_memory', 'update_memory',
                     'delete_memory', 'merge_memories',
                 }),
                 category='memory', description='Memory CRUD tools'),
        ToolSpec('todo', _build_todo, phase='capability',
                 provides=frozenset({'todo_write'}),
                 category='task', description='Structured task checklist'),
        ToolSpec('scheduler', _build_scheduler, phase='capability',
                 category='scheduler', description='Scheduler / proactive agent tools'),
        ToolSpec('swarm', _build_swarm, phase='capability',
                 provides=frozenset({
                     'spawn_agents', 'await_agents', 'get_agent_result',
                 }),
                 category='swarm', description='Async multi-agent swarm'),
        ToolSpec('mcp', _build_mcp, phase='capability',
                 category='mcp', description='External MCP-server tools'),
        # ── per-request custom tools (always last; handlers are task-local) ──
        ToolSpec('custom', _build_custom, phase='capability',
                 category='custom',
                 description='Per-request custom tools (handlers via task[_tool_env])'),
    ]
    for spec in builtins:
        register_tool_spec(spec)
