"""Conservative request-local routing for the native tool catalog.

``tools.nativeExposure=routed`` is the shipped default; ``full`` bypasses
this module.  The routed arm selects tool families from the latest user
request and explicit feature toggles.  Safety-critical/custom and
progressive MCP surfaces are never hidden by the router.
"""

from __future__ import annotations

from collections import OrderedDict
import re
import threading
import time
from typing import Any

from lib.tools.discovery_vocabulary import route_capability_aliases


# ``swarm`` rides _ALWAYS: the parallel sub-agent tools are default tools
# (no user-facing switch), so the router must never hide them. Artifact
# continuation also rides _ALWAYS; its builder returns no schemas outside V2,
# while a V2 result may name those functions on any source-tool round.
# Background lifecycle control must outlive launch intent; its builder still
# enforces runtime feature gates, and dispatch enforces task authority.
_ALWAYS = frozenset({'read_files', 'inspect_image', 'knowledge', 'skills',
                     'todo', 'mcp', 'custom', 'swarm', 'background',
                     'tool_result_artifacts', 'timer_watcher'})

_KEYWORDS: dict[str, tuple[str, ...]] = {
    'search': ('search', 'research', 'browse', 'online', 'latest', 'news',
               '查找', '搜索', '检索', '联网', '最新', '调研', '网页', 'http'),
    'project': ('code', 'repo', 'file', 'test', 'bug', 'fix', 'implement',
                'build', 'terminal', 'command', '代码', '仓库', '文件', '测试',
                '修复', '实现', '构建', '命令'),
    'browser': ('browser', 'page', 'dom', 'click', 'tab', 'cookie', '浏览器',
                '页面', '点击', '标签页'),
    'download': (
        'download', 'save', 'copy', 'archive', 'zip', 'install', 'unzip',
        'export', '下载', '保存', '拷贝', '复制', '压缩包', '安装', '解压',
        '导出', '最新版', '本地'),
    'desktop': ('desktop', 'clipboard', 'application', '桌面', '剪贴板', '应用'),
    'image': route_capability_aliases('image'),
    'video': (
        'motion', 'animation', 'report', 'research report', '动画', '报告',
    ) + route_capability_aliases('video'),
    'page_preview': route_capability_aliases('page_preview'),
    'conversation': ('conversation', 'peer', 'project board', 'charter',
                     '会话', '对话', '协作', '看板', '章程'),
    'memory': ('remember', 'memory', 'recall', '记住', '记忆', '回忆'),
    'scheduler': ('schedule', 'timer', 'remind', 'later', 'cron', '定时',
                  '提醒', '计划任务', '稍后'),
}


def _latest_user_text(messages: Any) -> str:
    if not isinstance(messages, list):
        return ''
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get('role') != 'user':
            continue
        content = message.get('content', '')
        if isinstance(content, str):
            return content.lower()
        if isinstance(content, list):
            return ' '.join(str(block.get('text') or '').lower()
                            for block in content if isinstance(block, dict))
    return ''


def _matches(text: str, group: str) -> bool:
    return any(term in text for term in _KEYWORDS[group])


