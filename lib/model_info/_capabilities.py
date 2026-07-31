# HOT_PATH — functions in this module are called per-request.
"""lib/model_info/_capabilities.py — Per-model capability probes.

Reasoning-effort mapping (Gemini) plus the continue/resume replay-capability
gates (thinking-signature replay, thought-signature on tool calls,
reasoning-content replay, assistant-prefill tolerance) and vision support.

Depends on the family predicates in ._family (acyclic — _family imports
nothing from here).
"""

from lib.log import get_logger
from lib.model_info._family import (
    is_claude,
    is_deepseek,
    is_gemini,
    is_gpt_56,
)

logger = get_logger(__name__)


# Gemini 3.x reasoning-effort ladder.
#
# Gemini 3.x is a *reasoning* model family (minimal / low / medium / high
# thinking levels, default medium). On the OpenAI-compatible gateway the only
# knob that actually reaches Vertex's ``thinkingLevel`` is the OpenAI-style
# ``reasoning_effort`` string — verified empirically by the reasoning-token
# count in ``usage`` (minimal≈0 → high≈1000+ tokens). The legacy
# ``enable_thinking`` boolean and the nested ``thinking.thinking_level`` field
# are both silently ignored on this path.
#
# Tofu's depth ladder (off/low/medium/high/xhigh/max) collapses onto Gemini's
# four levels — xhigh/max have no Gemini equivalent and clamp to ``high``.
_GEMINI_EFFORT_MAP = {
    'off': 'minimal', 'minimal': 'minimal',
    'low': 'low',
    'medium': 'medium',
    'high': 'high', 'xhigh': 'high', 'max': 'high',
    # 'ultra' is a GPT-5.6-only tier — Gemini has no equivalent, clamp to high.
    'ultra': 'high',
}


def gemini_reasoning_effort(effort, thinking_enabled: bool = True) -> str:
    """Map a Tofu thinking-depth value to a Gemini 3.x ``reasoning_effort``.

    Args:
        effort: Tofu depth ladder value (off/low/medium/high/xhigh/max) or None.
        thinking_enabled: When False, force ``minimal`` regardless of effort
            (Gemini has no true "off" — minimal is the lowest level and yields
            ~0 reasoning tokens for simple queries).

    Returns:
        One of ``'minimal'`` / ``'low'`` / ``'medium'`` / ``'high'``.
    """
    if not thinking_enabled:
        return 'minimal'
    return _GEMINI_EFFORT_MAP.get((effort or 'medium').lower(), 'medium')


# OpenAI GPT-5 reasoning-effort ladder.
#
# The GPT-5 family (gpt-5, gpt-5.2, gpt-5.4, gpt-5.6, …) is controlled by the
# OpenAI-native ``reasoning_effort`` string: minimal / low / medium / high.
# GPT-5.6 (May 2026) introduced a fifth ``ultra`` tier above ``high`` for the
# hardest problems. Tofu's depth ladder (off/low/medium/high/xhigh/max/ultra)
# collapses onto these: ``xhigh`` and ``max`` are Claude-ladder rungs with no
# GPT equivalent (clamp to ``high``), and ``ultra`` clamps to ``high`` on any
# GPT-5 model older than 5.6.
_GPT_EFFORT_MAP = {
    'off': 'minimal', 'minimal': 'minimal',
    'low': 'low',
    'medium': 'medium',
    'high': 'high', 'xhigh': 'high', 'max': 'high',
    'ultra': 'ultra',
}


def gpt_reasoning_effort(effort, thinking_enabled: bool = True,
                         model: str = '') -> str:
    """Map a Tofu thinking-depth value to a GPT-5 ``reasoning_effort`` string.

    Args:
        effort: Tofu depth ladder value (off/low/medium/high/xhigh/max/ultra)
            or None.
        thinking_enabled: When False, force ``minimal`` (GPT-5's lowest tier).
        model: The concrete model id, used to gate the ``ultra`` tier — only
            GPT-5.6+ accepts it; older GPT-5.x downgrade ``ultra`` to ``high``.

    Returns:
        One of ``'minimal'`` / ``'low'`` / ``'medium'`` / ``'high'`` /
        ``'ultra'``.
    """
    if not thinking_enabled:
        return 'minimal'
    eff = _GPT_EFFORT_MAP.get((effort or 'medium').lower(), 'medium')
    if eff == 'ultra' and not is_gpt_56(model):
        logger.debug('[ModelInfo] reasoning_effort=ultra not supported on %s '
                     '— downgrading to high', model)
        eff = 'high'
    return eff


# Moonshot Kimi K3 reasoning-effort ladder.
#
# K3 always thinks (thinking cannot be disabled) and takes the TOP-LEVEL
# ``reasoning_effort`` string: low / high / max (default max) — per the
# official quickstart (platform.kimi.ai/docs/guide/kimi-k3-quickstart) and
# verified live against the sankuai gateway 2026-07-24. Sending any
# temperature other than 1.0 earns an HTTP 400
# (``invalid temperature: only 1 is allowed for this model``), so K3 bodies
# must omit temperature entirely.
#
# Tofu's depth ladder (off/low/medium/high/xhigh/max/ultra) collapses onto
# K3's three rungs, rounding UP so a depth never gets less reasoning than
# asked for. ``off`` maps to ``low`` — the closest legal rung, since K3 has
# no true off switch.
_KIMI_K3_EFFORT_MAP = {
    'off': 'low', 'minimal': 'low',
    'low': 'low',
    'medium': 'high',
    'high': 'high',
    'xhigh': 'max', 'max': 'max',
    # 'ultra' is a GPT-5.6-only tier — K3's top rung is max.
    'ultra': 'max',
}


def kimi_k3_reasoning_effort(effort, thinking_enabled: bool = True) -> str:
    """Map a Tofu thinking-depth value to a Kimi K3 ``reasoning_effort``.

    Args:
        effort: Tofu depth ladder value (off/low/medium/high/xhigh/max/ultra)
            or None.
        thinking_enabled: When False, force ``low`` — K3 cannot truly disable
            thinking, so ``low`` is the cheapest legal rung.

    Returns:
        One of ``'low'`` / ``'high'`` / ``'max'``.
    """
    if not thinking_enabled:
        return 'low'
    return _KIMI_K3_EFFORT_MAP.get((effort or 'medium').lower(), 'high')



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


def model_requires_reasoning_content_replay(model: str) -> bool:
    """True if this model's API REJECTS an assistant turn whose
    ``reasoning_content`` was emptied/stripped when thinking mode is on.

    DeepSeek V4 (pro/flash) in thinking mode returns HTTP 400
    (``The reasoning_content in the thinking mode must be passed back to
    the API.``) if a prior assistant message that carried a tool_call has
    its ``reasoning_content`` blanked.  Compaction's ``strip_thinking``
    step must therefore skip these models so the reasoning trace stays on
    the replayed turn.
    """
    return is_deepseek(model)


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
