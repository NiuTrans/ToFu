# HOT_PATH — functions in this module are called per-request.
"""lib/model_info.py — Model family detection and per-model token limit management.

Extracted from lib/llm_client.py to reduce module size and improve reusability.
All public names are re-exported from llm_client.py for backward compatibility.

Contains:
  • Model family detection helpers (is_claude, is_qwen, is_gemini, etc.)
  • Per-model max output token limits (_MODEL_MAX_OUTPUT)
  • Auto-learned model limits (_learn_model_limit, _load_learned_limits)
  • Token limit error parsing (_parse_token_limit_from_error)
"""

import json
import os
import re
import threading

from lib.log import get_logger

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════
#  Model Detection Helpers
# ══════════════════════════════════════════════════════════

def is_claude(model: str) -> bool:
    """Anthropic Claude models (including AWS/GCP-prefixed variants)."""
    m = model.lower()
    return 'claude' in m or 'anthropic' in m


def is_claude_opus_47(model: str) -> bool:
    """Claude Opus 4.7+ (AWS-gateway / Bedrock / direct-API aliases).

    Opus 4.7 introduced breaking changes vs 4.6:
      • Sampling params (temperature/top_p/top_k) are silently ignored.
      • thinking.budget_tokens removed — only thinking.type='adaptive'.
      • Thinking content is HIDDEN by default — must send
        thinking.display='summarized' to surface the reasoning trace.
      • New 'xhigh' effort level between 'high' and 'max'.

    Detects these id variants:
      - claude-opus-4-7
      - aws.claude-opus-4.7
      - us.anthropic.claude-opus-4-7-v1:0
      - (future) claude-opus-4-8, etc.
    """
    m = model.lower()
    if 'claude' not in m and 'anthropic' not in m:
        return False
    # Extract (major, minor) from "opus-X-Y" or "opus-X.Y" — returns True iff
    # (major, minor) >= (4, 7).  Handles opus-4-7, opus-4.8, opus-5-0, etc.
    match = re.search(r'opus[-_.]?(\d+)[-_.](\d+)', m)
    if not match:
        return False
    major, minor = int(match.group(1)), int(match.group(2))
    return (major, minor) >= (4, 7)


def is_longcat(model: str) -> bool:
    """Internal LongCat models (Flash, MoE, etc.)."""
    return 'longcat' in model.lower()


def is_qwen(model: str) -> bool:
    """Alibaba Qwen models (including qwq/qvq reasoning variants)."""
    m = model.lower()
    return 'qwen' in m or 'qwq' in m or 'qvq' in m


def is_gemini(model: str) -> bool:
    """Google Gemini models."""
    return 'gemini' in model.lower()


def is_minimax(model: str) -> bool:
    """MiniMax models (M2, M2.5, M2.7, M2-her, etc.)."""
    m = model.lower()
    return 'minimax' in m or m == 'm2-her'


def is_doubao(model: str) -> bool:
    """ByteDance Doubao / Seed models."""
    m = model.lower()
    return 'doubao' in m or 'seed' in m


def is_glm(model: str) -> bool:
    """Zhipu GLM models (GLM-4, GLM-5, etc.)."""
    return 'glm' in model.lower()


def is_kimi(model: str) -> bool:
    """Moonshot Kimi models (kimi-k2, kimi-k2.5, kimi-k2.6, moonshot-v1, etc.)."""
    m = model.lower()
    return 'kimi' in m or 'moonshot' in m


def is_ernie(model: str) -> bool:
    """Baidu ERNIE models (ERNIE-5.0, ERNIE-X1, ERNIE-4.5, etc.)."""
    return 'ernie' in model.lower()


def is_gpt(model: str) -> bool:
    """OpenAI GPT models (gpt-4, gpt-4.1, gpt-4o, etc.)."""
    return 'gpt' in model.lower()



