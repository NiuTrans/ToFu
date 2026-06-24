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

# Anthropic's hard ceiling: at most 4 ``cache_control`` markers per request.
_MAX_CACHE_BP = 4

# The conversation TAIL and the last TOOL definition each get a RESERVED
# marker that the system phase can never consume. The tail marker covers the
# growing prefix and is the single highest-value segment — without it
# cache_creation is 0 every round and cache_read stays pinned at the static
# system+tools prefix, so the whole conversation body is re-billed uncached on
# every turn (conv mqj7x0t8: 138/151 rounds with cache_w=0). The system
# message is assembled as several cache-stable blocks (static prompt +
# optional memory-accumulation + optional swarm/parallel-execution, and more
# may be added later), so its budget MUST be whatever is left after these
# reservations — never the other way around.
_TAIL_RESERVED_BP = 1   # conversation tail (volatile, short TTL)
_TOOL_RESERVED_BP = 1   # last tool definition (stable)


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

    Annotates up to ``_MAX_CACHE_BP`` (4) content blocks with cache_control.
    The last tool definition and the conversation tail each get a RESERVED
    marker (see ``_TOOL_RESERVED_BP`` / ``_TAIL_RESERVED_BP``); the system
    message gets only the markers left over after those reservations. This
    ordering guarantees the highest-value segments — the growing-prefix tail
    above all — can never be starved no matter how many cache-stable blocks
    the system message accumulates:
      1. System messages (static / memory / swarm blocks) — leftover budget
      2. Last tool definition — reserved
      3. Last message with content — the conversation tail — reserved

    Mixed TTL strategy (when CACHE_EXTENDED_TTL is enabled):
      - System + tools: ttl="1h" — stable content
      - Conversation tail: ttl="5m" (default) — changes every round
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

    # Reserve markers for the tool + tail phases up front, then give the
    # system phase only what's left. This is what guarantees those two
    # high-value breakpoints can never be starved — adding a 4th/5th system
    # block just drops the extra system marker, it cannot steal the tool or
    # tail slot. Reservations are conditional on those phases actually having
    # something to mark (no tools → the tool marker reverts to the system
    # phase; this also matters for the system-only edge case).
    _reserve = 0
    if body.get('tools'):
        _reserve += _TOOL_RESERVED_BP
    if len(messages) >= 2:
        _reserve += _TAIL_RESERVED_BP
    _system_bp_budget = max(0, _MAX_CACHE_BP - _reserve)

    # Cache system messages (stable, extended TTL), capped at the budget left
    # after the tool/tail reservations above.
    for i, msg in enumerate(messages):
        if msg.get('role') != 'system' or bp >= _system_bp_budget:
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
                if bp >= _system_bp_budget:
                    break
                if isinstance(blk, dict) and blk.get('type') == 'text':
                    content[blk_idx] = {**blk, 'cache_control': dict(_cc_stable)}
                    bp += 1

    # Cache last tool definition (stable, extended TTL)
    tools = body.get('tools')
    if tools and bp < _MAX_CACHE_BP:
        fn = tools[-1].get('function')
        if fn:
            tools[-1] = {**tools[-1],
                         'function': {**fn, 'cache_control': dict(_cc_stable)}}
            bp += 1

    # Cache conversation tail (volatile, short TTL)
    if len(messages) >= 2 and bp < _MAX_CACHE_BP:
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
