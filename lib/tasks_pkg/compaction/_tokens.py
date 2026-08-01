"""Token estimation + context-limit decision helpers.

Pure functions of ``messages`` + ``task`` — no side effects, no DB, no LLM.
That makes this the cleanest target for unit tests, and the safest module
for the orchestrator to import for "should I trigger force-compact?"
decisions.

Imports nothing from sibling sub-modules except ``_constants``.
"""

import re
import time

from lib.log import get_logger
from lib.tasks_pkg.compaction._constants import (
    _COMPACTION_RESERVE,
    _cooldown_lock,
    _DEFAULT_CONTEXT_LIMIT,
    _IMAGE_TOKENS_DEFAULT,
    _OUTPUT_RESERVE,
    _SUMMARY_COOLDOWN,
    _SUMMARY_TRIGGER_RATIO,
    _summary_cooldowns,
)

logger = get_logger(__name__)


def _estimate_msg_tokens(msg: dict) -> int:
    """Rough token estimate for a single message (CJK-aware).

    Uses ``lib.token_counter.heuristic.cheap_estimate_text`` which is
    the same entropy-aware heuristic (1 token per CJK char + 1 token per
    dense base64/hex char + 1 token per ~3 other chars) that gates the
    richer counter backends.

    For a bit-exact authoritative count (via tiktoken / Anthropic
    count_tokens / HF tokenizer), callers should use
    ``_count_tokens_authoritative()`` below.

    Images: fixed estimate per image (NOT base64 length) — the LLM API
    processes images natively and charges ~85-1105 tokens regardless of
    the data-URL size.
    """
    from lib.token_counter.heuristic import cheap_estimate_text

    text_tokens = 0
    image_tokens = 0
    for field in ('content', 'reasoning_content'):
        val = msg.get(field)
        if not val:
            continue
        if isinstance(val, str):
            text_tokens += cheap_estimate_text(val)
        elif isinstance(val, list):
            for block in val:
                if isinstance(block, dict):
                    if block.get('type') == 'text':
                        text_tokens += cheap_estimate_text(block.get('text', ''))
                    elif block.get('type') == 'image_url':
                        image_tokens += _IMAGE_TOKENS_DEFAULT
    for tc in msg.get('tool_calls', []):
        text_tokens += cheap_estimate_text(tc.get('function', {}).get('arguments', ''))
    return text_tokens + image_tokens


def _estimate_total_tokens(messages: list) -> int:
    """Sum per-message CJK-aware estimates. Fast — never networks."""
    return sum(_estimate_msg_tokens(m) for m in messages)


def _count_tokens_authoritative(messages: list, task: dict | None = None) -> tuple[int, str]:
    """Authoritative token count via ``lib.token_counter.count_tokens``.

    Tries (in order): usage_cache → native count_tokens API →
    exact offline tokenizer (tiktoken / deepseek / HF) → heuristic.

    Returns ``(tokens, method)`` where method is the backend that
    produced the count (``'usage_cache' | 'anthropic_api' | 'tiktoken' | …``).
    """
    try:
        from lib.token_counter import count_tokens as _ct_count_tokens
    except Exception as e:
        logger.debug('[Compact] token_counter unavailable, using heuristic: %s', e)
        return _estimate_total_tokens(messages), 'heuristic_fallback'

    cfg = (task or {}).get('config', {}) or {}
    model = cfg.get('model', '') or ''
    context_limit = _get_context_limit(task)
    conv_id = (task or {}).get('convId', '') or ''
    # The orchestrator stashes the live tool schema here (see
    # orchestrator.py `_assemble_tool_list`). It ships in every request and
    # the gateway tokenizes it, so the gate must include it or it
    # under-counts by the whole tool-schema size on tool-heavy configs.
    tools = (task or {}).get('_tool_schema') or None

    try:
        result = _ct_count_tokens(
            messages,
            model=model,
            tools=tools,
            conv_id=conv_id or None,
            context_limit=context_limit,
        )
        auth_tokens = int(result.get('tokens', 0))
        method = str(result.get('method', 'unknown'))
    except Exception as e:
        logger.warning('[Compact] count_tokens call failed, falling back to '
                       'heuristic: %s', e)
        return _estimate_total_tokens(messages), 'heuristic_fallback'

    # Safety floor for the COMPACTION GATE only (not the UI counter): never
    # let the gate report FEWER tokens than the conservative entropy
    # heuristic would. tiktoken's cl100k vocabulary under-counts Claude's
    # tokenizer on high-entropy content (base64/minified data) — for conv
    # mq7y3irly1r4hu tiktoken gave 0.66x of the gateway while the heuristic
    # gave 0.83x. A gate that trusts the lower number can let an oversized
    # prompt slip past the trigger into the fatal reactive path. Taking the
    # max keeps the gate on the safe side regardless of which backend wins,
    # while the UI still gets the accuracy-optimized count from count_tokens.
    heuristic_tokens = _estimate_total_tokens(messages)
    if heuristic_tokens > auth_tokens:
        logger.debug('[Compact] heuristic floor %d > authoritative %d (via %s) '
                     '— using floor for gate', heuristic_tokens, auth_tokens, method)
        return heuristic_tokens, f'{method}+heuristic_floor'
    return auth_tokens, method