# ══════════════════════════════════════════════════════════
#  Continue / Resume capability probes
# ══════════════════════════════════════════════════════════
#
# What each provider's API actually accepts when replaying an interrupted
# assistant turn (tool_calls already made, results available):
#
#   Provider            | tool_use replay | thinking replay         | Prefill
#   --------------------+-----------------+-------------------------+---------
#   Anthropic (Claude)  | required        | thinking{} block with   | NO
#                       |                 |   signature — mandatory |
#                       |                 |   when tools were used  |
#                       |                 |   and extended-thinking |
#                       |                 |   is on                 |
#   Gemini (openai cpt) | required        | extra_content.google.   | tolerated
#                       |                 |   thought_signature on  |
#                       |                 |   each tool_call        |
#   OpenAI / DeepSeek / | standard        | reasoning_content is    | tolerated
#   Qwen / GLM / Kimi / | tool_calls +    | NOT re-accepted (o1/o3  | (non-lossless —
#   Doubao / MiniMax    | tool role msgs  | strip it server-side)   |  assistant turn
#   ERNIE / LongCat     |                 |                         |  just gets appended)
#
# Anthropic's Messages API refuses a final `assistant` turn used as a
# prefill for free text — the conversation must end on `user` or `tool`.
# That's why "Continue" can NEVER be truly lossless against Claude for
# free-form text written between tool batches.  For thinking replay it
# CAN be lossless as long as we echo back the opaque `signature`.

def model_requires_thinking_signature_replay(model: str) -> bool:
    """True if this model's API requires echoing back the thinking block with
    its opaque ``signature`` when replaying an assistant turn that made tool
    calls.

    Applies to Anthropic Claude models in extended-thinking mode.  Gating
    callers on this keeps the thinking-block payload off requests that would
    reject it (e.g. vanilla OpenAI chat/completions strips vendor fields).
    """
    # Claude is currently the only family whose API ties thinking continuity
    # to a signed opaque block.  Opus 4.7+ hides thinking by default but STILL
    # requires the signature replay for tool-use continuity.
    return is_claude(model)


def model_requires_thought_signature_on_tool_calls(model: str) -> bool:
    """True if this model's API requires ``extra_content.google.thought_signature``
    on each replayed tool_call entry (Gemini 3.x via OpenAI-compat proxy).

    See memory ``gemini-thought-signature-openai-compat`` — omitting this
    field on a subsequent request returns HTTP 400.
    """
    return is_gemini(model)


def model_supports_assistant_prefill(model: str) -> bool:
    """True if the API tolerates a trailing ``role='assistant'`` message as a
    prefill / forced continuation.

    Anthropic Messages API rejects this with HTTP 400 ("This model does not
    support assistant message prefill. The conversation must end with a user
    message."), so Claude is excluded.  All OpenAI-compatible endpoints we
    ship with currently accept it (the server just concatenates the prefill
    token-for-token), but the continuation is non-lossless: the model may
    or may not honour the prefill and cannot recover mid-token decoder
    state.  Callers should document this as best-effort, not exact.
    """
    return not is_claude(model)


def model_supports_vision(model: str) -> bool:
    """Check whether *model* supports vision (image_url content blocks).

    Lookup order:
      1. Active dispatch slots (runtime state — includes benchmark updates).
      2. DEFAULT_SLOT_CONFIGS (static reference table).
      3. Discovery _VISION_PAT regex (name-based heuristic fallback).

    When in doubt (unknown model, no slot data), defaults to True to avoid
    stripping images from models we don't know about yet.
    """
    # ── 1. Check active dispatcher slots (runtime state) ──
    try:
        from lib.llm_dispatch.factory import get_dispatcher
        dispatcher = get_dispatcher()
        for slot in dispatcher.slots:
            if slot.model == model:
                return 'vision' in slot.capabilities
    except Exception as e:
        logger.debug('[ModelInfo] Could not check dispatcher for vision cap: %s', e)

    # ── 2. Check static DEFAULT_SLOT_CONFIGS ──
    from lib.llm_dispatch.config import DEFAULT_SLOT_CONFIGS
    slot_cfg = DEFAULT_SLOT_CONFIGS.get(model)
    if slot_cfg:
        return 'vision' in slot_cfg.get('caps', set())

    # ── 3. Fallback: name-based heuristic ──
    from lib.llm_dispatch.discovery import _VISION_PAT
    if _VISION_PAT.search(model):
        return True

    # Unknown model — default to True (don't strip images from unknown models)
    logger.debug('[ModelInfo] Unknown model %s — defaulting vision=True', model)
    return True


# ── Per-model max output token limits ──
# If the API rejects max_tokens > N, list the model family here.
# build_body() will clamp automatically.

