"""Token estimation + context-limit decision helpers.

Pure functions of ``messages`` + ``task`` — no side effects, no DB, no LLM.
That makes this the cleanest target for unit tests, and the safest module
for the orchestrator to import for "should I trigger force-compact?"
decisions.

Imports nothing from sibling sub-modules except ``_constants``.
"""

import base64
import hashlib
import math
import re
import time
from collections import OrderedDict

from lib.log import get_logger
from lib.tasks_pkg.compaction._constants import (
    _AUTO_COMPACT_MIN_PAYBACK_ROUNDS,
    _COMPACTION_RESERVE,
    _cooldown_lock,
    _DEFAULT_CONTEXT_LIMIT,
    _DEFAULT_WORKING_SET_TOKENS,
    _IMAGE_TOKENS_DEFAULT,
    _IMAGE_TOKENS_LOW,
    _OUTPUT_RESERVE,
    _SUMMARY_COOLDOWN,
    _SUMMARY_TRIGGER_RATIO,
    _summary_cooldowns,
    heuristic_floor_max_ratio,
    real_anchor_slack,
)

logger = get_logger(__name__)

_WORKING_SET_AUDITED: set[int] = set()
_TASK_TOOL_SCHEMA = object()

_PRICING_TIER_WORKING_SET_MARGIN = 0.90
_COMPACTION_CADENCE_MAX_GAPS = 8
_COMPACTION_CADENCE_MAX_PAYBACK_ROUNDS = 6.0

# ── Multimodal estimate helpers (Codex-inspired, codex-rs utils/audio) ──

_AUDIO_TOKENS_PER_SECOND = 10.0
"""Same constant as codex-rs ``utils/audio::AUDIO_TOKENS_PER_SECOND`` —
duration × 10 is the estimate when the payload decodes."""

_AUDIO_TOKEN_CACHE_SIZE = 32
"""Same bound as codex-rs ``AUDIO_TOKEN_ESTIMATE_CACHE_SIZE``."""

_audio_token_cache: 'OrderedDict[str, int]' = OrderedDict()


def _image_block_tokens(block: dict) -> int:
    """Per-image estimate honoring the OpenAI ``detail`` field when set.

    ``detail='low'`` is billed a flat 85 tokens regardless of resolution;
    everything else keeps the conservative high-detail default. (codex-rs
    additionally patch-counts ``detail='original'`` images — chatui has no
    producer of that detail level, so that branch is deliberately not
    ported: no speculative code for a shape we never emit.)
    """
    image_url = block.get('image_url')
    detail = image_url.get('detail') if isinstance(image_url, dict) else None
    if isinstance(detail, str) and detail.lower() == 'low':
        return _IMAGE_TOKENS_LOW
    return _IMAGE_TOKENS_DEFAULT


