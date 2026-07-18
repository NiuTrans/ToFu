"""lib/llm_dispatch/discovery/_capabilities.py — Model capability inference.

Compiled name-pattern regexes + the capability / RPM / cost inference
helpers. ``_VISION_PAT`` is imported directly by
``lib/model_info/_capabilities.py`` and is re-exported from the package
facade.
"""

import re

from lib.log import get_logger

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════
#  Capability Inference Patterns
# ══════════════════════════════════════════════════════

_EMBEDDING_PAT = re.compile(r'embed', re.I)
_IMAGE_GEN_PAT = re.compile(r'(dall-?e|[-_]image|image[-_])', re.I)

# Thinking / reasoning models. Most cloud families embed "think" or
# "reason" in the model ID, but a few model families ship as dual-mode
# (thinking-by-default) without that hint:
#   • GLM 4.5+ / 5.x   (Zhipu AI; "glm-4.5", "glm-5", "glm5.1")
#   • Kimi K2-thinking variants
#   • Qwen3-* (dual-mode by default — name doesn't reveal it)
#   • DeepSeek V4 (dual-mode)
# Self-hosted vLLM/SGLang deployments expose those models with their
# raw IDs (e.g. "glm5.1-fp8") and won't pick up thinking auto-tagging
# unless the regex covers them explicitly.
_THINKING_PAT = re.compile(
    r'(think|reason|\bo[1234]-|\bo[1234]\b|ernie-x'
    r'|glm[-_]?(?:4\.[5-9]|[5-9])'
    r'|qwen-?3'
    r'|deepseek-?v[4-9])',
    re.I,
)

# Vision-capable families (permissive — most modern models support it)
_VISION_PAT = re.compile(
    r'(vision|vl\b|vlm'
    r'|gpt-4[.o]|gpt-5'                     # GPT-4o+, GPT-5+
    r'|claude.*(opus|sonnet|haiku)'          # All Claude 3+ have vision
    r'|gemini(?!.*lite)'                     # Gemini (except flash-lite)
    r'|qwen.*(vl|max|plus)'                 # Qwen VL/Max/Plus
    r'|ernie-5\.0'                           # ERNIE 5.0 is natively multimodal
    r'|kimi-k2\.[56]'                        # Kimi K2.5/K2.6 are natively multimodal
    r'|glm-5v'                               # GLM-5V (vision variant)
    r')',
    re.I,
)

# Cheap model name hints (fallback when no pricing data exists)
_CHEAP_HINT_PAT = re.compile(
    r'(mini|nano|lite|turbo|small|haiku|free)',
    re.I,
)


# ══════════════════════════════════════════════════════
#  Capability / RPM / Cost Inference
# ══════════════════════════════════════════════════════

def _infer_capabilities(model_id: str, model_meta: dict = None) -> set:
    """Infer model capabilities from its name and optional API metadata.

    Auto-tags 'cheap' if the model's input price < Sonnet input ($3/1M) AND
    output price < Sonnet output ($15/1M), using MODEL_PRICING.

    Args:
        model_id: The model identifier (e.g. 'gpt-5.4-mini').
        model_meta: Optional metadata dict from the /v1/models response.

    Returns:
        Set of capabilities like {'text', 'vision', 'thinking', 'cheap'}.
    """
    caps = set()

    # Some providers include capability info in model metadata
    if model_meta and isinstance(model_meta.get('capabilities'), list):
        for c in model_meta['capabilities']:
            if isinstance(c, str):
                caps.add(c.lower())

    mid_lower = model_id.lower()

    # ── Embedding models (not chat models) ──
    if _EMBEDDING_PAT.search(mid_lower):
        caps.add('embedding')
        return caps

    # ── Image generation models (not chat models) ──
    if _IMAGE_GEN_PAT.search(mid_lower):
        caps.add('image_gen')
        return caps

    # ── Chat models ──
    caps.add('text')

    if _THINKING_PAT.search(mid_lower):
        caps.add('thinking')

    if _VISION_PAT.search(mid_lower):
        caps.add('vision')

    # ── Pricing-based tier tags (cheap, plus any future PRICING_TIERS rows) ──
    # Driven by a single table in lib/llm_dispatch/config.py so the same
    # rule applies everywhere (discovery, Settings UI load, provider
    # templates, static rewriter).
    from lib.llm_dispatch.config import get_pricing_tiers
    caps |= get_pricing_tiers(model_id)
    # Note: name-heuristic fallback (_CHEAP_HINT_PAT) is intentionally removed.
    # Tier tags come only from real pricing data to avoid false positives.

    return caps


def _infer_rpm(model_id: str, capabilities: set) -> int:
    """Guess a reasonable RPM limit from model type."""
    mid = model_id.lower()
    if 'embedding' in capabilities:
        return 60
    if 'image_gen' in capabilities:
        return 10
    if any(x in mid for x in ('nano', 'turbo', 'free')):
        return 200
    if any(x in mid for x in ('mini', 'small', 'haiku', 'lite')):
        return 120
    if any(x in mid for x in ('flash',)):
        return 100
    if any(x in mid for x in ('opus', 'large', 'max', '-pro')):
        return 30
    return 60


def _infer_cost(model_id: str, capabilities: set) -> float:
    """Get blended cost per 1K tokens for dispatch priority.

    Checks MODEL_PRICING first, falls back to name-based estimate.
    """
    from lib import MODEL_PRICING
    pricing = MODEL_PRICING.get(model_id)
    if pricing:
        return round((pricing['input'] + pricing['output']) / 2.0 / 1000.0, 4)

    mid = model_id.lower()
    if 'embedding' in capabilities:
        return 0.001
    if 'image_gen' in capabilities:
        return 0.02
    if any(x in mid for x in ('nano', 'free')):
        return 0.001
    if any(x in mid for x in ('mini', 'small', 'lite', 'haiku', 'turbo')):
        return 0.002
    if any(x in mid for x in ('flash',)):
        return 0.003
    if any(x in mid for x in ('opus', 'large', 'max')):
        return 0.02
    return 0.005
