# HOT_PATH
"""Anthropic prompt caching — cache breakpoint annotation.

Public API:
  - add_cache_breakpoints(body, log_prefix='')
"""

from lib.env_compat import getenv_compat
from lib.log import get_logger
from lib.model_info import is_claude

logger = get_logger(__name__)

# ── In-memory cache-fix generation stamp (deploy self-report) ──
# A MONOTONIC integer bumped whenever a prefix-cache live↔replay fix lands. The
# server prints THIS value (from the IMPORTED module, i.e. the bytecode actually
# loaded into the running process) in its boot banner, so the deploy-acceptance
# harness can verify the RUNNING code version — not merely the on-disk source.
# A Python process compiles .py at import and never re-reads it, so a disk-fresh
# source proves nothing about the loaded bytecode; only this self-report does.
# Bump this by 1 for each new cache-fix that must be confirmed deployed.
#   gen 5 = ab161bf str↔block + 1274cee raw↔stripped + 0a9f6af prefill-skip
#           + 8ecbbcf reasoning_content parity + 1920827 single-source builder.
CACHE_FIX_GEN = 5

# ── Cache-marker capability matrix ──
# Empirically probed on the sankuai gateway (2026-05-03).
#   1. Needs markers       → claude, glm-5, qwen, deepseek
#   2. Auto-caches         → minimax, doubao (markers harmful)
#   3. Unknown             → default NO markers
_CACHE_MARKERS_HELP = ('glm-5', 'qwen', 'deepseek')

# Anthropic's hard ceiling: at most 4 ``cache_control`` markers per request.
_MAX_CACHE_BP = 4

# The conversation TAIL, the last TOOL definition, and (in a long agent loop)
# a MID-HISTORY stepping-stone each get a RESERVED marker the system phase can
# never consume. The tail marker covers the growing prefix and is the single
# highest-value segment — without it cache_creation is 0 every round and
# cache_read stays pinned at the static system+tools prefix, so the whole
# conversation body is re-billed uncached on every turn (conv mqj7x0t8:
# 138/151 rounds with cache_w=0).
#
# The system prompt is static and CONTIGUOUS (static prompt + optional
# memory-accumulation + optional swarm/parallel-execution blocks), so ONE
# marker on its LAST block caches the entire system prefix — spending 2-3
# markers on separate system blocks is pure waste that starves the mid anchor.
# So the system phase gets only the budget left after the tool/tail/mid
# reservations (min 1), and places its marker(s) on the LAST stable block(s).
_TAIL_RESERVED_BP = 1   # conversation tail (volatile, short TTL)
_TOOL_RESERVED_BP = 1   # last tool definition (stable)
_MID_RESERVED_BP = 1    # mid-history stepping-stone (long loops — see below)

# Anthropic's cache lookup only searches ~20 content blocks BACKWARD from a
# breakpoint for a prior cache entry to extend. In a tool loop emitting ~3
# blocks/turn, a lone rolling tail breakpoint loses sight of the previously
# written cache segment once the tail advances >20 blocks past it, after which
# the whole prefix is re-written every round even though the bytes are
# identical. A stepping-stone breakpoint that TRAILS the tail by a bounded,
# quantized distance keeps a prior cache entry permanently within the tail's
# 20-block lookback. Only armed once the TAIL is genuinely farther than the
# ~20-block window from the head — below that the head is still directly
# reachable from the rolling tail, so a mid anchor is wasted budget (and, if
# forced, would collapse onto the early user turn). See the arming gate below.
_MID_LOOKBACK = 20
# The stepping-stone trails the tail by ~_MID_TRAIL messages but is snapped to
# a COARSE-QUANTIZED absolute index (multiple of _MID_STEP) so it only ADVANCES
# IN JUMPS, not every round. A breakpoint that moved every round would always
# be a fresh write, never a read. Quantizing keeps message[mid_idx] byte-
# identical for a run of rounds (messages are append-only within a task) → it
# caches once, is read back for the next _MID_STEP rounds, then jumps forward.
# _MID_TRAIL < 20 so the stone stays inside the rolling tail's lookback window.
_MID_STEP = 8
_MID_TRAIL = 12