# ── Parse Bedrock / Anthropic "prompt too long" error text ─────────────

_PROMPT_TOO_LONG_RE = re.compile(
    r'(\d[\d,]*)\s*tokens?\s*(?:>|exceeds?|greater than)?\s*(\d[\d,]*)?\s*(?:maximum|limit)?',
    re.IGNORECASE,
)


def _parse_reported_token_count(error_text: str) -> int | None:
    """Extract the requested size N from an overflow error.

    Handles both "prompt is too long: N tokens > M maximum" (N first) and
    "maximum context length is M tokens … you requested N tokens" (M first)
    by delegating to :func:`_parse_context_overflow` and returning N.
    """
    requested, _stated_max = _parse_context_overflow(error_text)
    return requested


# Gateway/provider-stated ceiling, e.g.
#   "This model's maximum context length is 1048565 tokens"
#   "... > 200000 maximum"
_STATED_MAX_RE = re.compile(
    r'(?:maximum\s+context\s+length\s+is|context\s+length\s+is|'
    r'maximum\s+(?:is|of)|max(?:imum)?\s+tokens?\s+(?:is|of)?)\s*(\d[\d,]*)',
    re.IGNORECASE,
)
_STATED_MAX_TRAILING_RE = re.compile(r'>\s*(\d[\d,]*)\s*(?:tokens?\s*)?(?:maximum|limit)',
                                     re.IGNORECASE)
# Explicitly-requested size, e.g. "you requested 1076791 tokens".
_REQUESTED_RE = re.compile(r'(?:you\s+)?requested\s+(\d[\d,]*)', re.IGNORECASE)


def _parse_context_overflow(error_text: str) -> tuple[int | None, int | None]:
    """Parse an overflow error into ``(requested_tokens, stated_maximum)``.

    Either element may be ``None`` if absent. ``stated_maximum`` is the
    authoritative ceiling the gateway named (preferred for learning a shrunk
    limit); ``requested_tokens`` is the size of the rejected prompt (a lower
    bound used only when no maximum was stated).

    Examples
    --------
    "prompt is too long: 210819 tokens > 200000 maximum"
        → (210819, 200000)
    "maximum context length is 1048565 tokens. However, you requested 1076791 tokens"
        → (1076791, 1048565)
    """
    if not error_text:
        return None, None

    def _coerce(s: str | None) -> int | None:
        if not s:
            return None
        try:
            n = int(s.replace(',', ''))
        except (ValueError, AttributeError) as e:
            logger.debug('[_tokens] overflow-count coerce failed for %r: %s', s, e)
            return None
        return n if 0 < n < 50_000_000 else None

    stated_max = None
    try:
        m = _STATED_MAX_RE.search(error_text)
        if m:
            stated_max = _coerce(m.group(1))
        if stated_max is None:
            m = _STATED_MAX_TRAILING_RE.search(error_text)
            if m:
                stated_max = _coerce(m.group(1))
    except (ValueError, AttributeError) as e:
        logger.debug('[_tokens] stated-max parse caught %s: %s', type(e).__name__, e)

    requested = None
    try:
        m = _REQUESTED_RE.search(error_text)
        if m:
            requested = _coerce(m.group(1))
        if requested is None:
            # Fall back to the leading "N tokens" of the classic shape.
            m = _PROMPT_TOO_LONG_RE.search(error_text)
            if m:
                requested = _coerce(m.group(1))
    except (ValueError, AttributeError) as e:
        logger.debug('[_tokens] requested parse caught %s: %s', type(e).__name__, e)

    return requested, stated_max


