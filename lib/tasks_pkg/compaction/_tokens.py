"""Token estimation + context-limit decision helpers.

Pure functions of ``messages`` + ``task`` — no side effects, no DB, no LLM.
That makes this the cleanest target for unit tests, and the safest module
for the orchestrator to import for "should I trigger force-compact?"
decisions.

Imports nothing from sibling sub-modules except ``_constants``.
"""

import base64
import binascii
import hashlib
import math
import re
import struct
import time
from collections import OrderedDict
from contextlib import nullcontext

from lib.conversation_sync.settled_rows import SettledDict, memo_get, memo_put
from lib.log import get_logger
from lib.tasks_pkg.compaction._constants import (
    _AUTO_COMPACT_MIN_PAYBACK_ROUNDS,
    _COMPACTION_RESERVE,
    _cooldown_lock,
    _DEFAULT_CONTEXT_LIMIT,
    _IMAGE_TOKENS_DEFAULT,
    _IMAGE_TOKENS_LOW,
    _OUTPUT_RESERVE,
    _SUMMARY_COOLDOWN,
    _SUMMARY_TRIGGER_RATIO,
    _summary_cooldowns,
    heuristic_floor_max_ratio,
)

logger = get_logger(__name__)

_TASK_TOOL_SCHEMA = object()
_SETTLED_MESSAGE_ESTIMATE_KEY = ('compaction.message-estimate',)

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


# Claude vision cost (https://docs.claude.com/en/docs/build-with-claude/vision):
# tokens ~= width * height / 750, after an image is scaled down (aspect ratio
# preserved) to a long edge of at most 1568 px and at most ~1.15 megapixels.
_CLAUDE_IMAGE_MAX_EDGE_PX = 1568
_CLAUDE_IMAGE_MAX_PIXELS = 1_150_000
_CLAUDE_IMAGE_PIXELS_PER_TOKEN = 750
# JPEG start-of-frame markers carry the frame size (DHT/JPG/DAC excluded).
_JPEG_SOF_MARKERS = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