def routed_native_spec_keys(ctx: Any, *, specs: Any = ()) -> set[str]:
    """Return the native ``ToolSpec.key`` set selected for this request."""
    text = _latest_user_text(getattr(ctx, 'messages', None))
    selected = set(_ALWAYS)

    # Frontend/headless switches are hard constraints.  Keyword routing may
    # add an unselected family for the current task, but it may never retract
    # a family the human explicitly enabled in the toolbar or API config.
    if (getattr(ctx, 'search_mode', 'off') in ('single', 'multi')
            or getattr(ctx, 'search_enabled', False)):
        selected.update({'search', 'fetch', 'search_settings'})
    elif getattr(ctx, 'fetch_enabled', False):
        selected.add('fetch')
    if getattr(ctx, 'browser_enabled', False):
        selected.add('browser')
    if getattr(ctx, 'desktop_enabled', False):
        selected.add('desktop')
    if getattr(ctx, 'image_gen_enabled', False):
        selected.add('image_gen')
    if getattr(ctx, 'human_guidance_enabled', False):
        selected.add('human_guidance')
    if getattr(ctx, 'scheduler_enabled', False):
        selected.add('scheduler')
    cfg = getattr(ctx, 'cfg', {}) or {}
    if (cfg.get('evolution') or {}).get('goal_id'):
        selected.add('evolution')
        if (cfg.get('evolution') or {}).get('stage') in ('executing', 'reviewing'):
            selected.add('evolution_source')
        if (cfg.get('evolution') or {}).get('stage') == 'reviewing':
            selected.add('evolution_review')
        if (cfg.get('evolution') or {}).get('stage')=='executing' and cfg.get('projectEnabled'):
            selected.add('evolution_artifact')
    if cfg.get('memoryEnabled', True):
        selected.add('memory')
    if cfg.get('mcpEnabled', True):
        selected.add('mcp')

    if (getattr(ctx, 'project_enabled', False)
            or getattr(ctx, 'code_exec_enabled', False)
            or _matches(text, 'project')):
        selected.add('project')
        selected.add('page_preview')
    if getattr(ctx, 'project_enabled', False):
        # The project brain read surface is eager by design — it must ride
        # every plain project turn, not wait for a keyword the model has no
        # reason to type. conv_ref joins it: the sibling digest names
        # list_conversations/get_conversation on every project turn. The
        # advisory-write half stays searchable but rides the same project gate
        # so native/full exposure paths can defer it provider-side instead of
        # dropping it. create_project is no longer a model-facing tool at all
        # (the absolute-path-write auto-register covers the useful case).
        selected.update({'conv_ref', 'project_brain', 'project_brain_write'})
    if _matches(text, 'search'):
        selected.update({'search', 'fetch', 'search_settings'})
    if _matches(text, 'browser'):
        selected.add('browser')
    if _matches(text, 'desktop'):
        selected.add('desktop')
    if _matches(text, 'image'):
        selected.add('image_gen')
    if _matches(text, 'video'):
        selected.update({'motion_video', 'produce', 'page_preview'})
    if _matches(text, 'page_preview'):
        selected.add('page_preview')
    if _matches(text, 'conversation') or any(
            isinstance(message, dict)
            and (message.get('convRefs') or message.get('convRefTexts'))
            for message in (getattr(ctx, 'messages', None) or [])):
        # A conversation/coordination mention wants the whole family visible:
        # open-a-sibling (conv_ref) AND the brain write surface (claim an
        # epic, message a peer) — the read half already rides project mode.
        selected.update({'conv_ref', 'project_brain', 'project_brain_write'})
    if _matches(text, 'memory'):
        selected.add('memory')
    if _matches(text, 'scheduler'):
        selected.add('scheduler')

    # Cross-family companions declare their own routing dependencies on the
    # ToolSpec. This keeps a capability such as browser-backed server download
    # from silently disappearing merely because its schema owner is neither
    # the generic search nor browser spec.
    active_groups = set(selected)
    if _matches(text, 'download'):
        active_groups.add('download')
    pending_specs = list(specs or ())
    changed = True
    while changed:
        changed = False
        for spec in pending_specs:
            if not (set(getattr(spec, 'native_route_groups', ()) or ())
                    & active_groups):
                continue
            key = str(getattr(spec, 'key', '') or '')
            if key and key not in selected:
                selected.add(key)
                active_groups.add(key)
                changed = True
    selected.discard('')

    # A bare chat/research task still needs a way to acquire external facts.
    if not getattr(ctx, 'project_enabled', False) and not re.search(
            r'\b(code|repo|test|bug|file)\b', text):
        selected.update({'search', 'fetch'})
    return _union_conversation_sticky(ctx, selected)


# ── Conversation-sticky exposure union ──
# Keyword routing is deliberately request-local, but the provider-facing wire
# must never SHRINK or reshuffle between turns of one conversation: any
# tools-array change breaks the provider prefix cache at the tools region.
# Measured on live kimi-k3 traffic (2026-09, 84 turn boundaries over 8 days):
# every named turn_boundary_rebill / body_change CacheRoundRecord carried the
# <hoisted>.tools + <bytes>tools culprits, with full-prefix re-bills at gaps
# as short as 62 s. Turn-1 routing still earns its cold-start schema saving,
# so the routed set is UNION-sticky per owner+conversation: a family selected
# by any earlier turn stays on the wire for the conversation's life; later
# turns only ever ADD families. Assembly order is unchanged (registry order),
# so one join costs at most one bounded insertion instead of a whole-array
# flip. Execution authority is untouched — the complete executable catalog is
# preserved before routing, and explicit allow/denylists still apply after
# assembly, so the union neither grants nor retracts anything. Mirrors the
# MCP wire freeze: a capability that appears mid-conversation rides the tail
# delta blocks rather than rewriting cached prefix bytes.
_STICKY_TTL_SECONDS = 24 * 60 * 60
_STICKY_STATE: OrderedDict[str, dict[str, Any]] = OrderedDict()
_STICKY_LOCK = threading.Lock()


def _sticky_state_capacity() -> int:
    from lib.tools.resource_policy import tool_routing_sticky_state_capacity
    return tool_routing_sticky_state_capacity()


def _union_conversation_sticky(ctx: Any, selected: set[str]) -> set[str]:
    """Union *selected* with the conversation's lifetime routed set.

    Stateless callers (empty ``conv_id`` — tests, compat adapters) keep the
    pure request-local result and record nothing.
    """
    conv = str(getattr(ctx, 'conv_id', '') or '').strip()
    if not conv:
        return selected
    try:
        owner = int(getattr(ctx, 'owner_user_id', 0) or 0)
    except (TypeError, ValueError):
        owner = 0
    key = f'{owner}:{conv}'
    now = time.monotonic()
    with _STICKY_LOCK:
        while (_STICKY_STATE
               and now - float(next(iter(_STICKY_STATE.values())).get(
                   'touched') or 0) > _STICKY_TTL_SECONDS):
            _STICKY_STATE.popitem(last=False)
        state = _STICKY_STATE.pop(key, None)
        if state is None:
            capacity = max(1, int(_sticky_state_capacity()))
            while len(_STICKY_STATE) >= capacity:
                _STICKY_STATE.popitem(last=False)
            state = {'keys': set(), 'touched': now}
        union = set(state['keys']) | set(selected)
        state['keys'] = union
        state['touched'] = now
        _STICKY_STATE[key] = state
        return union


def _reset_sticky_exposure_for_tests() -> None:
    with _STICKY_LOCK:
        _STICKY_STATE.clear()


__all__ = ['routed_native_spec_keys']
