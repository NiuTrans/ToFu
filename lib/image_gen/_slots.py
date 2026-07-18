"""lib/image_gen/_slots.py — dispatch-slot selection + provider routing.

Extracted verbatim from the former flat ``lib/image_gen.py``. Holds the
model-family constants, the FRIDAY-vs-OpenAI-compatible provider predicates,
the base-URL derivations that keep one provider's key off another's endpoint,
and the dispatch slot picker.
"""

from urllib.parse import urlparse

from lib.log import get_logger

from ._errors import _IMAGE_GEN_BASE_DEFAULT

logger = get_logger(__name__)

# ── Poll settings (Gemini async) ──
_POLL_INTERVAL = 3       # seconds between polls
_POLL_MAX_WAIT = 180     # max seconds to wait for result

# ── Models that use the OpenAI sync images/generations API ──
_OPENAI_IMAGE_MODELS = frozenset({
    'gpt-image-2',
    'gpt-image-1.5',
    'gpt-image-1',
    'gpt-image-1-mini',
    'dall-e-3',
})

# ── Size mapping for OpenAI models ──
_OPENAI_SIZE_MAP = {
    '1:1': '1024x1024',
    '16:9': '1536x1024',
    '9:16': '1024x1536',
    '4:3': '1536x1024',
    '3:4': '1024x1536',
}


# ── Domains that use the proprietary FRIDAY async image API ──
# All other providers use the standard OpenAI-compatible chat completions API.
_FRIDAY_DOMAINS = frozenset({
    'your-llm-gateway.example.com',
})


def _is_friday_provider(slot) -> bool:
    """Check if a slot's provider uses the proprietary FRIDAY image API.

    FRIDAY providers have custom async endpoints:
      - Gemini: {base}/v1/google/models/{model}:imageGenerate  (submit+poll)
      - OpenAI: {base}/v1/openai/native/images/generations

    All other providers (yeysai.com, OpenRouter, etc.) use the standard
    OpenAI-compatible ``/v1/chat/completions`` endpoint.
    """
    if slot and slot.base_url:
        p = urlparse(slot.base_url)
        return p.netloc in _FRIDAY_DOMAINS
    return False


def _friday_base_from_slot(slot) -> str:
    """Derive the FRIDAY API base URL from a dispatch slot's base_url.

    FRIDAY image API paths always start at the root:
      - Gemini: {base}/v1/google/models/{model}:imageGenerate
      - OpenAI: {base}/v1/openai/native/images/generations

    So the FRIDAY base is just ``scheme://host`` with no path component.
    This prevents cross-provider key contamination (e.g. sending a key
    from provider A to provider B's endpoint).
    """
    if slot and slot.base_url:
        p = urlparse(slot.base_url)
        return f'{p.scheme}://{p.netloc}'
    return _IMAGE_GEN_BASE_DEFAULT


def _api_base_from_slot(slot) -> str:
    """Derive the standard OpenAI-compatible API base URL from a slot.

    Returns the slot's base_url directly (e.g. 'https://yeysai.com/v1'),
    or falls back to the default.
    """
    if slot and slot.base_url:
        return slot.base_url.rstrip('/')
    return _IMAGE_GEN_BASE_DEFAULT.rstrip('/')


def _pick_image_slot(prefer_model: str = ''):
    """Pick a dispatch slot with 'image_gen' capability.

    Args:
        prefer_model: When set, prefer this model; otherwise let dispatch
            pick the best slot score across all image_gen models. Both the
            Gemini and OpenAI image families support editing, so no model
            steering is needed for edits.

    Returns (api_key, model, slot) or (None, None, None)
    if no slot available.
    """
    try:
        from lib.llm_dispatch import get_dispatcher
        disp = get_dispatcher()
        slot = disp.pick_and_reserve(
            capability='image_gen',
            prefer_model=prefer_model or None,
        )
        if slot:
            return slot.api_key, slot.model, slot
    except Exception as e:
        logger.warning('[ImageGen] Dispatch pick failed: %s', e)
    return None, None, None


def _is_openai_model(model: str) -> bool:
    """Check if the model uses the OpenAI sync images/generations API."""
    return model in _OPENAI_IMAGE_MODELS or model.lower() in _OPENAI_IMAGE_MODELS