def _data_url_image_size(url) -> tuple[int, int] | None:
    """Read ``(width, height)`` from a base64 image data URL's header.

    Decodes only the few base64 quanta that hold the PNG IHDR, GIF screen
    descriptor, WebP VP8/VP8L/VP8X header or JPEG SOFn frame header (JPEG
    segments before the frame are skipped by their declared lengths), never
    the image body. Anything else returns None.
    """
    if not isinstance(url, str) or not url.startswith('data:'):
        return None
    comma = url.find(',')
    if comma < 0 or ';base64' not in url[:comma]:
        return None
    start = comma + 1

    def read(offset: int, size: int) -> bytes:
        first = start + (offset // 3) * 4
        last = start + ((offset + size + 2) // 3) * 4
        try:
            raw = base64.b64decode(url[first:last])
        except (binascii.Error, ValueError):
            return b''
        return raw[offset % 3:offset % 3 + size]

    head = read(0, 30)
    size = None
    if (head[:8] == b'\x89PNG\r\n\x1a\n' and head[12:16] == b'IHDR'
            and len(head) >= 24):
        size = struct.unpack('>II', head[16:24])
    elif head[:6] in (b'GIF87a', b'GIF89a') and len(head) >= 10:
        size = struct.unpack('<HH', head[6:10])
    elif head[:4] == b'RIFF' and head[8:12] == b'WEBP' and len(head) == 30:
        chunk = head[12:16]
        if chunk == b'VP8X':
            size = (1 + int.from_bytes(head[24:27], 'little'),
                    1 + int.from_bytes(head[27:30], 'little'))
        elif chunk == b'VP8L':
            bits = int.from_bytes(head[21:25], 'little')
            size = ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
        elif chunk == b'VP8 ':
            size = (int.from_bytes(head[26:28], 'little') & 0x3FFF,
                    int.from_bytes(head[28:30], 'little') & 0x3FFF)
    elif head[:2] == b'\xff\xd8':
        offset = 2
        while size is None:
            segment = read(offset, 9)
            if len(segment) < 4 or segment[0] != 0xFF:
                return None
            marker = segment[1]
            if marker == 0xFF:  # fill byte before a marker
                offset += 1
            elif marker in _JPEG_SOF_MARKERS:
                if len(segment) < 9:
                    return None
                height, width = struct.unpack('>HH', segment[5:9])
                size = (width, height)
            else:
                length = int.from_bytes(segment[2:4], 'big')
                if marker in (0xD9, 0xDA) or length < 2:
                    return None  # end of image / scan data before any frame header
                offset += 2 + length
    if size is None or size[0] <= 0 or size[1] <= 0:
        return None
    return size


def claude_image_tokens(block: dict, model: str) -> int | None:
    """Tokens Claude bills for one ``image_url`` block, from its pixel size.

    None when ``model`` is not a Claude model or the size cannot be read from
    a data-URL header; callers then keep their existing flat estimate.
    """
    if not model:
        return None
    from lib.model_info import is_claude
    if not is_claude(model):
        return None
    image_url = block.get('image_url')
    size = _data_url_image_size(
        image_url.get('url') if isinstance(image_url, dict) else image_url)
    if size is None:
        return None
    width, height = size
    scale = min(1.0, _CLAUDE_IMAGE_MAX_EDGE_PX / max(width, height),
                math.sqrt(_CLAUDE_IMAGE_MAX_PIXELS / (width * height)))
    return max(1, math.ceil(
        width * height * scale * scale / _CLAUDE_IMAGE_PIXELS_PER_TOKEN))


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

    A settled row is deep-immutable, so its estimate lives in its memo slot.
    """
    if type(msg) is SettledDict:
        estimate = memo_get(msg, _SETTLED_MESSAGE_ESTIMATE_KEY)
        if estimate is None:
            estimate = memo_put(msg, _SETTLED_MESSAGE_ESTIMATE_KEY,
                                _estimate_message_tokens(msg))
        return estimate
    return _estimate_message_tokens(msg)


def _estimate_message_tokens(msg: dict) -> int:
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


def estimate_msg_tokens_for_model(msg: dict, model: str = '') -> int:
    """``_estimate_msg_tokens`` with Claude images priced by pixel size.

    The shared estimator has no route, so every image uses the flat estimate.
    For a Claude ``model`` each readable image is swapped for
    ``claude_image_tokens``; other routes return the shared estimate.
    """
    tokens = _estimate_msg_tokens(msg)
    for field in ('content', 'reasoning_content'):
        value = msg.get(field)
        if not isinstance(value, list):
            continue
        for block in value:
            if isinstance(block, dict) and block.get('type') == 'image_url':
                billed = claude_image_tokens(block, model)
                if billed is not None:
                    tokens += billed - _image_block_tokens(block)
    return tokens


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


def _task_text_count_memo(task: dict | None):
    """Return the task-owned text-count memo; it dies with the task dict."""
    if task is None:
        return None
    memo = task.get('_requestTextCountMemo')
    if memo is None:
        from lib.token_counter import RequestTextCounts
        memo = task.setdefault('_requestTextCountMemo', RequestTextCounts())
    return memo


def task_round_text_identities(task: dict | None):
    """Scope one loop round: its counts digest each text object once."""
    memo = _task_text_count_memo(task)
    return memo.shared_text_identities() if memo is not None else nullcontext()


def _task_text_count_view(task: dict | None, tool_schema):
    """One count's view of the task memo, named by its tool projection."""
    memo = _task_text_count_memo(task)
    if memo is None:
        return None
    # The task's own tool surfaces are copy-on-write catalog values; prompt
    # admission already reuses their schema count by list identity.
    return memo.view(
        'request' if tool_schema is _TASK_TOOL_SCHEMA else 'admission',
        reuse_tool_schema_identity=True)


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
    The count, its network-tier prefilter and the entropy floor read one
    view of the task memo, so each text is digested once per call and
    tokenized or estimated only when no earlier count saw it.
    """
    view = _task_text_count_view(task, tool_schema)
    with view.estimating() if view is not None else nullcontext():
        return _count_tokens_in_view(
            messages, task, measurement_out=measurement_out,
            tool_schema=tool_schema,
            collect_reusable_text_counts=collect_reusable_text_counts,
            text_count_view=view)


def _count_tokens_in_view(
    messages: list,
    task: dict | None,
    *,
    measurement_out: dict | None,
    tool_schema,
    collect_reusable_text_counts: bool,
    text_count_view,
) -> tuple[int, str]:
    cfg = (task or {}).get('config', {}) or {}
    from lib.llm_dispatch.native_context_scope import native_task_context_profile
    routed = native_task_context_profile(task or {})
    model = (routed.get('model') if routed else None) or cfg.get('model', '') or ''
    context_limit = _get_context_limit(task)
    from lib.tasks_pkg.compaction._ownership import compaction_owner_key
    conv_id = compaction_owner_key(task)
    # Shared assembly stashes the initial admission projection here, separate
    # from provider discovery candidates and execution authority. Include its
    # schema cost at startup; routed admission checks the final provider body.
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
                text_count_memo=text_count_view,
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

    # Ground-truth calibration (see _calibration.py): once the task has
    # observed a real billed input size for this (model, estimator-tier),
    # scale the estimate by the learned ratio. This corrects the *scale*
    # error (0.66x tiktoken under-count, ~10x CJK over-count) that the
    # heuristic floor below can only clamp, not fix. When calibration was
    # applied, the floor is skipped — estimate-vs-estimate clamping would
    # re-introduce the very bias the ratio just removed.
    _raw_estimate = auth_tokens
    _calibrated = False
    from lib.tasks_pkg.compaction._calibration import consume_stream_sample
    consume_stream_sample(task, model=model)
    if _is_estimate:
        from lib.tasks_pkg.compaction._calibration import calibrated_estimate
        auth_tokens, _calibrated = calibrated_estimate(
            task, model=model, method=method, estimate=auth_tokens)
        if _calibrated:
            method = f'{method}+cal'

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
    if _is_estimate and not _calibrated and heuristic_tokens > auth_tokens:
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

    # Provider-measured clamp (DOWN-ONLY). The entropy floor above can only
    # cover tiktoken's under-count; it has no upper bound tied to reality, so an
    # uncalibrated round-1 gate can demand a summary for a prompt the provider
    # already accepted at ~55% of that size (conv mvf9laurcvekgh: gate 908,877
    # vs billed 509,044). A provider's own billed input IS measured evidence for
    # this conversation's message list, and it is recorded on every stream, so
    # the gate no longer has to start each turn blind: calibration needs a
    # second round to learn a ratio, this does not.
    #
    # Soundness: the bound is `measured + estimate(appended suffix)` × (1 +
    # real_anchor_slack()), the slack covering in-place rewrites of the measured
    # prefix (refreshed requirements, restored checklists, tool-result
    # replacement) that the suffix count cannot see. Estimate tiers only: a
    # provider-measured tier is already exact. A missing/stale/rewritten
    # measurement yields None and leaves today's behaviour untouched.
    _anchor_applied = False
    if _is_estimate:
        from lib.tasks_pkg.compaction._calibration import provider_prompt_anchor
        _anchor = provider_prompt_anchor(task, model=model, messages=messages)
        if _anchor is not None:
            from lib.tasks_pkg.compaction._constants import real_anchor_slack
            _anchor_bound = int(_anchor * (1.0 + real_anchor_slack()))
            if _anchor_bound < auth_tokens:
                logger.info('[Compact] gate %d clamped to provider-measured %d '
                            '(measured %d × %.2f slack, via %s)',
                            auth_tokens, _anchor_bound, _anchor,
                            1.0 + real_anchor_slack(), method)
                auth_tokens = _anchor_bound
                _anchor_applied = True
                method = f'{method}+anchor'

    # A historical provider input is not an upper bound on a reconstructed
    # request. Only UsageCacheCounter may reuse it, after validating the full
    # prefix/model/envelope and adding the suffix. A cache miss must never
    # regain authority through an unvalidated conversation/durable anchor.
    if isinstance(measurement_out, dict):
        measurement_out.update({
            'message_tokens': int(message_tokens),
            'message_count': len(messages),
            'gate_tokens': int(auth_tokens),
            'method': method,
            'raw_estimate_tokens': int(_raw_estimate),
            'calibrated': bool(_calibrated),
            'provider_anchored': bool(_anchor_applied),
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
        window = context_profile(
            model, (task or {}).get('provider_id') or '').get('window')
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
    from lib.llm_dispatch.context_policy import task_context_profile
    routed = task_context_profile(task)
    if routed is not None:
        return int(routed['window'])
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
    from lib.llm_dispatch.context_policy import pinned_context_profile
    routed = pinned_context_profile(model, provider_id=provider_id)
    return routed or resolved_context_profile(model or '', provider_id or '')


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
        'working_set_tokens': 0,  # Read-only wire compatibility; no economic setting.
        'min_usable_ratio': _MIN_USABLE_RATIO,
    }


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


def _observed_compaction_cadence_payback_horizon(
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
    # Preserve the existing budget/receipt tuple; automatic L2 has one trigger.
    return window_threshold, window_threshold, 0


def _should_force_compact(
    messages: list,
    task: dict | None = None,
    *,
    measurement_out: dict | None = None,
    current_round: object = None,
    remaining_api_rounds: object = None,
) -> bool:
    """Decide whether force-compact should fire.

    Returns True only when the complete prompt exceeds route-window safety.
    Positive rewrite ROI is not evidence that now beats delaying the rewrite.
    """
    from lib.tasks_pkg.compaction._ownership import compaction_owner_key
    conv_id = compaction_owner_key(task)
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

    calibration_measurement = measurement_out if isinstance(measurement_out, dict) else {}
    total_tokens, method = _count_tokens_authoritative(
        messages, task, measurement_out=calibration_measurement)

    # Feed the ground-truth calibration loop: pair the PREVIOUS round's
    # raw estimate with the provider-measured usage that followed it, then
    # stash this round's raw estimate for the next pairing. Advisory; the
    # gate proceeds on the current count either way.
    if task is not None:
        try:
            from lib.tasks_pkg.compaction._calibration import (
                update_and_stash_sample,
            )
            update_and_stash_sample(
                task,
                model=str(((task.get('config') or {}).get('model')) or ''),
                raw_estimate=int(calibration_measurement.get(
                    'raw_estimate_tokens') or total_tokens),
                method=method,
            )
        except Exception as _cal_exc:
            logger.debug('[Compact] token calibration sample failed: %s',
                         _cal_exc)

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
