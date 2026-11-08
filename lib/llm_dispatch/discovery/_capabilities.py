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
_RERANKING_PAT = re.compile(r'rerank', re.I)
_IMAGE_GEN_PAT = re.compile(
    r'(dall-?e|[-_]image|image[-_]|imagen|seedream|cogview|kolors'
    r'|flux[-_.]?\d|stable-diffusion|sdxl|ideogram|recraft'
    r'|(?:^|[^a-z0-9])t2i(?:$|[^a-z0-9])|text-to-image)',
    re.I,
)

# Task families recognized from an unmistakable model-id pattern. Each maps a
# name pattern to the ONE task capability (lib/model_info/capability_taxonomy
# MODEL_KINDS) it proves. Order is precedence: video/audio generation run
# before the broad image pattern because ids such as 'image-to-video' or
# 'wan2.2-i2v' contain image tokens yet produce video.
_TRANSCRIPTION_PAT = re.compile(
    r'(whisper|transcri(?:be|ption)|paraformer|sensevoice|speech-to-text'
    r'|(?:^|[^a-z0-9])asr(?:$|[^a-z]))',
    re.I,
)
_TTS_PAT = re.compile(
    r'(tts|text-to-speech|cosyvoice|fish-speech'
    r'|(?:^|/)speech-\d)',                      # MiniMax speech-02-hd / speech-2.5-*
    re.I,
)
_AUDIO_GEN_PAT = re.compile(
    r'(lyria|suno|musicgen|stable-audio|audioldm|riffusion|ace-step'
    r'|text-to-(?:music|sound)|(?:^|[^a-z0-9])music-\d)',
    re.I,
)
_VIDEO_GEN_PAT = re.compile(
    r'((?:^|[^a-z0-9])sora(?:$|[^a-z])|(?:^|[^a-z0-9])veo-\d|seedance'
    r'|kling(?!.*image)|hailuo|cogvideo|hunyuan-?video|ltx-?video|vidu'
    r'|pixverse|(?:^|[^a-z0-9])[ti]2v(?:$|[^a-z0-9])'
    r'|(?:text|image)-to-video|video-generat)',
    re.I,
)
_TASK_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ('embedding', _EMBEDDING_PAT),
    ('reranking', _RERANKING_PAT),
    ('transcription', _TRANSCRIPTION_PAT),
    ('tts', _TTS_PAT),
    ('audio_gen', _AUDIO_GEN_PAT),
    ('video_gen', _VIDEO_GEN_PAT),
    ('image_gen', _IMAGE_GEN_PAT),
)


def _task_capability(model_id: str) -> str | None:
    """The non-chat task capability a model id unmistakably names, else None."""
    for capability, pattern in _TASK_PATTERNS:
        if pattern.search(model_id):
            return capability
    return None

# Thinking / reasoning models. Most cloud families embed "think" or
# "reason" in the model ID, but a few model families ship as dual-mode
# (thinking-by-default) without that hint:
#   • GLM 4.5+ / 5.x   (Zhipu AI; "glm-4.5", "glm-5", "glm5.1")
#   • Kimi K2-thinking variants
#   • Qwen3-* (dual-mode by default — name doesn't reveal it)
#   • DeepSeek V4 / V4.1 'deepseek-flash' (dual-mode)
#   • Anthropic Fable ("fable-5" — Claude-family reasoning model, no hint)
# Self-hosted vLLM/SGLang deployments expose those models with their
# raw IDs (e.g. "glm5.1-fp8") and won't pick up thinking auto-tagging
# unless the regex covers them explicitly.
_THINKING_PAT = re.compile(
    r'(think|reason|\bo[1234]-|\bo[1234]\b|ernie-x'
    r'|glm[-_]?(?:4\.[5-9]|[5-9])'
    r'|qwen-?3'
    r'|deepseek-?(?:v[4-9]|flash)'            # DeepSeek V4+ / V4.1 'deepseek-flash' (dual-mode)
    r'|kimi-k3'                                # Kimi K3 always runs in thinking mode
    r'|fable)',
    re.I,
)

# Vision-capable families (permissive — most modern models support it)
_VISION_PAT = re.compile(
    r'(vision|vl\b|vlm'
    r'|gpt-4[.o]|gpt-5'                     # GPT-4o+, GPT-5+
    r'|claude.*(opus|sonnet|haiku)'          # All Claude 3+ have vision
    r'|fable'                                # Anthropic Fable (Claude-family, multimodal)
    r'|gemini(?!.*lite)'                     # Gemini (except flash-lite)
    r'|qwen.*(vl|max|plus)'                 # Qwen VL/Max/Plus
    r'|ernie-5\.0'                           # ERNIE 5.0 is natively multimodal
    r'|kimi-(?:k2\.[56]|k3)'                 # Kimi K2.5/K2.6/K3 are natively multimodal (vision)
    r'|deepseek-flash'                        # V4.1 Flash base model is natively multimodal (2026-09-10)
    r'|glm-5v'                               # GLM-5V (vision variant)
    r')',
    re.I,
)

# Video-capable families. Distinct from vision: 'video' means the model
# accepts a full video stream as input (not just still images). Kimi K3
# is the currently-shipped example (per platform.kimi.com/docs/guide/
# use-kimi-vision-model). K2.6 / K2.7-code also support video but their
# vision baseline isn't uniformly landed here yet — expand as those slots
# gain the 'vision' seed.
_VIDEO_PAT = re.compile(
    r'(kimi-k3)',
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

# Generation slots share the image-generation request-rate and cost defaults.
_MEDIA_GENERATION_CAPS = frozenset({'image_gen', 'video_gen', 'audio_gen'})

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

    # An explicit list is complete evidence, not a seed for name heuristics.
    if model_meta is not None:
        declared = model_meta.get('capabilities')
        if isinstance(declared, list):
            return {c for c in declared if isinstance(c, str)}
        if isinstance(declared, dict):
            return {name for name, fact in declared.items()
                    if fact is True or fact == 'supported'
                    or isinstance(fact, dict) and fact.get('supported') is True}
        # Catalog IDs alone prove no optional inference feature. A task
        # family is not an optional feature: the 'text' placeholder would
        # itself claim a chat surface, so a whisper/tts/image/video id keeps
        # its non-chat kind instead of leaking into chat pickers.
        task = _task_capability(model_id.lower())
        return {task} if task else {'text'}

    mid_lower = model_id.lower()

    # ── Non-chat task families (embedding, rerank, speech, media generation) ──
    task = _task_capability(mid_lower)
    if task:
        caps.add(task)
        return caps

    # ── Chat models ──
    caps.add('text')

    if _VISION_PAT.search(mid_lower):
        caps.add('vision')

    if _VIDEO_PAT.search(mid_lower):
        caps.add('video')

    if _THINKING_PAT.search(mid_lower):
        caps.add('thinking')

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
    if _MEDIA_GENERATION_CAPS.intersection(capabilities):
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
    from lib.pricing import pricing_for_model
    pricing = pricing_for_model(model_id)
    if pricing:
        return round((pricing['input'] + pricing['output']) / 2.0 / 1000.0, 4)

    mid = model_id.lower()
    if 'embedding' in capabilities:
        return 0.001
    if _MEDIA_GENERATION_CAPS.intersection(capabilities):
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