def _audio_tokens_uncached(payload_b64: str, fmt: str) -> int:
    """Decode + duration-probe one audio payload (see _estimate_audio_tokens)."""
    try:
        raw = base64.b64decode(payload_b64)
    except Exception as e:
        logger.debug('[Tokens] audio base64 decode failed: %s', e)
        raw = b''
    if raw:
        try:
            from lib.transcription._audio import _probe_duration_s
            mime = fmt if '/' in fmt else f'audio/{fmt or "wav"}'
            duration = _probe_duration_s(raw, mime)
        except Exception as e:
            logger.debug('[Tokens] audio duration probe failed: %s', e)
            duration = None
        if duration:
            return max(1, math.ceil(duration * _AUDIO_TOKENS_PER_SECOND))
    # codex-rs fallback: size heuristic (~4 chars/token over the payload).
    return max(1, len(payload_b64) // 4)


def _estimate_audio_tokens(block: dict) -> int:
    """Estimate an ``input_audio`` content block (codex-rs utils/audio port).

    The base64 payload otherwise slips past the text estimator entirely (it
    is not a ``text`` block), letting minutes of audio count as ZERO tokens
    against the compaction gate. Handles both the OpenAI inline shape
    (``input_audio: {data, format}`` — what lib/transcription emits) and the
    data-URL shape (``audio_url: data:audio/…;base64,…``). Results are
    cached by payload sha1 (same memoization as codex-rs) so the per-round
    estimate never re-parses a multi-MB payload.
    """
    payload = ''
    fmt = ''
    inner = block.get('input_audio')
    if isinstance(inner, dict):
        payload = inner.get('data') or ''
        fmt = inner.get('format') or ''
    else:
        url = block.get('audio_url')
        if isinstance(url, str) and url[:5].lower() == 'data:' and ',' in url:
            meta, payload = url.split(',', 1)
            fmt = meta[5:].split(';')[0].rsplit('/', 1)[-1]
    if not payload:
        return 0
    key = hashlib.sha1(payload.encode('ascii', 'ignore')).hexdigest()
    cached = _audio_token_cache.get(key)
    if cached is not None:
        _audio_token_cache.move_to_end(key)
        return cached
    tokens = _audio_tokens_uncached(payload, fmt)
    _audio_token_cache[key] = tokens
    while len(_audio_token_cache) > _AUDIO_TOKEN_CACHE_SIZE:
        _audio_token_cache.popitem(last=False)
    return tokens


def _estimate_msg_tokens(msg: dict) -> int:
    """Rough token estimate for a single message (CJK-aware).

    Uses the shared digest-cached entropy heuristic (1 token per CJK char +
    1 token per dense base64/hex char + 1 token per ~3 other chars) that gates
    the richer counter backends. Reuse changes cost, never the estimate.

    For a bit-exact authoritative count (via tiktoken / Anthropic
    count_tokens / HF tokenizer), callers should use
    ``_count_tokens_authoritative()`` below.

    Images: fixed estimate per image (NOT base64 length) — the LLM API
    processes images natively and charges ~85-1105 tokens regardless of
    the data-URL size. ``detail='low'`` images use the low flat rate.

    Audio (``input_audio`` blocks): duration-based estimate when the WAV
    payload decodes (10 tokens/sec, the codex-rs constant), size-heuristic
    fallback otherwise; cached by payload sha1.
    """
    from lib.token_counter import cached_cheap_estimate_text

    text_tokens = 0
    image_tokens = 0
    for field in ('content', 'reasoning_content'):
        val = msg.get(field)
        if not val:
            continue
        if isinstance(val, str):
            text_tokens += cached_cheap_estimate_text(val, reusable=True)
        elif isinstance(val, list):
            for block in val:
                if isinstance(block, dict):
                    if block.get('type') == 'text':
                        text_tokens += cached_cheap_estimate_text(
                            block.get('text', ''), reusable=True)
                    elif block.get('type') == 'image_url':
                        image_tokens += _image_block_tokens(block)
                    elif block.get('type') == 'input_audio':
                        # Duration-based when decodable — see
                        # _estimate_audio_tokens (previously ZERO tokens,
                        # letting audio blow past the compaction gate).
                        image_tokens += _estimate_audio_tokens(block)
    for tc in msg.get('tool_calls', []):
        text_tokens += cached_cheap_estimate_text(
            tc.get('function', {}).get('arguments', ''), reusable=True)
    return text_tokens + image_tokens


def _estimate_total_tokens(messages: list) -> int:
    """Sum per-message CJK-aware estimates. Fast — never networks."""
    return sum(_estimate_msg_tokens(m) for m in messages)


def _fallback_tool_schema_tokens(tools, *, model: str) -> int:
    """Keep the full-request contract when the canonical counter degrades."""
    if not tools:
        return 0
    try:
        from lib.context_telemetry import tool_schema_tokens

        return max(0, int(tool_schema_tokens(tools, model=model) or 0))
    except Exception as exc:
        logger.warning(
            '[Compact] tool-schema fallback failed; request estimate may be '
            'incomplete: %s',
            exc,
        )
        return 0


def _count_tokens_authoritative(
    messages: list,
    task: dict | None = None,
    *,
    measurement_out: dict | None = None,
    tool_schema=_TASK_TOOL_SCHEMA,
    collect_reusable_text_counts: bool = False,
) -> tuple[int, str]:
    """Authoritative token count via ``lib.token_counter.count_tokens``.

    Tries (in order): usage_cache → native count_tokens API →
    exact offline tokenizer (tiktoken / deepseek / HF) → heuristic.

    Returns ``(tokens, method)`` where method is the backend that
    produced the count (``'usage_cache' | 'anthropic_api' | 'tiktoken' | …``).
    """
    cfg = (task or {}).get('config', {}) or {}
    model = cfg.get('model', '') or ''
    context_limit = _get_context_limit(task)
    conv_id = (task or {}).get('convId', '') or ''
    # The orchestrator stashes the live tool schema here (see
    # orchestrator.py `_assemble_tool_list`). It ships in every request and
    # the gateway tokenizes it, so the gate must include it or it
    # under-counts by the whole tool-schema size on tool-heavy configs.
    tools = (
        (task or {}).get('_tool_schema') or None
        if tool_schema is _TASK_TOOL_SCHEMA
        else tool_schema or None
    )

    try:
        from lib.token_counter import count_tokens as _ct_count_tokens
    except Exception as e:
        logger.debug('[Compact] token_counter unavailable, using heuristic: %s', e)
        _ct_count_tokens = None

    message_tokens: int | None = None
    if _ct_count_tokens is None:
        message_tokens = _estimate_total_tokens(messages)
        auth_tokens = message_tokens + _fallback_tool_schema_tokens(
            tools, model=model)
        method = 'heuristic_fallback'
    else:
        try:
            result = _ct_count_tokens(
                messages,
                model=model,
                tools=tools,
                conv_id=conv_id or None,
                context_limit=context_limit,
                measurement_out=(
                    measurement_out
                    if (collect_reusable_text_counts
                        and isinstance(measurement_out, dict))
                    else None
                ),
            )
            auth_tokens = int(result.get('tokens', 0))
            method = str(result.get('method', 'unknown'))
        except Exception as e:
            logger.warning('[Compact] count_tokens call failed, falling back to '
                           'heuristic: %s', e)
            message_tokens = _estimate_total_tokens(messages)
            auth_tokens = message_tokens + _fallback_tool_schema_tokens(
                tools, model=model)
            method = 'heuristic_fallback'

    # Exact tiers are provider/usage-MEASURED — they outrank any estimate or
    # anchor. Everything else (tiktoken / offline tokenizers / heuristic)
    # is an estimate and gets both guards below.
    _base_method = method.split('+', 1)[0]
    _is_estimate = _base_method not in (
        'usage_cache', 'anthropic_api', 'gemini_api')

    # Safety floor for the COMPACTION GATE only (not the UI counter): never
    # let the gate report FEWER tokens than the conservative entropy
    # heuristic would. tiktoken's cl100k vocabulary under-counts Claude's
    # tokenizer on high-entropy content (base64/minified data) — for conv
    # mq7y3irly1r4hu tiktoken gave 0.66x of the gateway while the heuristic
    # gave 0.83x. A gate that trusts the lower number can let an oversized
    # prompt slip past the trigger into the fatal reactive path. Taking the
    # max keeps the gate on the safe side regardless of which backend wins,
    # while the UI still gets the accuracy-optimized count from count_tokens.
    #
    # BOUNDED (2026-08-01, ): the floor exists ONLY to
    #   cover tiktoken's proven 0.66x under-count. Left unbounded it inverts
    #   into a systematic OVER-count on CJK-heavy content (1 token per CJK
    #   char + accumulated reasoning_content) — measured ~10x on
    #   conv=mrxinirv0t6n6v (gate 2,198,193 vs real prompt 215,552 →
    #   force-compact fired at ~22% real window usage). Cap the floor at
    #   heuristic_floor_max_ratio() × the estimate-tier count.
    if message_tokens is None:
        message_tokens = _estimate_total_tokens(messages)
    heuristic_tokens = message_tokens
    if _is_estimate and heuristic_tokens > auth_tokens:
        if auth_tokens > 0:
            _ratio = heuristic_floor_max_ratio()
            _floor_cap = int(auth_tokens * _ratio)
            if heuristic_tokens > _floor_cap:
                logger.info('[Compact] heuristic floor %d capped at %.2f×%d=%d '
                            '(via %s) — CJK over-count guard',
                            heuristic_tokens, _ratio, auth_tokens,
                            _floor_cap, method)
                heuristic_tokens = _floor_cap
        logger.debug('[Compact] heuristic floor %d > authoritative %d (via %s) '
                     '— using floor for gate', heuristic_tokens, auth_tokens, method)
        auth_tokens = heuristic_tokens
        method = f'{method}+heuristic_floor'

    # REAL-anchor clamp (estimate tiers only; 2026-08-01, ).
    #   When no exact tier could validate (task cold start / the message list
    #   was just REWRITTEN by a compaction — the usage_cache signature never
    #   matches again), estimates are the only input, and on CJK-heavy
    #   content BOTH can sit an order of magnitude above reality. Clamp the
    #   count DOWN to the conversation's last provider-MEASURED prompt ×
    #   (1 + slack). Down-only: over-triggering destroys context lossily and
    #   irreversibly, while under-triggering is bounded by the next round's
    #   fresh usage recording and the L3 reactive net.
    if _is_estimate:
        _anchor, _anchor_src = 0, 'none'
        try:
            from lib.tasks_pkg.compaction._real_anchor import real_prompt_anchor
            _anchor, _anchor_src = real_prompt_anchor(conv_id, task)
        except Exception as _ae:
            logger.debug('[Compact] real anchor lookup failed: %s', _ae)
        if _anchor > 0:
            _slack = real_anchor_slack()
            _anchor_cap = int(_anchor * (1.0 + _slack))
            if auth_tokens > _anchor_cap:
                logger.info('[Compact] estimate count %d (via %s) clamped to '
                            'real anchor %d × (1+%.2f) = %d (src=%s) conv=%s '
                            '— provider-measured yardstick wins over estimate',
                            auth_tokens, method, _anchor, _slack, _anchor_cap,
                            _anchor_src, conv_id[:8] if conv_id else '?')
                auth_tokens = _anchor_cap
                method = f'{method}+anchor:{_anchor_src}'
    if isinstance(measurement_out, dict):
        measurement_out.update({
            'message_tokens': int(message_tokens),
            'message_count': len(messages),
            'gate_tokens': int(auth_tokens),
            'method': method,
        })
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
    """Operational static window; unknown knowledge gets a safety fallback."""
    model = ((task or {}).get('config', {}) or {}).get('model', '') or ''
    try:
        from lib.model_info import context_profile
        window = context_profile(model).get('window')
        if window is not None:
            return int(window)
    except Exception as e:
        logger.debug('[Compact] model context profile failed: %s', e)
    # Runtime safety only: this value is never exposed as model knowledge.
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


def resolve_model_context_profile(model: str, provider_id: str = '') -> dict:
    """Knowledge profile composed with provider-scoped learned evidence."""
    from lib.model_info import resolved_context_profile
    return resolved_context_profile(model or '', provider_id or '')


def resolve_model_context_limit(model: str, provider_id: str = '') -> int:
    """Backward-compatible operational integer context limit."""
    profile = resolve_model_context_profile(model, provider_id)
    return int(profile['window']) if profile['window'] is not None else _DEFAULT_CONTEXT_LIMIT


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
        ``summary_trigger_ratio``, ``working_set_tokens`` and
        ``min_usable_ratio``.
    """
    return {
        'runtime_fallback_limit': _DEFAULT_CONTEXT_LIMIT,
        'default_limit': None,  # compatibility key; unknown is not model knowledge
        'output_reserve': _OUTPUT_RESERVE,
        'compaction_reserve': _COMPACTION_RESERVE,
        'summary_trigger_ratio': _SUMMARY_TRIGGER_RATIO,
        'working_set_tokens': _working_set_token_limit(None),
        'min_usable_ratio': _MIN_USABLE_RATIO,
    }


def _working_set_token_limit(task: dict | None = None) -> int:
    """Resolve the economic prompt working-set ceiling.

    A per-request ``config.compaction.workingSetTokens`` override wins over
    ``TOFU_WORKING_CONTEXT_TOKENS``.  Zero disables the economic ceiling.
    Without an explicit override, a provider/model rate card with a proven
    price increase selects 90% of the last cheaper tier. Flat/unknown pricing
    retains the conservative 128K default. Positive values are clamped so a
    typo cannot cause constant tiny-context compactions or remove all bounds.
    """
    import os

    comp_cfg = ((task or {}).get('config', {}) or {}).get('compaction')
    if isinstance(comp_cfg, dict) and str(
            comp_cfg.get('strategy') or 'fixed').lower() == 'adaptive':
        # Filled only after the economic decision that triggered this attempt.
        # Before that decision the hard window remains the sole threshold.
        adaptive = int((task or {}).get(
            '_adaptiveCompactionWorkingSetTokens') or 0)
        return max(32_000, min(2_000_000, adaptive)) if adaptive > 0 else 0
    raw = comp_cfg.get('workingSetTokens') if isinstance(comp_cfg, dict) else None
    if raw is None and 'TOFU_WORKING_CONTEXT_TOKENS' in os.environ:
        raw = os.environ.get('TOFU_WORKING_CONTEXT_TOKENS')
    if raw is None:
        value = _DEFAULT_WORKING_SET_TOKENS
        try:
            from lib.pricing import first_pricing_increase_boundary
            cfg = (task or {}).get('config', {}) or {}
            model = str(cfg.get('model') or (task or {}).get('model') or '')
            provider_id = str((task or {}).get('provider_id') or '')
            boundary = first_pricing_increase_boundary(
                model, provider_id or None) if model else None
            if boundary:
                value = int(
                    int(boundary['maxPromptTokens'])
                    * _PRICING_TIER_WORKING_SET_MARGIN)
        except Exception as exc:
            logger.debug('[Compact] pricing-tier working-set lookup failed: %s',
                         exc)
        return max(32_000, min(2_000_000, value))
    try:
        value = int(raw)
    except (TypeError, ValueError) as e:
        logger.debug('[Compact] invalid workingSetTokens=%r (%s) — using %d',
                     raw, e, _DEFAULT_WORKING_SET_TOKENS)
        value = _DEFAULT_WORKING_SET_TOKENS
    if value <= 0:
        return 0
    return max(32_000, min(2_000_000, value))


def _record_compaction_cadence(
    task: dict | None,
    current_round: object = None,
) -> int | None:
    """Record one successful prefix rewrite and its task-local round gap.

    State is deliberately bounded and lives on the request task, never in a
    process global. A second rewrite in the same round is idempotent. The
    observed gaps predict how many cache-read rounds the next rewritten prefix
    is likely to survive; total task age is not evidence for that lifetime.
    """
    if not isinstance(task, dict):
        return None
    try:
        round_num = int(current_round)
    except (TypeError, ValueError, OverflowError):
        return None
    if round_num < 0:
        return None
    previous_raw = task.get('_compactionCadenceLastRound')
    try:
        previous = int(previous_raw) if previous_raw is not None else None
    except (TypeError, ValueError, OverflowError):
        previous = None
    gap = None
    if previous is not None and round_num > previous:
        gap = round_num - previous
        gaps = task.setdefault('_compactionCadenceRoundGaps', [])
        if not isinstance(gaps, list):
            gaps = []
            task['_compactionCadenceRoundGaps'] = gaps
        gaps.append(gap)
        if len(gaps) > _COMPACTION_CADENCE_MAX_GAPS:
            del gaps[:-_COMPACTION_CADENCE_MAX_GAPS]
    if previous is None or round_num > previous:
        task['_compactionCadenceLastRound'] = round_num
    return gap


def _fixed_compaction_cadence_payback_horizon(
    task: dict | None,
    current_round: object = None,
    *,
    remaining_api_rounds: object = None,
) -> float:
    """Return a bounded ROI horizon from observed compaction-window cadence.

    The conservative prediction is the shortest of the recent successful
    rewrite gaps and the current window's already-observed age. A task that is
    100 rounds old but rewrites its prefix every three rounds therefore gets a
    three-round horizon, not the old six-round task-survival allowance.
    """
    minimum = max(0.0, float(_AUTO_COMPACT_MIN_PAYBACK_ROUNDS))
    try:
        round_num = max(0, int(current_round or 0))
    except (TypeError, ValueError, OverflowError):
        round_num = 0

    observations: list[int] = []
    if isinstance(task, dict):
        raw_gaps = task.get('_compactionCadenceRoundGaps')
        if isinstance(raw_gaps, list):
            for raw_gap in raw_gaps[-4:]:
                try:
                    gap = int(raw_gap)
                except (TypeError, ValueError, OverflowError):
                    continue
                if gap > 0:
                    observations.append(gap)
        previous_raw = task.get('_compactionCadenceLastRound')
        try:
            previous = (int(previous_raw)
                        if previous_raw is not None else None)
        except (TypeError, ValueError, OverflowError):
            previous = None
        if previous is not None and round_num > previous:
            observations.append(round_num - previous)

    horizon = minimum
    if observations:
        horizon = max(minimum, min(
            _COMPACTION_CADENCE_MAX_PAYBACK_ROUNDS,
            float(min(observations)),
        ))

    if remaining_api_rounds is not None:
        try:
            remaining = max(0, int(remaining_api_rounds))
        except (TypeError, ValueError, OverflowError):
            remaining = None
        if remaining is not None:
            horizon = min(horizon, max(minimum, float(remaining)))
    return horizon


def _compaction_trigger_threshold(
    task: dict | None = None,
    *,
    context_limit: int | None = None,
) -> tuple[int, int, int]:
    """Return ``(effective, window_safety, economic_working_set)`` thresholds."""
    if context_limit is None:
        context_limit = _get_context_limit(task)
    usable = _usable_context(context_limit)
    window_threshold = int(usable * _SUMMARY_TRIGGER_RATIO)
    working_set = _working_set_token_limit(task)
    effective = (min(window_threshold, working_set)
                 if working_set > 0 else window_threshold)
    return effective, window_threshold, working_set


def _audit_working_set_once(working_set: int) -> None:
    """Record each effective working-set tuning value once per process."""
    if working_set <= 0 or working_set in _WORKING_SET_AUDITED:
        return
    _WORKING_SET_AUDITED.add(working_set)
    try:
        from lib.log import audit_log
        audit_log(
            'config_change',
            change='economic_context_working_set',
            working_set_tokens=working_set,
            previous='context-window-only',
            reason='bound repeated agent-loop input cost and latency',
            approved_by='user',
        )
    except Exception as e:
        logger.debug('[Compact] working-set config audit skipped: %s', e)


def _adaptive_compaction_economics(messages: list, task: dict | None, *,
                                   total_tokens: int,
                                   window_threshold: int) -> dict:
    """Make one observable Kimi-priced early-compaction decision."""
    task = task if isinstance(task, dict) else {}
    cfg = task.get('config') or {}
    comp_cfg = cfg.get('compaction') or {}
    inputs = task.get('_adaptiveCompactionInputs') or {}
    hot_tokens = _estimate_total_tokens(list(messages or [])[-12:])
    cold_tokens = max(0, int(total_tokens) - hot_tokens)

    def bounded_number(name: str, default: float, low: float,
                       high: float) -> float:
        try:
            value = float(inputs.get(name, comp_cfg.get(name, default)))
        except (TypeError, ValueError, OverflowError):
            value = default
        if not math.isfinite(value):
            value = default
        return max(low, min(high, value))

    cache_ratio = bounded_number(
        'cacheReadRatio', float(task.get('_lastCacheReadRatio') or 0.8),
        0.0, 1.0)
    remaining_rounds = bounded_number('remainingRoundsMedian', 6.0, 0.0, 200.0)
    evidence_loss = bounded_number(
        'historicalEvidenceLossRate', 0.01, 0.0, 1.0)
    summary_input = int(bounded_number(
        'summaryInputTokens', min(cold_tokens, 32_000), 0, 1_000_000))
    summary_output = int(bounded_number(
        'summaryOutputTokens', 2_000, 0, 128_000))
    input_rate = 2.76
    output_rate = 13.81
    cache_read_multiplier = 0.10
    pricing_source = 'kimi_k3_frozen_fallback'
    try:
        from lib.pricing import lookup_pricing
        pricing = lookup_pricing(
            str(cfg.get('model') or task.get('model') or ''),
            task.get('provider_id') or None)
        if pricing:
            input_rate = max(0.0, float(pricing.get('input') or input_rate))
            output_rate = max(0.0, float(pricing.get('output') or output_rate))
            cache_read_multiplier = max(
                0.0, float(pricing.get('cacheReadMul', cache_read_multiplier)))
            pricing_source = str(pricing.get('_pricingSource') or 'resolved_price')
    except Exception as exc:
        logger.debug('[Compact] adaptive pricing lookup failed: %s', exc)
    blended_input_rate = input_rate * (
        (1.0 - cache_ratio) + cache_ratio * cache_read_multiplier)
    savings_per_round = cold_tokens * blended_input_rate / 1_000_000
    compaction_cost = (
        summary_input * input_rate + summary_output * output_rate) / 1_000_000
    gross_savings = savings_per_round * remaining_rounds
    risk_penalty = gross_savings * min(1.0, evidence_loss * 5.0)
    net_savings = gross_savings - compaction_cost - risk_penalty
    economic_floor = int(bounded_number(
        'minimumColdTokens', 32_000, 32_000, 256_000))
    should_trigger = bool(
        total_tokens >= 64_000 and cold_tokens >= economic_floor
        and remaining_rounds >= 1 and net_savings > 0)
    result = {
        'strategy': 'adaptive_v2',
        'totalTokens': int(total_tokens),
        'hotTokens': int(hot_tokens),
        'coldTokens': int(cold_tokens),
        'windowThreshold': int(window_threshold),
        'cacheReadRatio': cache_ratio,
        'remainingRoundsMedian': remaining_rounds,
        'summaryInputTokens': summary_input,
        'summaryOutputTokens': summary_output,
        'historicalEvidenceLossRate': evidence_loss,
        'savingsPerRemainingRoundUsd': round(savings_per_round, 9),
        'compactionCostUsd': round(compaction_cost, 9),
        'evidenceRiskPenaltyUsd': round(risk_penalty, 9),
        'projectedNetSavingsUsd': round(net_savings, 9),
        'pricingSource': pricing_source,
        'shouldTrigger': should_trigger,
        'reason': ('positive_expected_value' if should_trigger else
                   'insufficient_expected_value'),
    }
    task['_adaptiveCompactionDecision'] = result
    if should_trigger:
        task['_adaptiveCompactionWorkingSetTokens'] = max(
            32_000, min(int(total_tokens), int(window_threshold)))
    return result


def _should_force_compact(
    messages: list,
    task: dict | None = None,
    *,
    measurement_out: dict | None = None,
    current_round: object = None,
    remaining_api_rounds: object = None,
) -> bool:
    """Decide whether force-compact should fire.

    Returns True when estimated token count exceeds the lower of the model's
    context-safety threshold and the economic working-set ceiling.
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
    trigger_threshold, window_threshold, working_set = (
        _compaction_trigger_threshold(task, context_limit=context_limit))
    if working_set > 0 and trigger_threshold < window_threshold:
        _audit_working_set_once(working_set)

    total_tokens, method = _count_tokens_authoritative(
        messages, task, measurement_out=measurement_out)

    # Public vendor APIs can own the economic working-set compaction at the
    # exact rendered-token boundary (including opaque reasoning state that the
    # local message projection cannot inspect). Keep local L1 above this gate,
    # but defer lossy local L2 until the model-window safety threshold. Manual
    # compaction and the reactive prompt-too-long path do not call this policy
    # branch and remain available.
    if ((task or {}).get('_nativeCompactionPrimary')
            and total_tokens <= window_threshold):
        logger.debug('[Compact] native-primary defer conv=%s mode=%s '
                     'tokens=%d <= hard_window=%d',
                     log_id, (task or {}).get('_nativeCompactionMode') or '?',
                     total_tokens, window_threshold)
        return False

    comp_cfg = ((task or {}).get('config', {}) or {}).get('compaction')
    adaptive = (isinstance(comp_cfg, dict)
                and str(comp_cfg.get('strategy') or '').lower() == 'adaptive')
    if adaptive:
        # The context-window safety gate is never bypassed by economics.
        if total_tokens > window_threshold:
            logger.info('[Compact] adaptive hard-window trigger conv=%s '
                        'tokens=%d > %d', log_id, total_tokens,
                        window_threshold)
            return True
        decision = _adaptive_compaction_economics(
            messages, task, total_tokens=total_tokens,
            window_threshold=window_threshold)
        logger.info('[Compact] adaptive decision conv=%s trigger=%s cold=%d '
                    'net_usd=%.6f reason=%s', log_id,
                    decision['shouldTrigger'], decision['coldTokens'],
                    decision['projectedNetSavingsUsd'], decision['reason'])
        return bool(decision['shouldTrigger'])

    # A proactive candidate that was recently declined as low-yield or
    # cache-negative records a message-token retry floor. Do not reconsider
    # the identical hot tail every round; wait for meaningful prompt growth.
    # Window safety always wins, so a request approaching the actual context
    # ceiling bypasses this economic hysteresis immediately.
    retry_after = int((task or {}).get('_autoCompactRetryAfterTokens') or 0)
    if retry_after > 0 and total_tokens < window_threshold:
        message_tokens = int(
            (measurement_out or {}).get('message_tokens') or 0)
        if message_tokens <= 0:
            # Compatibility fallback for tests/extensions that replace the
            # authoritative counter without honoring its optional out-param.
            message_tokens = _estimate_total_tokens(messages)
        retry_witness_invalidated = False
        witness = (task or {}).get('_autoCompactRetryWitness')
        if (message_tokens < retry_after and isinstance(witness, dict)
                and witness.get('reason') == 'cache_negative'):
            try:
                witnessed_payback_limit = float(
                    witness.get('paybackLimitRounds')
                    or _AUTO_COMPACT_MIN_PAYBACK_ROUNDS)
            except (TypeError, ValueError, OverflowError):
                witnessed_payback_limit = float(
                    _AUTO_COMPACT_MIN_PAYBACK_ROUNDS)
            current_payback_limit = (
                _fixed_compaction_cadence_payback_horizon(
                    task,
                    current_round,
                    remaining_api_rounds=remaining_api_rounds,
                )
            )
            if current_payback_limit > witnessed_payback_limit:
                retry_witness_invalidated = True
                logger.debug(
                    '[Compact] conv=%s payback horizon advanced: %.1f > %.1f; '
                    'reconsidering before retry token floor',
                    log_id, current_payback_limit,
                    witnessed_payback_limit)
            witnessed_cache_read = int(
                witness.get('cacheReadTokens') or 0)
            if (not retry_witness_invalidated
                    and witnessed_cache_read > 0 and conv_id):
                try:
                    from lib.tasks_pkg.cache_tracking._state import (
                        get_warm_cache_read,
                    )
                    from lib.tasks_pkg.manager import task_user_id
                    current_cache_read = int(get_warm_cache_read(
                        conv_id, user_id=task_user_id(task)) or 0)
                    retry_witness_invalidated = (
                        current_cache_read < witnessed_cache_read)
                    if retry_witness_invalidated:
                        logger.debug(
                            '[Compact] conv=%s cache witness cooled: %d < %d; '
                            'reconsidering before retry token floor',
                            log_id, current_cache_read,
                            witnessed_cache_read)
                except Exception as exc:
                    logger.debug(
                        '[Compact] conv=%s retry cache witness unavailable: %s',
                        log_id, exc)
        if message_tokens < retry_after and not retry_witness_invalidated:
            logger.debug(
                '[Compact] conv=%s proactive retry deferred: messages=%d '
                '< retry_after=%d (authoritative=%d, window=%d)',
                log_id, message_tokens, retry_after, total_tokens,
                window_threshold)
            return False
        if task is not None:
            task.pop('_autoCompactRetryAfterTokens', None)
            task.pop('_autoCompactRetryWitness', None)

    logger.debug('[Compact] conv=%s  tokens=%d (via %s)  threshold=%d  '
                 'window_threshold=%d working_set=%d limit=%d usable=%d',
                 log_id, total_tokens, method, trigger_threshold,
                 window_threshold, working_set, context_limit, usable)

    if total_tokens > trigger_threshold:
        logger.info('[Compact] Force-compact TRIGGERED  conv=%s  '
                    'tokens=%d (via %s) > threshold=%d  '
                    '(limit=%d, usable=%d, window_threshold=%d, '
                    'working_set=%d, ratio=%.0f%%)',
                    log_id, total_tokens, method, trigger_threshold,
                    context_limit, usable, window_threshold, working_set,
                    _SUMMARY_TRIGGER_RATIO * 100)
        return True

    return False