def _qwen_max_output(model: str) -> int:
    """Return the max output token limit for a specific Qwen model.

    DashScope enforces strict per-model max_tokens limits:
      - qwen-turbo:    16,384
      - qwen-plus:     32,768
      - qwen3.5-plus:  32,768
      - qwen3.6-plus:  32,768
      - qwen3.5-flash: 32,768
      - qwen-max:      32,768
      - qwen3-max:     32,768
      - qwen3-vl-*:    32,768
      - qwq-plus:      65,536  (reasoning model)
      - qvq-max/plus:  32,768  (visual reasoning)
      - qwen3-coder-*: 65,536
      - qwen-long:     16,384
      - Default:       16,384  (safe minimum for unknown variants)
    """
    m = model.lower()
    # Reasoning models — higher limits
    if 'qwq' in m:
        return 65536
    # Visual reasoning models (QVQ)
    if 'qvq' in m:
        return 32768
    # Coder models — higher limits
    if 'coder' in m:
        return 65536
    # qwen-turbo / qwen3-turbo — lowest limit
    if 'turbo' in m:
        return 16384
    # qwen-plus / qwen3-plus / qwen3.6-plus — medium limit
    if 'plus' in m:
        return 32768
    # qwen-max / qwen3-max — medium limit
    if 'max' in m:
        return 32768
    # qwen-flash / qwen3.5-flash — medium limit
    if 'flash' in m:
        return 32768
    # qwen-vl — medium limit
    if 'vl' in m:
        return 32768
    # Unknown Qwen variant — use safe minimum
    return 16384


def _minimax_max_output(model: str) -> int:
    """Return the max output token limit for a specific MiniMax model.

    M2-her has a strict 2048 max_tokens limit.
    M2/M2.1/M2.5/M2.7 variants support up to 65536.
    """
    if 'her' in model.lower():
        return 2048
    return 65536


def _ernie_max_output(model: str) -> int:
    """Return the max output token limit for a specific Baidu ERNIE model.

    Per Qianfan V2 API model list (2026-04):
      - ERNIE 5.0 / thinking variants:   65,536
      - ERNIE X1.1:                       65,536
      - ERNIE X1 Turbo:                   28,160
      - ERNIE 4.5 Turbo (128k/32k):      12,288
      - ERNIE 4.5 Turbo VL:              16,384
      - ERNIE Speed / Lite (pro-128k):    4,096
      - Default:                          16,384
    """
    m = model.lower()
    if '5.0' in m or 'x1.1' in m:
        return 65536
    if 'x1' in m and 'turbo' in m:
        return 28160
    if 'speed' in m or 'lite' in m:
        return 4096
    if 'vl' in m:
        return 16384
    if '4.5' in m and 'turbo' in m:
        return 12288
    return 16384


def _kimi_max_output(model: str) -> int:
    """Return the max output token limit for a specific Kimi model.

    Moonshot Kimi model limits:
      - kimi-k2.6:                 32,768  (default for K2.6 per docs)
      - kimi-k2.5:                 32,768  (default for K2.5)
      - kimi-k2-turbo-preview:     32,768
      - kimi-k2-thinking-turbo:    32,768
      - kimi-k2-*-preview:         32,768
      - kimi-k2-thinking:          32,768
      - moonshot-v1-*:             16,384  (legacy models)
      - Default:                   32,768
    """
    m = model.lower()
    if 'moonshot-v1' in m:
        return 16384
    return 32768


_MODEL_MAX_OUTPUT = {
    # (checker_fn, limit) — limit can be int or callable(model) → int
    'longcat': (is_longcat, 65536),
    'qwen':    (is_qwen,    _qwen_max_output),  # per-model lookup
    'gemini':  (is_gemini,  65536),
    'minimax': (is_minimax, _minimax_max_output),
    'kimi':    (is_kimi,    _kimi_max_output),    # per-model lookup
    'doubao':  (is_doubao,  16384),
    'ernie':   (is_ernie,   _ernie_max_output),   # per-model lookup
    'gpt':     (is_gpt,     32768),
    'glm':     (is_glm,     131072),

    # Claude: 128000 output limit — matches build_body default, so no clamp needed
}


# ── Auto-learned model limits (persisted to server_config.json) ──────────
_limits_lock = threading.Lock()
_LEARNED_MODEL_LIMITS: dict[str, int] = {}  # model_id → max_tokens


def _load_learned_limits() -> dict:
    """Load auto-learned model token limits from server config."""
    try:
        from lib.config_dir import config_path
        cfg_path = config_path('server_config.json')
        if os.path.isfile(cfg_path):
            with open(cfg_path) as f:
                cfg = json.load(f)
            limits = cfg.get('model_limits', {})
            if limits:
                logger.info('[ModelInfo] Loaded %d auto-learned model limits: %s',
                            len(limits), ', '.join(f'{m}={v}' for m, v in limits.items()))
            return limits
    except Exception as e:
        logger.warning('[ModelInfo] Failed to load learned model limits: %s', e)
    return {}


