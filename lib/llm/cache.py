# HOT_PATH
"""Anthropic prompt caching — cache breakpoint annotation.

Public API:
  - add_cache_breakpoints(body, log_prefix='')
"""

from lib.env_compat import getenv_compat
from lib.log import get_logger
from lib.model_info import is_claude

logger = get_logger(__name__)

# ── Cache-marker capability matrix ──
# Empirically probed on the sankuai gateway (2026-05-03).
#   1. Needs markers       → claude, glm-5, qwen, deepseek
#   2. Auto-caches         → minimax, doubao (markers harmful)
#   3. Unknown             → default NO markers
_CACHE_MARKERS_HELP = ('glm-5', 'qwen', 'deepseek')


def _gateway_honors_cache_markers(model: str) -> bool:
    """Return True if attaching cache_control markers helps this model."""
    if is_claude(model):
        return True
    enabled = getenv_compat('TOFU_CACHE_MARKERS_NONCLAUDE', default='1')
    if enabled.strip().lower() in ('0', 'false', 'no', 'off', ''):
        return False
    lowered = (model or '').lower()
    return any(k in lowered for k in _CACHE_MARKERS_HELP)


def add_cache_breakpoints(body, log_prefix=''):
    """Add Anthropic-style ephemeral cache breakpoints with mixed TTL.

    Annotates up to 4 content blocks with cache_control for:
      1. System messages (1-2 breakpoints for static/dynamic blocks)
      2. Last tool definition
      3. Last message with content — the conversation tail

    Mixed TTL strategy (when CACHE_EXTENDED_TTL is enabled):
      - BP1-BP3 (system + tools): ttl="1h" — stable content
      - BP4 (conversation tail): ttl="5m" (default) — changes every round
    """
    model = body.get('model', '')
    if not _gateway_honors_cache_markers(model):
        return

    _task_id = body.pop('_task_id', '')
    if _task_id:
        from lib.tasks_pkg.cache_tracking import latch_extended_ttl
        use_extended_ttl = latch_extended_ttl(_task_id)
    else:
        import lib as _lib
        use_extended_ttl = getattr(_lib, 'CACHE_EXTENDED_TTL', False)

    if not is_claude(model):
        use_extended_ttl = False

    if use_extended_ttl:
        _cc_stable = {'type': 'ephemeral', 'ttl': '1h'}
        _cc_tail   = {'type': 'ephemeral'}
    else:
        _cc_stable = {'type': 'ephemeral'}
        _cc_tail   = {'type': 'ephemeral'}

    messages = body.get('messages', [])

    # Phase 0: Strip ALL existing cache_control
    for i, msg in enumerate(messages):
        content = msg.get('content')
        if isinstance(content, list):
            for j, block in enumerate(content):
                if isinstance(block, dict) and 'cache_control' in block:
                    content[j] = {k: v for k, v in block.items() if k != 'cache_control'}
    tools = body.get('tools')
    if tools:
        for t_idx, tool in enumerate(tools):
            fn = tool.get('function')
            if fn and 'cache_control' in fn:
                tools[t_idx] = {**tool,
                                'function': {k: v for k, v in fn.items() if k != 'cache_control'}}

    bp = 0

    # Cache system messages (BP1-BP2: stable, extended TTL)
    for i, msg in enumerate(messages):
        if msg.get('role') != 'system' or bp >= 4:
            continue
        content = msg.get('content', '')
        if isinstance(content, str) and content.strip():
            messages[i] = {**msg, 'content': [
                {'type': 'text', 'text': content,
                 'cache_control': dict(_cc_stable)}
            ]}
            bp += 1
        elif isinstance(content, list) and content:
            for blk_idx, blk in enumerate(content):
                if bp >= 4:
                    break
                if isinstance(blk, dict) and blk.get('type') == 'text':
                    content[blk_idx] = {**blk, 'cache_control': dict(_cc_stable)}
                    bp += 1

    # Cache last tool definition (BP3: stable, extended TTL)
    tools = body.get('tools')
    if tools and bp < 4:
        fn = tools[-1].get('function')
        if fn:
            tools[-1] = {**tools[-1],
                         'function': {**fn, 'cache_control': dict(_cc_stable)}}
            bp += 1

    # Cache conversation tail (BP4: volatile, short TTL)
    if len(messages) >= 2 and bp < 4:
        _bp4_placed = False
        for _bp4_offset in range(1, min(6, len(messages))):
            idx = len(messages) - _bp4_offset
            if idx <= 0:
                break
            msg = messages[idx]
            if msg.get('role') == 'system':
                break
            content = msg.get('content', '')
            if isinstance(content, str) and content:
                messages[idx] = {**msg, 'content': [
                    {'type': 'text', 'text': content,
                     'cache_control': dict(_cc_tail)}
                ]}
                bp += 1
                _bp4_placed = True
                break
            elif isinstance(content, list) and content:
                last = content[-1]
                if isinstance(last, dict):
                    content[-1] = {**last, 'cache_control': dict(_cc_tail)}
                    bp += 1
                    _bp4_placed = True
                    break
        if not _bp4_placed and log_prefix:
            logger.debug('%s Cache: BP4 tail breakpoint could not be placed '
                         '(no message with content near tail)', log_prefix)

    if bp > 0 and log_prefix:
        _ttl_info = ' (mixed TTL: BP1-3=1h, BP4=5m)' if use_extended_ttl else ''
        logger.debug('%s Cache: %d breakpoint(s)%s', log_prefix, bp, _ttl_info)
