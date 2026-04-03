# HOT_PATH
"""Model configuration resolution, tool list assembly, and search addendum generation.

Extracted from orchestrator.py to reduce file size and isolate concerns.
"""

from datetime import datetime, timezone

from lib.log import get_logger

logger = get_logger(__name__)

import re

import lib as _lib  # module ref for hot-reload (Settings changes take effect without restart)
from lib.browser.advanced import ADVANCED_BROWSER_TOOLS
from lib.tools import (
    BROWSER_TOOL_NAMES,
    BROWSER_TOOLS,
    CODE_EXEC_TOOL,
    CODE_EXEC_TOOL_NAMES,
    CONV_REF_TOOL_NAMES,
    EMIT_TO_USER_TOOL,
    EMIT_TO_USER_TOOL_NAMES,
    ERROR_TRACKER_TOOL_NAMES,
    ERROR_TRACKER_TOOLS,
    FETCH_URL_TOOL,
    IMAGE_GEN_TOOL_NAMES,
    PROJECT_TOOL_NAMES,
    PROJECT_TOOL_READ_LOCAL_FILE,
    PROJECT_TOOLS,
    SEARCH_TOOL_MULTI,
    SEARCH_TOOL_SINGLE,
    build_project_tool_meta,
)


def _build_search_addendum() -> str:
    """Build a minimal timestamp string so the model knows 'now'.

    Static search guidance lives in _TOOL_USAGE_GUIDANCE (system_context.py)
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
    project_path, project_enabled, code_exec_enabled, skills_enabled,
    browser_enabled, desktop_enabled, swarm_enabled.
    """
    tid = task_id[:8]
    model = cfg.get('model', _lib.LLM_MODEL)
    max_tokens = cfg.get('maxTokens', 128000)
    temperature = cfg.get('temperature', 1.0)
    thinking_enabled = cfg.get('thinkingEnabled', False)
    search_mode = cfg.get('searchMode', 'multi')
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
    elif preset in ('opus', 'medium', 'high', 'max'):
        thinking_enabled = True
        if preset in ('medium', 'high', 'max'):
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
    fetch_enabled = True  # always on — no longer a user-facing toggle

    project_path = cfg.get('projectPath', '')
    project_enabled = bool(project_path)
    code_exec_enabled = cfg.get('codeExecEnabled', False)
    skills_enabled = cfg.get('skillsEnabled', True)
    browser_enabled = cfg.get('browserEnabled', False)
    desktop_enabled = cfg.get('desktopEnabled', False)
    swarm_enabled = cfg.get('swarmEnabled', False)
    image_gen_enabled = cfg.get('imageGenEnabled', False)
    human_guidance_enabled = cfg.get('humanGuidanceEnabled', False)
    scheduler_enabled = cfg.get('schedulerEnabled', False)
    return {
        'model': model,
        'thinking_enabled': thinking_enabled,
        'thinking_depth': thinking_depth,
        'preset': preset,
        'max_tokens': max_tokens,
        'temperature': temperature,
        'search_mode': search_mode,
        'search_enabled': search_enabled,
        'fetch_enabled': fetch_enabled,
        'project_path': project_path,
        'project_enabled': project_enabled,
        'code_exec_enabled': code_exec_enabled,
        'skills_enabled': skills_enabled,
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
                         messages=None):
    """Build the tool_list based on enabled features.

    Returns (tool_list, has_real_tools, max_tool_rounds) where tool_list may be
    None if no tools are enabled.
    """
    tid = task_id[:8]
    tool_list = []

    # ★ Search tools
    if search_mode == 'single':
        tool_list.append(SEARCH_TOOL_SINGLE)
    elif search_mode == 'multi':
        tool_list.append(SEARCH_TOOL_MULTI)
    if fetch_enabled or search_enabled:
        tool_list.append(FETCH_URL_TOOL)

    # ★ Project tools
    if project_enabled:
        tool_list.extend(t for t in PROJECT_TOOLS
                         if t is not PROJECT_TOOL_READ_LOCAL_FILE)
    elif code_exec_enabled:
        tool_list.append(CODE_EXEC_TOOL)

    # ★ Local file reader — always available (uses absolute paths, no project needed)
    tool_list.append(PROJECT_TOOL_READ_LOCAL_FILE)

    # ★ Browser extension tools
    if browser_enabled:
        from lib.browser import is_extension_connected
        if is_extension_connected():
            tool_list.extend(BROWSER_TOOLS)
            tool_list.extend(ADVANCED_BROWSER_TOOLS)
            logger.debug('[Task %s] Browser extension connected — browser tools enabled (%d tools)',
                         tid, len(BROWSER_TOOLS) + len(ADVANCED_BROWSER_TOOLS))
        else:
            logger.warning('[Task %s] Browser requested but extension not connected', tid)

    # ★ Desktop Agent tools
    if desktop_enabled:
        from routes.desktop import is_desktop_agent_connected
        if is_desktop_agent_connected():
            from lib.desktop_tools import DESKTOP_TOOLS
            tool_list.extend(DESKTOP_TOOLS)
            logger.debug('[Task %s] 🖥️ Desktop agent connected — %d desktop tools enabled', tid, len(DESKTOP_TOOLS))
        else:
            logger.warning('[Task %s] Desktop requested but agent not connected', tid)

    # ★ Image generation tool
    if image_gen_enabled:
        from lib.tools.image_gen import GENERATE_IMAGE_TOOL
        tool_list.append(GENERATE_IMAGE_TOOL)
        logger.debug('[Task %s] 🎨 Image generation tool enabled', tid)

    # ★ Error tracker tools — available when project mode is enabled
    if project_enabled:
        tool_list.extend(ERROR_TRACKER_TOOLS)
        logger.debug('[Task %s] 🔍 Error tracker tools enabled', tid)

    # ★ Conversation reference tools — only available when user @-mentions a conversation
    #   Detect by checking if any user message contains [REFERENCED_CONVERSATION
    from lib.tools import CONV_REF_TOOLS
    _has_conv_ref = False
    if messages:
        for _m in messages:
            _c = _m.get('content', '') if isinstance(_m.get('content'), str) else ''
            if '[REFERENCED_CONVERSATION' in _c:
                _has_conv_ref = True
                break
    if _has_conv_ref and len(tool_list) > 0:
        tool_list.extend(CONV_REF_TOOLS)
        logger.debug('[Task %s] 💬 Conversation @mention detected — conv_ref tools enabled', tid)

    # ★ Human Guidance tool — user opt-in via toggle
    if human_guidance_enabled and len(tool_list) > 0:
        from lib.tools.human_guidance import ASK_HUMAN_TOOL
        tool_list.append(ASK_HUMAN_TOOL)
        logger.info('[Task %s] 🙋 Human guidance (ask_human) tool enabled', tid)
    elif human_guidance_enabled:
        logger.debug('[Task %s] 🙋 Human guidance requested but no base tools — skipped', tid)

    # ★ Skills tool — only when other tools exist
    has_real_tools = len(tool_list) > 0
    if has_real_tools:
        from lib.skills import ALL_SKILL_TOOLS
        tool_list.extend(ALL_SKILL_TOOLS)

    # ★ emit_to_user — terminal tool to reference existing tool results
    #   instead of re-outputting them. Only useful when other tools exist.
    if has_real_tools:
        tool_list.append(EMIT_TO_USER_TOOL)

    # ★ Scheduler tools — proactive agent, cross-conv tasks, cron management
    if scheduler_enabled and has_real_tools:
        from lib.scheduler.tool_defs import SCHEDULER_TOOLS
        tool_list.extend(SCHEDULER_TOOLS)
        logger.debug('[Task %s] ⏰ Scheduler tools enabled (%d tools)', tid, len(SCHEDULER_TOOLS))

    # ★ Swarm tools — when project tools exist AND user toggled swarm on
    if swarm_enabled and project_enabled:
        from lib.swarm.tools import CHECK_AGENTS_TOOL, SPAWN_AGENTS_TOOL
        tool_list.append(SPAWN_AGENTS_TOOL)
        tool_list.append(CHECK_AGENTS_TOOL)
        logger.debug('[Task %s] 🐝 Swarm mode enabled — spawn_agents + check_agents tools available', tid)

    deferred_tools = []  # tools discovered via tool_search
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

        # ★ Tool deferral: partition into core (always loaded) and deferred
        #   (on-demand via tool_search). Saves 5-15K tokens on tool definitions.
        #   Inspired by Claude Code's shouldDefer + ToolSearchTool pattern.
        from lib.tools.deferral import partition_tools
        core_tools, deferred_tools = partition_tools(tool_list)
        if deferred_tools:
            tool_list = core_tools

    return tool_list, deferred_tools, has_real_tools, max_tool_rounds
