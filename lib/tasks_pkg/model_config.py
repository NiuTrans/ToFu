# HOT_PATH
"""Model configuration resolution, tool list assembly, and search addendum generation.

Extracted from orchestrator.py to reduce file size and isolate concerns.
"""

from datetime import datetime, timezone

from lib.log import get_logger

logger = get_logger(__name__)

import re

import lib as _lib  # module ref for hot-reload (Settings changes take effect without restart)
from lib.tools import ToolContext, assemble_tool_list, resolve_enabled_plugins


def _build_search_addendum() -> str:
    """Build a minimal timestamp string so the model knows 'now'.

    Static search guidance lives in system_prompt_cc.section_using_tools
    and does NOT belong here — putting it here caused it to be injected into
    every user message on every round, bloating the conversation.
    """
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    return f'Current date and time: {now}'


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
    browser_enabled, desktop_enabled, swarm_enabled.
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
        # ★ New path: preset IS the model_id (sent directly from frontend)
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
    code_exec_enabled = cfg.get('codeExecEnabled', False)
    memory_enabled = cfg.get('memoryEnabled', True)
    browser_enabled = cfg.get('browserEnabled', False)
    desktop_enabled = cfg.get('desktopEnabled', False)
    swarm_enabled = cfg.get('swarmEnabled', False)
    image_gen_enabled = cfg.get('imageGenEnabled', False)
    human_guidance_enabled = cfg.get('humanGuidanceEnabled', False)
    scheduler_enabled = cfg.get('schedulerEnabled', False)
    lean = is_lean_mode(_chat_mode)
    return {
        'model': model,
        'chat_mode': _chat_mode,
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
        'swarm_enabled': swarm_enabled,
        'image_gen_enabled': image_gen_enabled,
        'human_guidance_enabled': human_guidance_enabled,
        'scheduler_enabled': scheduler_enabled,
    }


def _assemble_tool_list(cfg, project_path, project_enabled, task_id,
                         search_mode, search_enabled, fetch_enabled,
                         code_exec_enabled, browser_enabled, desktop_enabled,
                         swarm_enabled, image_gen_enabled=False,
                         human_guidance_enabled=False, scheduler_enabled=False,
                         messages=None, conv_id=''):
    """Build the tool_list based on enabled features.

    Returns (tool_list, has_real_tools, max_tool_rounds) where tool_list may be
    None if no tools are enabled.

    **Caller-supplied tools take precedence.** When ``cfg['tools']`` is a
    non-empty list (set by OpenAI/Anthropic compat adapters or by API
    callers passing ``tools=[...]`` to /api/v1/chat/completions), we
    use it verbatim — the auto-derived feature toggles
    (search/fetch/memory/etc.) are ignored. This is the contract
    documented in docs/COMPAT_OPENAI.md and COMPAT_ANTHROPIC.md.
    """
    tid = task_id[:8]
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
            logger.info('[Task %s] using %d caller-supplied tool(s); '
                        'auto-derived tools disabled', tid, len(ok))
            return ok, True, 999_999_999

    # ── Declarative assembly — the per-feature if-ladder now lives as
    #    self-describing ToolSpec objects in lib/tools/registry.py.  Native
    #    tools AND third-party plugins (tofu.tools entry points) flow through
    #    the same registry, so adding/removing a tool needs ZERO edits here.
    #    The spec registration order reproduces the cache-stable layout the
    #    old ladder produced.
    # Third-party (tofu.tools entry-point) plugins are gated per request so a
    # plugin installed in a shared multi-tenant process can't leak its tool
    # schema into unrelated callers. Resolved from cfg['plugins'] →
    # TOFU_DEFAULT_TOOL_PLUGINS env → fail-closed (no plugins). See
    # lib/tools/registry.py "Plugin isolation" and docs/TOOL_PLUGINS.md.
    enabled_plugins = resolve_enabled_plugins(cfg)
    # ``lean`` is a retained seam (is_lean_mode, always False after the air/pro
    # merge) that would drop the always-on capability tools (memory/todo/
    # scheduler). Derived from cfg here so every _assemble_tool_list caller
    # (orchestrator, swarm rehydrate, endpoint runner, tests) honors it with
    # no signature change — the chatMode key rides on cfg.
    from lib.tasks_pkg.chat_mode import is_lean_mode, normalize_chat_mode
    _lean = is_lean_mode(normalize_chat_mode(cfg))
    ctx = ToolContext(
        cfg=cfg, task_id=task_id, lean=_lean,
        project_path=project_path, project_enabled=project_enabled,
        search_mode=search_mode, search_enabled=search_enabled,
        fetch_enabled=fetch_enabled, code_exec_enabled=code_exec_enabled,
        browser_enabled=browser_enabled, desktop_enabled=desktop_enabled,
        swarm_enabled=swarm_enabled, image_gen_enabled=image_gen_enabled,
        human_guidance_enabled=human_guidance_enabled,
        scheduler_enabled=scheduler_enabled, messages=messages,
        enabled_plugins=enabled_plugins, conv_id=conv_id,
    )
    tool_list, has_real_tools = assemble_tool_list(ctx)

    # ── Per-conversation tool-schema latch (root fix for tools-array cache
    #    breaks). Freeze the EXACT tool list this conversation first used and
    #    serve it byte-identical on every later round, so a mid-conversation
    #    toggle (Swarm/Scheduler/Browser/…) cannot invalidate the cached
    #    prefix. The change is deferred to the next NEW conversation (or to an
    #    explicit "Apply now" that clears the latch). `diverged` signals the
    #    frontend that a pending change is being held. conv_id='' (stateless
    #    assembly / compat adapters) or TOFU_TOOLSET_LATCH=0 → no-op.
    from lib.tools import latch_tool_list, tool_list_diff
    tool_list, _toolset_diverged = latch_tool_list(conv_id, tool_list)
    if _toolset_diverged:
        _diff = tool_list_diff(conv_id)
        logger.info('[Task %s] 🔒 tool-schema latch held a pending toggle '
                    'change (conv=%s) added=%s removed=%s — deferring to next '
                    'conversation / Apply-now to keep prompt cache intact',
                    tid, conv_id[:8], _diff.get('added'), _diff.get('removed'))
        cfg['_toolsetDiff'] = _diff
    # Surface the flag so the orchestrator can attach it to the done event.
    cfg['_toolsetDiverged'] = bool(_toolset_diverged)

    if not tool_list:
        tool_list = None
        max_tool_rounds = 0
    else:
        # ★ NO tool round limit — the model decides when to stop.
        # ★ 禁止在此处添加任何轮数上限。模型自行判断何时停止。
        # ★ 任何形式的 max_tool_rounds 硬性限制、预算警告、重复检测强制停止
        #   都不允许加入。如果模型陷入循环，应从 prompt 质量层面解决，
        #   而不是在 orchestrator 里粗暴截断。
        max_tool_rounds = 999_999_999  # effectively unlimited

    return tool_list, has_real_tools, max_tool_rounds