def _is_prefill_converted(msg) -> bool:
    """True if ``msg`` is a trailing assistant turn that was converted to a
    user turn by ``_strip_trailing_assistant_for_claude`` (Claude prefill guard).

    Such a turn is a VOLATILE synthetic representation: it exists as
    ``user('[Your previous response for context]:\\n'+X)`` ONLY while it is the
    trailing message; the moment the user's next turn arrives it becomes buried
    prefix and is sent as a bare ``assistant(X)`` again — a role+content flip.
    So a cache breakpoint anchored on it writes an entry the NEXT round's bytes
    can never read back → a (rare, regenerate/resume/interrupted-only) prefix
    break. add_cache_breakpoints refuses to mark it. Keyed on the sentinel that
    _model_tweaks stamps (CLAUDE_PREFILL_SENTINEL — kept in sync there).
    """
    if not isinstance(msg, dict) or msg.get('role') != 'user':
        return False
    content = msg.get('content')
    if isinstance(content, str):
        text = content
    elif isinstance(content, list) and content and isinstance(content[0], dict):
        text = content[0].get('text', '') or ''
    else:
        return False
    from lib.llm.body._model_tweaks import CLAUDE_PREFILL_SENTINEL
    return text.startswith(CLAUDE_PREFILL_SENTINEL)


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

    # Read the task-id NON-destructively: the streaming retry loop
    # (lib/llm/stream.py / astream.py) re-feeds the SAME body dict to
    # prepare_request → add_cache_breakpoints on every 429/503 attempt. If we
    # POPPED _task_id here, attempt 2+ would find it gone and fall back to the
    # live global CACHE_EXTENDED_TTL — flipping the ttl='1h'↔bare marker (and
    # the extended-cache-ttl beta header) mid-task → a DIFFERENT Anthropic
    # cache key → full prefix miss (the mrne3bqe "R4/R5 read=0, R6 rebounds"
    # bug). Leaving _task_id on the body keeps the latch decision stable across
    # every attempt. The key is stripped only at the OpenAI serialization
    # boundary (prepare_request); the Anthropic path rebuilds the body from an
    # allowlist so it never leaks there.
    _task_id = body.get('_task_id', '')
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

    # Phase 0.5: Representation invariance — decouple a message's byte
    # representation from WHETHER it is anchored this round.
    #
    # Root cause of the "wrote-but-can't-read-back" pin: a marker is placed by
    # wrapping ``str`` content into ``[{'type':'text',...}]``. When the anchor
    # quantum-advances the following round, the previously-anchored message is
    # rebuilt from the persistent list as a bare ``str`` again — a str↔list
    # FLIP. For ``tool`` messages that flip SURVIVES ``openai_body_to_anthropic``
    # (tool str→str, list→blocks), so the message's wire bytes genuinely change
    # between rounds. Anthropic then looks back from the moving anchor, finds the
    # prior [0:bp] cache entry but the prefix bytes no longer match at the
    # flipped message → cannot extend → falls all the way back to the static
    # floor → cache_read pins.
    #
    # Fix: normalize EVERY markable non-empty ``str`` content to the single-block
    # form up front, regardless of whether it gets a marker this round. Now
    # anchoring only ADDS/REMOVES the ``cache_control`` key on an
    # already-list block; ``_msg_bytes`` (which strips cache_control) is
    # byte-identical in the anchored and un-anchored states, and so is the
    # Anthropic translation. Assistant messages are left alone (their tool_calls
    # take a dedicated block path); system content is handled by its own phase.
    for i, msg in enumerate(messages):
        role = msg.get('role')
        if role in ('tool', 'user'):
            content = msg.get('content')
            if isinstance(content, str) and content:
                messages[i] = {**msg, 'content': [{'type': 'text', 'text': content}]}
        elif role == 'assistant':
            # An assistant turn is frequently the conversation TAIL (the model's
            # prose before its tool_calls, or a prefill-resume round), so the
            # rolling tail marker wraps→unwraps its str content the same way —
            # the flip is asymmetric if we only normalize tool/user. Normalize
            # it too whenever it carries NON-EMPTY ``str`` content — INCLUDING
            # an assistant turn that also carries ``tool_calls`` (the
            # prose-before-run_command shape).
            #
            # ★ THE {content} FLOOR-MISS FIX. An ``assistant/tool_call`` turn
            #   with prose was PREVIOUSLY carved out (``and not tool_calls``).
            #   That left its ``str`` content un-normalized, so the tail phase
            #   wrapped it into a block the round it was the tail, and it
            #   reverted to a bare ``str`` the round it became buried prefix —
            #   a canonical-INVISIBLE ``str``↔block flip on ``content`` that
            #   re-billed the cached prefix EVERY round (the dominant residual
            #   miss the field-level tracer proved as ``…(run_command){content}``).
            #   Normalizing non-empty content up front makes anchoring only
            #   toggle a ``cache_control`` key on an already-list block, so the
            #   turn's ``content`` bytes are identical whether or not it is the
            #   tail. The tool_use blocks still drive ``_assistant_blocks``'
            #   last-block marker hoist (it moves a content-level marker onto the
            #   last emitted block regardless), so the marker logic is undisturbed.
            #
            #   EMPTY content is still left alone: a fabricated ``[{text:''}]``
            #   block would be a new empty text block the model never wrote (and
            #   the old carve-out's real concern), so the guard stays
            #   ``isinstance(content, str) and content``.
            content = msg.get('content')
            if isinstance(content, str) and content:
                messages[i] = {**msg, 'content': [{'type': 'text', 'text': content}]}

    bp = 0

    # Reserve markers for the tool + tail (+ mid-anchor) phases up front, then
    # give the system phase only what's left. This guarantees those high-value
    # breakpoints can never be starved — adding another system block just drops
    # the extra system marker, it cannot steal the tool / tail / mid slot.
    # Reservations are conditional on those phases actually having something to
    # mark (no tools → the tool marker reverts to the system phase; short conv
    # → no mid anchor; this also matters for the system-only edge case).
    _nonsys_msgs = sum(1 for m in messages if m.get('role') != 'system')
    _n_sys = len(messages) - _nonsys_msgs
    # Arm the mid anchor only once the tail is genuinely FARTHER than the
    # lookback window from the head — a stone trailing the tail by _MID_TRAIL
    # must land safely past the head region (system + first user), else it
    # collapses onto the early user turn (which the anti-oscillation regression
    # forbids). Require the trail target to sit at least _MID_LOOKBACK messages
    # past the head.
    _head_floor = _n_sys + 1  # system block(s) + the first user turn
    _mid_target = len(messages) - _MID_TRAIL
    _mid_armed = _mid_target >= _head_floor + _MID_LOOKBACK
    _reserve = 0
    if body.get('tools'):
        _reserve += _TOOL_RESERVED_BP
    if len(messages) >= 2:
        _reserve += _TAIL_RESERVED_BP
    if _mid_armed:
        _reserve += _MID_RESERVED_BP
    _system_bp_budget = max(0, _MAX_CACHE_BP - _reserve)

    # Cache the system prefix, capped at the budget left after the reservations
    # above. The system prompt is CONTIGUOUS, so a marker on its LAST stable
    # block already caches everything before it — we place on the LAST N blocks
    # (N = budget), not the first, so a single leftover marker still covers the
    # whole system prefix rather than only the first block.
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
            _text_idxs = [j for j, blk in enumerate(content)
                          if isinstance(blk, dict) and blk.get('type') == 'text']
            _slots = _system_bp_budget - bp
            for blk_idx in _text_idxs[-_slots:] if _slots > 0 else []:
                content[blk_idx] = {**content[blk_idx],
                                    'cache_control': dict(_cc_stable)}
                bp += 1

    # Cache last tool definition (stable, extended TTL)
    tools = body.get('tools')
    if tools and bp < _MAX_CACHE_BP:
        fn = tools[-1].get('function')
        if fn:
            tools[-1] = {**tools[-1],
                         'function': {**fn, 'cache_control': dict(_cc_stable)}}
            bp += 1

    # Cache a stepping-stone breakpoint that TRAILS the tail by ~_MID_TRAIL
    # messages (quantized to _MID_STEP), keeping a prior cache entry inside the
    # rolling tail's 20-block lookback window (the 20-block-lookback fix). The
    # quantized absolute index advances in jumps, not every round — a marker
    # that moved every round would always be a fresh write, never a read.
    # Within a task messages are append-only, so message[mid_idx] stays
    # byte-identical for a run of rounds → caches once, then reads back.
    if _mid_armed and bp < _MAX_CACHE_BP:
        # Target index trails the tail; snap DOWN to a _MID_STEP multiple so it
        # only moves every _MID_STEP rounds. Floor it past the head region so it
        # can never collapse onto the system/first-user turn, and keep it before
        # the reserved tail (last ~3 msgs).
        _mid_idx = max(_head_floor, (_mid_target // _MID_STEP) * _MID_STEP)
        _placed_mid = False
        # Scan forward from the quantized index to the first markable message,
        # never entering the reserved tail zone (last 3 msgs) or the system head.
        for i in range(_mid_idx, len(messages) - 3):
            msg = messages[i]
            if msg.get('role') == 'system':
                continue
            if _is_prefill_converted(msg):
                continue  # volatile — its bytes flip to bare assistant next round
            content = msg.get('content', '')
            if isinstance(content, str) and content:
                messages[i] = {**msg, 'content': [
                    {'type': 'text', 'text': content,
                     'cache_control': dict(_cc_stable)}
                ]}
                bp += 1
                _placed_mid = True
                break
            elif isinstance(content, list) and content and isinstance(content[-1], dict):
                content[-1] = {**content[-1], 'cache_control': dict(_cc_stable)}
                bp += 1
                _placed_mid = True
                break
        if not _placed_mid and log_prefix:
            logger.debug('%s Cache: mid-history anchor not placed '
                         '(no markable msg near trail index %d)', log_prefix, _mid_idx)

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
            if _is_prefill_converted(msg):
                # The trailing prefill-converted turn is volatile (user+sentinel
                # now, bare assistant once buried). Marking it writes a cache
                # entry the next round cannot read back — walk PAST it to the
                # previous stable turn so the tail breakpoint still lands.
                continue
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
