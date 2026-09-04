# HOT_PATH
"""Model configuration resolution, tool list assembly, and search addendum generation.

Extracted from orchestrator.py to reduce file size and isolate concerns.
"""


from lib.log import get_logger

logger = get_logger(__name__)

import re

import lib as _lib  # module ref for hot-reload (Settings changes take effect without restart)
from lib.tools.registry import (
    ToolContext, all_specs, assemble_tool_list, resolve_enabled_plugins,
)


_ULTRATHINK_RE = re.compile(r'\bultrathink\b', re.IGNORECASE)


def _has_ultrathink_keyword(text: str) -> bool:
    """Check if text contains the 'ultrathink' keyword (case-insensitive).

    Inspired by Claude Code's ``hasUltrathinkKeyword()`` in ``thinking.ts``.
    When detected, the orchestrator auto-escalates thinking_depth to 'max'.
    """
    return bool(_ULTRATHINK_RE.search(text))


def _extract_latest_user_text(cfg) -> str:
    """Extract the text of the most recent user message from the task config.

    The task config contains a 'messages' list from the frontend.
    Returns empty string if no user message is found.
    """
    messages = cfg.get('messages', [])
    if not messages:
        return ''
    # Walk backwards to find the last user message
    for msg in reversed(messages):
        if msg.get('role') == 'user':
            content = msg.get('content', '')
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                # Multimodal: extract text parts
                parts = [
                    b.get('text', '')
                    for b in content
                    if isinstance(b, dict) and b.get('type') == 'text'
                ]
                return ' '.join(parts)
    return ''


