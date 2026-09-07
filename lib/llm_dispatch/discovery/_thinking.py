"""lib/llm_dispatch/discovery/_thinking.py — Thinking-format detection.

``_detect_thinking_format`` is imported directly by
``tests/test_backend_unit.py`` and is re-exported from the package facade.
"""

import re

from lib.log import get_logger

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════
#  Thinking Format Detection
# ══════════════════════════════════════════════════════

# Model name patterns → thinking format hint
_THINKING_FORMAT_HINTS = [
    # Doubao / Claude-style: thinking.type = "enabled"
    # ``fable`` = Anthropic Fable (Claude-family); a proxy/Bedrock-hosted
    # Fable whose brand isn't exactly 'claude' still needs the Claude shape.
    (re.compile(r'claude|anthropic|fable', re.I),  'thinking_type'),
    (re.compile(r'doubao|seed.*pro', re.I),       'thinking_type'),
    # Qwen / LongCat style: enable_thinking = true
    (re.compile(r'qwen|qwq', re.I),              'enable_thinking'),
    # Gemini 3.x: OpenAI-style reasoning_effort string (→ Vertex thinkingLevel)
    (re.compile(r'gemini', re.I),                 'reasoning_effort'),
    (re.compile(r'longcat', re.I),                'enable_thinking'),
    # GLM (Zhipu AI): thinking.type format
    (re.compile(r'glm', re.I),                    'thinking_type'),
    # DeepSeek V4 (Apr 2026) uses thinking.type = "enabled" (dual-mode API).
    (re.compile(r'deepseek-v4', re.I),            'thinking_type'),
    # NOTE: the V3-era 'deepseek-reasoner' → 'none' hint was removed
    # 2026-07-31 — the alias was RETIRED by DeepSeek on 2026-07-24 and had
    # already been re-pointed at v4-flash-thinking in Apr, so 'none' was
    # doubly wrong. Unknown deepseek names fall through to auto-detect.
]


# OpenAI-shim engines that expose dual-mode thinking through the
# Jinja chat template (``chat_template_kwargs.enable_thinking``) rather
# than a top-level ``enable_thinking`` body field. The engine
# self-identifies in ``/v1/models`` via the ``owned_by`` field — that's
# the durable signal we key on. Adding a future shim is a one-line
# entry here, no other change required.
_CHAT_TEMPLATE_KWARGS_ENGINES = frozenset({'sglang', 'vllm'})


def _is_chat_template_kwargs_engine(models: list[dict]) -> bool:
    """True iff /v1/models advertises a self-hosted shim that uses the
    Jinja ``chat_template_kwargs`` thinking gate (sglang, vLLM)."""
    for m in models:
        owner = (m.get('owned_by') or m.get('ownedBy') or '').strip().lower()
        if owner in _CHAT_TEMPLATE_KWARGS_ENGINES:
            return True
    return False


def _detect_thinking_format(models: list[dict], brand: str) -> str:
    """Suggest the thinking_format for a provider based on its models and brand.

    Resolution order (first match wins):

    1. ``owned_by`` field from /v1/models indicates a chat-template-
       kwargs engine (sglang / vLLM). This wins over brand because the
       same model family (e.g. Qwen3) needs a different body shape when
       served via sglang vs. Alibaba Bailian.
    2. Brand-level overrides (Claude, Doubao, GLM, Qwen, Gemini cloud).
    3. Per-model name pattern vote.

    Args:
        models: List of discovered model dicts.
        brand: Detected brand ID.

    Returns:
        Suggested thinking_format string, or '' for auto-detect.
    """
    # 1. Engine-level override (sglang / vLLM)
    if _is_chat_template_kwargs_engine(models):
        return 'chat_template_kwargs'

    # 2. Brand-level overrides
    brand_map = {
        'claude': 'thinking_type',
        'doubao': 'thinking_type',
        'glm': 'thinking_type',
        'qwen': 'enable_thinking',
        'gemini': 'reasoning_effort',
    }
    if brand in brand_map:
        return brand_map[brand]

    # 3. Check model names for hints
    format_votes = {}
    for m in models:
        mid = m.get('model_id', '')
        for pat, fmt in _THINKING_FORMAT_HINTS:
            if pat.search(mid):
                format_votes[fmt] = format_votes.get(fmt, 0) + 1
                break

    if format_votes:
        # Return the most common format
        winner = max(format_votes, key=format_votes.get)
        if winner != 'none':
            return winner

    return ''  # auto-detect