def _human_size(byte_count: int) -> str:
    """Format a byte/char count as a human-readable string."""
    if byte_count < 1024:
        return f'{byte_count}B'
    elif byte_count < 1024 * 1024:
        return f'{byte_count / 1024:.1f}KB'
    else:
        return f'{byte_count / (1024 * 1024):.1f}MB'


# ═══════════════════════════════════════════════════════════════════════════════
#  Context limit helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _get_static_context_limit(task: dict | None = None) -> int:
    """Static (preset) context window for the model in *task*.

    Pure name-based heuristic — does NOT consult auto-learned values.
    Use :func:`_get_context_limit` for the operational lookup that
    layers learned overrides on top.
    """
    if task:
        model_raw = (task.get('config', {}) or {}).get('model', '') or ''
        model = model_raw.lower()

        try:
            # 1M window: Opus & Sonnet ≥ 4.6 (including the 5-series bare
            # aliases), Fable ≥ 5. ONE parser (claude_line_version) — a
            # bare-major alias like claude-sonnet-5 can no longer slip
            # through while dated snapshots (claude-opus-4-20250514)
            # correctly stay at 200K.
            from lib.model_info import claude_line_version
            for line, floor in (('opus', (4, 6)), ('sonnet', (4, 6)),
                                ('fable', (5, 0))):
                v = claude_line_version(model_raw, line)
                if v is not None and v >= floor:
                    return 1_000_000
        except Exception as e:
            logger.debug('[Compact] claude_line_version probe failed: %s', e)

        limits = {
            'claude-opus-4.6':   1_000_000,
            'claude-sonnet-4.6': 1_000_000,
            'claude':   200_000,
            'gpt-4':    128_000,
            'gpt-4o':   128_000,
            'o1':       200_000,
            'o3':       200_000,
            'o4':       200_000,
            'gemini':   1_000_000,
            'qwen':     128_000,
            # DeepSeek V4 family (pro + flash) is a true 1M-context model.
            # These MUST precede the generic 'deepseek' key below: lookup is
            # substring-match in dict-insertion order, so the specific V4
            # entries win while older deepseek-chat/v3.x/reasoner still fall
            # through to the 128k default.
            'deepseek-v4-pro':   1_000_000,
            'deepseek-v4-flash': 1_000_000,
            'deepseek': 128_000,
            'doubao':   128_000,
            # kimi-k3: true 1M window (owner-confirmed 2026-07-26; also
            # recorded in model_info/_max_output.py + pricing/_tables.py).
            # NOTE: only the k3 generation — k2.x ships 256k and must NOT
            # get a blanket 'kimi' key here.
            'kimi-k3':  1_000_000,
            'minimax':  1_000_000,
        }
        for key, limit in limits.items():
            if key in model:
                return limit
    return _DEFAULT_CONTEXT_LIMIT


_MIN_USABLE_RATIO = 0.7
"""Floor for usable context as a fraction of the model's window.

``_OUTPUT_RESERVE`` is a fixed absolute tuned for the 1M-context Claude
family (its 128K max-output cap).  On a small-window model (e.g. a 128K
gpt-4/qwen/deepseek) that fixed reserve can equal or exceed the whole
window, driving ``limit - reserves`` to zero or negative.  A non-positive
``usable`` makes the force-compact trigger threshold non-positive too, so
L2 summary compaction fires on *every* request regardless of size.

Clamp ``usable`` to at least this fraction of the window so reserves can
never consume more than ``1 - _MIN_USABLE_RATIO`` (30%) of the context.
0.7 preserves the historical small-model behaviour: before the 2026-06-02
``_OUTPUT_RESERVE`` 32K→128K bump, a 128K window had
``usable = (128000-32000-8000)/128000 ≈ 0.69``.  The frontend
(``static/js/context-bar.js``) applies the same floor."""


def _usable_context(context_limit: int) -> int:
    """Usable context tokens after output + compaction reserves.

    Floored at ``_MIN_USABLE_RATIO`` of ``context_limit`` so an oversized
    fixed reserve (see ``_MIN_USABLE_RATIO``) can never produce a
    zero/negative budget on small-window models.
    """
    raw = context_limit - _OUTPUT_RESERVE - _COMPACTION_RESERVE
    return max(raw, int(context_limit * _MIN_USABLE_RATIO))