def _resolve_model_config(cfg, task_id):
    """Resolve model and features from the task config.

    The frontend now sends the actual model_id directly (no preset→model
    mapping).  Legacy preset values (qwen, gemini, doubao, etc.) are still
    supported for backward compatibility with old conversations.

    Returns a dict with keys: model, thinking_enabled, thinking_depth, preset,
    max_tokens, temperature, search_mode, search_enabled, fetch_enabled,
    project_path, project_enabled, code_exec_enabled, memory_enabled,
    browser_enabled, desktop_enabled.
    """
    tid = task_id[:8]
    # ── Two-tier chat mode (chat/studio) → atomic flags ──
    #   Single source of truth: lib/tasks_pkg/chat_mode. When the request
    #   declares a tier, its derived flags OVERRIDE the atomic flags below so
    #   the UI dial and the resolved tool set can never disagree; absent a
    #   tier this is a byte-identical pass-through (legacy / headless callers).
    from lib.tasks_pkg.chat_mode import apply_chat_mode, is_lean_mode, normalize_chat_mode
    _chat_mode = normalize_chat_mode(cfg)
    if _chat_mode is not None:
        cfg = apply_chat_mode(cfg)
    model = cfg.get('model', _lib.LLM_MODEL)
    # ``.get(k, default)`` only substitutes when the key is ABSENT — a config
    # that carries maxTokens=None (e.g. resolve_conv_config with no
    # server_defaults, the killed-turn recovery path) would pass None straight
    # through to build_body → _clamp_max_tokens → ``min(None, int)`` raises
    # "'<' not supported between instances of 'int' and 'NoneType'" and the
    # whole turn FATALs. Coerce a missing/None/invalid value to the default.
    max_tokens = cfg.get('maxTokens')
    if not isinstance(max_tokens, int) or max_tokens <= 0:
        max_tokens = 128000
    temperature = cfg.get('temperature', 1.0)
    thinking_enabled = cfg.get('thinkingEnabled', False)
    search_mode = cfg.get('searchMode', 'multi')
    response_format = cfg.get('responseFormat')
    thinking_depth = cfg.get('thinkingDepth', None)
    _default_depth = cfg.get('defaultThinkingDepth', 'off')  # user-configured default

    # ── Legacy preset backward-compat: if 'preset' is a known brand key,
    #    resolve it to a model_id for old conversations / Feishu / debug scripts.
    preset = cfg.get('preset') or cfg.get('effort', '')
    _LEGACY_PRESET_MAP = {
        'low':          _lib.QWEN_MODEL or 'qwen3.6-plus',
        'qwen':         _lib.QWEN_MODEL or 'qwen3.6-plus',
        'gemini':       _lib.GEMINI_MODEL,
        'gemini_flash': _lib.GEMINI_FLASH_PREVIEW_MODEL,
        'minimax':      _lib.MINIMAX_MODEL,
        'doubao':       _lib.DOUBAO_MODEL,
    }
    if preset in _LEGACY_PRESET_MAP:
        resolved = _LEGACY_PRESET_MAP[preset]
        if resolved:  # skip if the env-var model is not configured (empty)
            model = resolved
        thinking_enabled = True
        logger.debug('[Task %s] legacy preset=%s → model=%s', tid, preset, model)
    elif preset in ('opus', 'medium', 'high', 'xhigh', 'max'):
        thinking_enabled = True
        if preset in ('medium', 'high', 'xhigh', 'max'):
            thinking_depth = preset
        if not thinking_depth:
            thinking_depth = _default_depth
        logger.debug('[Task %s] legacy preset=opus, depth=%s → model=%s', tid, thinking_depth, model)
    else:
        # New path: preset IS the model_id (sent directly from frontend)
        if preset:
            model = preset
        thinking_enabled = cfg.get('thinkingEnabled', True)
        logger.debug('[Task %s] model=%s (direct), thinking=%s, depth=%s', tid, model, thinking_enabled, thinking_depth)

    # Normalize preset to actual model for downstream use
    preset = model

    # ── Effort / ultrathink keyword detection (inspired by Claude Code) ──
    # If the user's latest message contains "ultrathink", auto-escalate
    # thinking_depth to 'max' and ensure thinking is enabled.
    _user_text = _extract_latest_user_text(cfg)
    if _user_text and _has_ultrathink_keyword(_user_text):
        thinking_enabled = True
        thinking_depth = 'max'
        logger.info('[Task %s] 🧠 Ultrathink keyword detected — escalating to max depth',
                    tid)

    search_enabled = search_mode in ('single', 'multi')
    # fetch_url is normally always on (no user-facing toggle). Benchmarks/tests
    # may pass fetchEnabled=False to strip all network tools — honored here.
    fetch_enabled = cfg.get('fetchEnabled', True)

    project_path = cfg.get('projectPath', '')
    project_enabled = bool(project_path)
    if not project_enabled:
        # RWA: a remote-worktree binding (cfg['project_remote'], 总闸
        # TOFU_REMOTE_WORKTREE) implies the project tool set even though no
        # server-side projectPath exists — paths are resolved agent-side.
        from lib.desktop.remote import remote_worktree_binding
        project_enabled = remote_worktree_binding(cfg) is not None
    code_exec_enabled = cfg.get('codeExecEnabled', False)
    memory_enabled = cfg.get('memoryEnabled', True)
    browser_enabled = cfg.get('browserEnabled', False)
    desktop_enabled = cfg.get('desktopEnabled', False)
    image_gen_enabled = cfg.get('imageGenEnabled', False)
    human_guidance_enabled = cfg.get('humanGuidanceEnabled', False)
    scheduler_enabled = cfg.get('schedulerEnabled', False)
    lean = is_lean_mode(_chat_mode)
    # ── Plan Mode (orthogonal toggle, composes with the chat/studio dial) ──
    # Read-only collaborative planning à la Codex plan.md. Resolution lives
    # in lib/tasks_pkg/plan_mode; enforcement is assembly filter (below) +
    # dispatch rejection lane + prompt contract.
    from lib.tasks_pkg.plan_mode import plan_mode_enabled
    plan_mode = plan_mode_enabled(cfg)
    if plan_mode:
        # Within-turn clarification is intrinsic to Plan Mode rather than a
        # second feature toggle the user must discover first.
        human_guidance_enabled = True
    return {
        'model': model,
        'chat_mode': _chat_mode,
        'plan_mode': plan_mode,
        'lean': lean,
        'thinking_enabled': thinking_enabled,
        'thinking_depth': thinking_depth,
        'preset': preset,
        'max_tokens': max_tokens,
        'temperature': temperature,
        'response_format': response_format,
        'search_mode': search_mode,
        'search_enabled': search_enabled,
        'fetch_enabled': fetch_enabled,
        'project_path': project_path,
        'project_enabled': project_enabled,
        'code_exec_enabled': code_exec_enabled,
        'memory_enabled': memory_enabled,
        'browser_enabled': browser_enabled,
        'desktop_enabled': desktop_enabled,
        'image_gen_enabled': image_gen_enabled,
        'human_guidance_enabled': human_guidance_enabled,
        'scheduler_enabled': scheduler_enabled,
    }