# Initialize on module load
_LEARNED_MODEL_LIMITS = _load_learned_limits()


def _clamp_max_tokens(model: str, max_tokens: int) -> int:
    """Clamp max_tokens to the model-specific API limit.

    Checks both family-level limits (_MODEL_MAX_OUTPUT) and
    auto-learned per-model limits (_LEARNED_MODEL_LIMITS).
    Takes the minimum of all applicable limits.
    """
    limit = max_tokens
    # Check family-level limits
    for _name, (check_fn, family_limit) in _MODEL_MAX_OUTPUT.items():
        if check_fn(model):
            # family_limit can be an int or a callable(model) → int
            effective_limit = family_limit(model) if callable(family_limit) else family_limit
            limit = min(limit, effective_limit)
            break
    # Check auto-learned model-specific limits
    learned = _LEARNED_MODEL_LIMITS.get(model)
    if learned:
        limit = min(limit, learned)
    return limit


def _learn_model_limit(model: str, limit: int):
    """Auto-learn and persist a model's max_tokens limit.

    Updates the in-memory dict and writes to data/config/server_config.json
    so the limit survives server restarts.

    Args:
        model: Model identifier (e.g. 'gpt-4.1-mini').
        limit: Detected max_tokens upper bound.
    """
    with _limits_lock:
        old = _LEARNED_MODEL_LIMITS.get(model)
        if old == limit:
            return  # already known
        _LEARNED_MODEL_LIMITS[model] = limit
        logger.warning('[ModelInfo] ⚙️ Auto-learned max_tokens for model=%s: %d (was: %s). '
                       'Persisting to config.', model, limit, old or 'unknown')
        # Persist to server_config.json
        try:
            from lib.config_dir import config_path
            cfg_path = config_path('server_config.json')
            cfg = {}
            if os.path.isfile(cfg_path):
                with open(cfg_path) as f:
                    cfg = json.load(f)
            cfg.setdefault('model_limits', {})[model] = limit
            os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
            with open(cfg_path, 'w') as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
            logger.info('[ModelInfo] Persisted model limit to %s', cfg_path)
        except Exception as e:
            logger.error('[ModelInfo] Failed to persist model limit for %s: %s',
                         model, e, exc_info=True)
    # Audit trail
    try:
        from lib.log import audit_log
        audit_log('model_limit_learned', model=model, max_tokens=limit, previous=old)
    except Exception as _audit_err:
        logger.debug('[ModelInfo] audit_log for model_limit_learned failed: %s', _audit_err)


def _parse_token_limit_from_error(error_text: str, model: str):
    """Parse max_tokens upper bound from an API error message.

    Recognizes common error message formats from various LLM API providers:
      - "Range of max_tokens should be [1, 65536]"
      - "max_tokens must be at most 65536"
      - "max_tokens value must be between 1 and 65536"
      - "max_output_tokens must be at most 65536"

    Args:
        error_text: The raw error response text (may include JSON wrapping).
        model: Model identifier (for logging).

    Returns:
        Detected max_tokens limit (int), or None if not a token-limit error.
    """
    patterns = [
        # "[1, 65536]" or "[1,65536]" style ranges
        r'max_tokens.*?\[\s*\d+\s*,\s*(\d+)\s*\]',
        # "max_tokens must be at most/less than/no more than N"
        r'max_tokens.*?(?:at most|less than or equal to|no more than|cannot exceed|'
        r'must not exceed|up to|maximum of|maximum is)\s+(\d+)',
        # "max_tokens ... between 1 and N"
        r'max_tokens.*?between\s+\d+\s+and\s+(\d+)',
        # max_output_tokens variants
        r'max_output_tokens.*?\[\s*\d+\s*,\s*(\d+)\s*\]',
        r'max_output_tokens.*?(?:at most|less than or equal to|maximum)\s+(\d+)',
    ]
    for pat in patterns:
        m = re.search(pat, error_text, re.IGNORECASE)
        if m:
            detected = int(m.group(1))
            if 1 <= detected <= 1_000_000:  # sanity: must be a plausible token count
                logger.debug('[ModelInfo] Parsed max_tokens limit=%d from error for model=%s',
                            detected, model)
                return detected
    return None