def _get_context_limit(task: dict | None = None) -> int:
    """Look up the model's effective context window in tokens."""
    static_limit = _get_static_context_limit(task)
    if not task:
        return static_limit
    try:
        from lib.context_limits import resolve_learned_context_limit
        provider_id = task.get('provider_id') or ''
        model = (task.get('config', {}) or {}).get('model', '') or ''
        return resolve_learned_context_limit(provider_id, model, static_limit)
    except Exception as e:
        logger.debug('[Compact] context_limits lookup failed: %s', e)
    return static_limit


def resolve_model_context_limit(model: str, provider_id: str = '') -> int:
    """Effective context window for a bare ``(model, provider_id)`` pair.

    Frontend-facing sibling of :func:`_get_context_limit` that doesn't need a
    full task dict — used to build the per-model limit map served in
    ``/api/v1/server-config`` so the Context Health Bar reads exact numbers
    (static preset + any auto-learned override) instead of re-deriving them.
    """
    synthetic = {'config': {'model': model or ''}, 'provider_id': provider_id or ''}
    return _get_context_limit(synthetic)


def build_context_policy() -> dict:
    """Return the authoritative context-window policy for frontend consumers.

    The Context Health Bar (``static/js/context-bar.js``) used to hard-code
    a copy of these constants — guaranteed to drift from the Python source.
    Serving them through ``/api/v1/server-config`` makes this module the
    single source of truth: the gauge reads numbers, never re-derives them.

    All values are the same constants the orchestrator uses to decide when
    to force-compact, so the bar's "hot" zone lines up exactly with the
    server's trigger:

        usable  = context_limit - output_reserve - compaction_reserve
        trigger = usable * summary_trigger_ratio   (tokens)

    On small-window models a fixed ``output_reserve`` can exceed the whole
    window, so ``usable`` is floored at ``min_usable_ratio`` of the limit
    (see :func:`_usable_context`).  The frontend MUST apply the same floor.

    Returns:
        Dict with ``default_limit``, ``output_reserve``, ``compaction_reserve``,
        ``summary_trigger_ratio`` and ``min_usable_ratio``.
    """
    return {
        'default_limit': _DEFAULT_CONTEXT_LIMIT,
        'output_reserve': _OUTPUT_RESERVE,
        'compaction_reserve': _COMPACTION_RESERVE,
        'summary_trigger_ratio': _SUMMARY_TRIGGER_RATIO,
        'min_usable_ratio': _MIN_USABLE_RATIO,
    }


def _should_force_compact(messages: list, task: dict | None = None) -> bool:
    """Decide whether force-compact should fire.

    Returns True when estimated token count exceeds
    ``_SUMMARY_TRIGGER_RATIO`` of usable context.
    """
    conv_id = task.get('convId', '') if task else ''
    log_id = conv_id[:8] if conv_id else '?'

    with _cooldown_lock:
        last = _summary_cooldowns.get(conv_id, 0)
        elapsed = time.time() - last
        if elapsed < _SUMMARY_COOLDOWN:
            logger.debug('[Compact] conv=%s  cooldown active (%.0fs remaining)',
                         log_id, _SUMMARY_COOLDOWN - elapsed)
            return False

    context_limit = _get_context_limit(task)
    usable = _usable_context(context_limit)
    trigger_threshold = int(usable * _SUMMARY_TRIGGER_RATIO)

    total_tokens, method = _count_tokens_authoritative(messages, task)

    logger.debug('[Compact] conv=%s  tokens=%d (via %s)  threshold=%d  '
                 'limit=%d  usable=%d',
                 log_id, total_tokens, method, trigger_threshold,
                 context_limit, usable)

    if total_tokens > trigger_threshold:
        logger.info('[Compact] Force-compact TRIGGERED  conv=%s  '
                    'tokens=%d (via %s) > threshold=%d  '
                    '(limit=%d, usable=%d, ratio=%.0f%%)',
                    log_id, total_tokens, method, trigger_threshold,
                    context_limit, usable,
                    _SUMMARY_TRIGGER_RATIO * 100)
        return True

    return False