def _assemble_tool_list(cfg, project_path, project_enabled, task_id,
                         search_mode, search_enabled, fetch_enabled,
                         code_exec_enabled, browser_enabled, desktop_enabled,
                         image_gen_enabled=False,
                         human_guidance_enabled=False, scheduler_enabled=False,
                         messages=None, conv_id=''):
    """Build the tool_list based on enabled features.

    Returns ``(tool_list, has_real_tools)`` where ``tool_list`` may be ``None``
    if no tools are enabled. Tool availability is not coupled to a round
    budget: a model keeps the same tool surface until it naturally stops.

    **Caller-supplied tools take precedence.** When
    ``cfg['_explicitToolSchemas']`` or ``cfg['tools']`` is a non-empty list
    (set by the embedded runtime or OpenAI/Anthropic compat adapters), the
    auto-derived feature toggles (search/fetch/memory/etc.) are ignored.
    Plan Mode is the deliberate exception: it fail-closes unproven schemas and
    always supplies the canonical ``ask_human`` protocol required for
    within-turn clarification.
    """
    tid = task_id[:8]
    from lib.tasks_pkg.plan_mode import (
        plan_mode_enabled, plan_mode_filter_tool_schemas,
    )
    _plan_mode = plan_mode_enabled(cfg)
    if _plan_mode:
        # Keep this invariant at the assembly boundary as well as model-config
        # resolution so direct/headless assembly callers cannot lose the
        # within-turn interaction tool by skipping the resolver.
        human_guidance_enabled = True
    explicit_tools = cfg.get('_explicitToolSchemas')
    if not isinstance(explicit_tools, list) or not explicit_tools:
        explicit_tools = cfg.get('tools')
    if isinstance(explicit_tools, list) and explicit_tools:
        # Validate shape — each tool must be an OpenAI-style
        # {type:'function', function:{name,description,parameters}}.
        ok = []
        for i, t in enumerate(explicit_tools):
            if isinstance(t, dict) and (t.get('function') or t.get('type') == 'function'):
                ok.append(t)
            else:
                logger.warning('[Task %s] dropping malformed tool[%d]: %r',
                               tid, i, t)
        if ok:
            def _schema_name(tool):
                fn = tool.get('function') or {}
                return str((fn.get('name') if isinstance(fn, dict) else '')
                           or tool.get('name') or '')

            caller_names = {_schema_name(tool) for tool in ok
                            if _schema_name(tool)}
            if _plan_mode:
                from lib.tasks_pkg.tool_dispatch._flags import _registry_tool_flags
                ok, dropped = plan_mode_filter_tool_schemas(
                    ok, _registry_tool_flags()[0])
                if dropped:
                    logger.info(
                        '[Task %s] Plan Mode — dropped %d unproven '
                        'caller-supplied tool schema(s): %s',
                        tid, len(dropped), ', '.join(sorted(dropped)),
                    )
                # ``ask_human`` has framework-owned wait/resume semantics. Use
                # its canonical schema even when the caller supplied a shadow
                # definition, and add it when explicit-tool precedence would
                # otherwise suppress automatic feature assembly.
                from copy import deepcopy
                from lib.tools.registry import canonical_human_guidance_schema
                ok = [tool for tool in ok if _schema_name(tool) != 'ask_human']
                ok.append(deepcopy(canonical_human_guidance_schema()))

            # Build every downstream authority map from the final, filtered
            # catalog. Dropped caller names must not survive as Tool Search or
            # dispatch metadata.
            final_names = [_schema_name(tool) for tool in ok
                           if _schema_name(tool)]
            cfg['_frontendSelectedToolNames'] = sorted(
                caller_names & set(final_names))
            cfg['_toolNamespaceByName'] = {
                name: ('builtin' if name == 'ask_human' and _plan_mode
                       else 'custom')
                for name in final_names
            }
            cfg['_executableToolNamespaceByName'] = dict(
                cfg['_toolNamespaceByName'])
            cfg['_executableToolCatalog'] = list(ok)
            cfg['_toolDiscoveryPolicyByName'] = {
                name: 'eager' for name in final_names}
            cfg['_toolScriptSafeByName'] = {
                name: False for name in final_names}
            cfg['_executableToolSearchTextByName'] = {
                name: ('human guidance within the current turn'
                       if name == 'ask_human' and _plan_mode
                       else 'custom caller supplied tool')
                for name in final_names
            }
            logger.info('[Task %s] using %d explicit/Plan tool(s); '
                        'auto-derived tools disabled', tid, len(ok))
            return (ok or None), bool(ok)

    # ── Declarative assembly — the per-feature if-ladder now lives as
    #    self-describing ToolSpec objects in lib/tools/registry/.  Native
    #    tools AND third-party plugins (tofu.tools entry points) flow through
    #    the same registry, so adding/removing a tool needs ZERO edits here.
    #    The spec registration order reproduces the cache-stable layout the
    #    old ladder produced.
    # Third-party (tofu.tools entry-point) plugins are gated per request so a
    # plugin installed in a shared multi-tenant process can't leak its tool
    # schema into unrelated callers. Resolved from cfg['plugins'] →
    # TOFU_DEFAULT_TOOL_PLUGINS env → fail-closed (no plugins). See
    # lib/tools/registry/ "Plugin isolation" and docs/TOOL_PLUGINS.md.
    enabled_plugins = resolve_enabled_plugins(cfg)
    # ``lean`` is a retained seam (is_lean_mode, always False after the air/pro
    # merge) that would drop the always-on capability tools (memory/todo/
    # scheduler). Derived from cfg here so every _assemble_tool_list caller
    # (orchestrator, swarm rehydrate, Flow runner, tests) honors it with
    # no signature change — the chatMode key rides on cfg.
    from lib.tasks_pkg.chat_mode import is_lean_mode, normalize_chat_mode
    _lean = is_lean_mode(normalize_chat_mode(cfg))
    ctx = ToolContext(
        cfg=cfg, task_id=task_id, lean=_lean,
        project_path=project_path, project_enabled=project_enabled,
        search_mode=search_mode, search_enabled=search_enabled,
        fetch_enabled=fetch_enabled, code_exec_enabled=code_exec_enabled,
        browser_enabled=browser_enabled, desktop_enabled=desktop_enabled,
        image_gen_enabled=image_gen_enabled,
        human_guidance_enabled=human_guidance_enabled,
        scheduler_enabled=scheduler_enabled, messages=messages,
        enabled_plugins=enabled_plugins, conv_id=conv_id,
        owner_user_id=int(
            cfg.get('userId') or cfg.get('_turnOwnerUserId') or 0),
    )
    tool_list, has_real_tools = assemble_tool_list(ctx)

    # ── Plan Mode wire filter (guidance layer, NOT the authority) ──
    # Drop mutating schemas from both initial exposure and Tool Search's
    # executable catalog so the model is neither tempted nor charged the
    # tokens. Dispatch still repeats the exact-call check as the final
    # authority for late-discovered or malformed calls.
    if _plan_mode and tool_list:
        from lib.tasks_pkg.tool_dispatch._flags import _registry_tool_flags
        _write_names = _registry_tool_flags()[0]
        _kept, _dropped = plan_mode_filter_tool_schemas(
            tool_list, _write_names)
        # Tool Search reads the executable catalog rather than the initially
        # exposed wire list. Apply the same policy to that authority snapshot.
        _catalog, _catalog_dropped = plan_mode_filter_tool_schemas(
            ctx.executable_tool_catalog, _write_names)
        ctx.executable_tool_catalog = _catalog
        _dropped = list(dict.fromkeys([*_dropped, *_catalog_dropped]))
        if _dropped:
            logger.info('[Task %s] 🗺️ Plan Mode — dropped %d mutating tool '
                        'schema(s) from wire: %s',
                        tid, len(_dropped), ', '.join(sorted(_dropped)))
        tool_list = _kept
    # Preserve the complete task-level execution authority before any routed
    # exposure is applied. Composer exposure toggles never grant authority.
    cfg['_executableToolCatalog'] = list(ctx.executable_tool_catalog)
    try:
        from lib.context_experiment_flags import (
            normalize_context_experiment_flags)
        cfg['_toolExecutionScope'] = normalize_context_experiment_flags(
            cfg)['tools']['executionScope']
    except Exception as exc:
        logger.debug('[ModelConfig] context experiment flags unavailable: %s', exc)
        cfg['_toolExecutionScope'] = 'available'
    cfg['_toolDiscoveryPolicyByName'] = dict(
        ctx.discovery_policy_by_name)
    cfg['_toolScriptSafeByName'] = dict(ctx.script_safe_by_name)
    cfg['_executableToolNamespaceByName'] = dict(ctx.tool_namespace_by_name)
    cfg['_executableToolSearchTextByName'] = dict(ctx.search_text_by_name)
    cfg['_toolContractDocumentsByName'] = dict(
        ctx.tool_contract_documents_by_name)

    # Request-local routing telemetry describes this assembly's live surface.
    try:
        from lib.context_experiment_flags import (
            normalize_context_experiment_flags)
        from lib.context_telemetry import stamp_tool_exposure
        _mode = normalize_context_experiment_flags(
            cfg)['tools']['nativeExposure']
        _available = sum(
            len(spec.provides) for spec in all_specs()
            if spec.source == 'builtin')
        _holder: dict = {}
        stamp_tool_exposure(
            _holder, mode=_mode, available=_available,
            exposed=len(tool_list),
            routed_keys=sorted(ctx.routed_spec_keys),
            omitted_keys=sorted(ctx.omitted_spec_keys))
        cfg['_toolExposureTelemetry'] = _holder['_toolExposureTelemetry']
    except Exception as _telemetry_exc:
        logger.debug('[Task %s] tool exposure telemetry skipped: %s',
                     tid, _telemetry_exc)

    # Preserve registry provenance across the later canonical-body boundary.
    # Every value below comes from this assembly, so tool toggles, MCP changes,
    # and Tool Search settings take effect on the next model round.
    _wire_names = set()
    for _tool in tool_list or ():
        if not isinstance(_tool, dict):
            continue
        _fn = _tool.get('function')
        _name = (_fn.get('name') if isinstance(_fn, dict) else '') \
            or _tool.get('name') or ''
        if _name:
            _wire_names.add(str(_name))
    _live_pins = ctx.frontend_selected_tool_names & _wire_names
    _live_namespaces = {
        name: namespace
        for name, namespace in ctx.tool_namespace_by_name.items()
        if name in _wire_names
    }
    _live_discovery_policy = {
        name: ctx.discovery_policy_by_name.get(name, 'eager')
        for name in _wire_names
    }
    _searchable_count = sum(
        ctx.discovery_policy_by_name.get(name, 'eager') == 'searchable'
        for name in {
            str(((tool.get('function') or {}).get('name') or ''))
            for tool in ctx.executable_tool_catalog if isinstance(tool, dict)
        }
        if name
    )
    from lib.context_experiment_flags import normalize_context_experiment_flags
    _live_tool_search_mode = normalize_context_experiment_flags(
        cfg)['tools']['toolSearch']
    cfg['_frontendSelectedToolNames'] = sorted(
        _live_pins & _wire_names)
    cfg['_toolNamespaceByName'] = {
        name: namespace
        for name, namespace in _live_namespaces.items()
        if name in _wire_names
    }
    cfg['_toolDiscoveryPolicyByName'] = _live_discovery_policy
    cfg['_toolSearchCatalogSize'] = len(ctx.executable_tool_catalog)
    cfg['_toolSearchableCount'] = _searchable_count
    cfg['_toolSearchMode'] = _live_tool_search_mode

    if not tool_list:
        tool_list = None

    return tool_list, has_real_tools
