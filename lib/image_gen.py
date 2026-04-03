"""lib/image_gen.py — Image generation via FRIDAY APIs.

Supports two families of image generation models:

 1. **Gemini** (async submit+poll):
    POST /v1/google/models/{model}:imageGenerate   → task ID
    GET  /v1/google/models/{taskId}:imageGenerateQuery → poll result
    Models: gemini-3.1-flash-image-preview, gemini-3-pro-image-preview,
            gemini-2.5-flash-image

 2. **OpenAI** (sync one-shot):
    POST /v1/openai/native/images/generations → b64_json / url
    Models: GPT-image-1.5

Dispatch picks from all available image_gen slots (currently 4 models × 2 keys = 8
slots) and cycles rapidly on 429 (rate-limit) since each slot has very low RPM (~10).
The dispatcher records 0.5s cooldown on 429'd slots, so subsequent picks naturally
steer to a different (key, model) pair.

Usage:
    from lib.image_gen import generate_image

    result = generate_image("A serene mountain landscape at sunset")
    if result['ok']:
        image_b64 = result['image_b64']
        mime_type = result['mime_type']
"""

import base64 as _b64
import os
import time
from urllib.parse import urlparse

import requests

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'generate_image',
]

# Proxy bypass via centralized lib/proxy — respects Settings UI config.
from lib.proxy import proxies_for as _proxies_for

# Image-gen API base — fallback only; prefer slot-derived base from dispatch.
# This is used when no provider-specific base_url is available from the slot.
_IMAGE_GEN_BASE_DEFAULT = os.environ.get('IMAGE_GEN_BASE_URL', '')

# ── Poll settings (Gemini async) ──
_POLL_INTERVAL = 3       # seconds between polls
_POLL_MAX_WAIT = 180     # max seconds to wait for result

# ── Models that use the OpenAI sync images/generations API ──
_OPENAI_IMAGE_MODELS = frozenset({
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

    Returns (api_key, model, slot) or (None, None, None)
    if no slot available.
    """
    try:
        from lib.llm_dispatch import get_dispatcher
        disp = get_dispatcher()
        # When user specifies a model, prefer it; otherwise let dispatch
        # pick the best slot score across all image_gen models.
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


# ══════════════════════════════════════════════════════════════
#  OpenAI sync API: POST /v1/openai/native/images/generations
# ══════════════════════════════════════════════════════════════

def _generate_openai(
    prompt: str,
    model: str,
    api_key: str,
    aspect_ratio: str,
    resolution: str,
    timeout: int,
    friday_base: str = '',
    extra_headers: dict | None = None,
) -> dict:
    """Generate an image using the OpenAI images/generations API (sync).

    Returns dict with 'ok', 'image_b64', 'mime_type', 'text', 'error'.
    Raises on HTTP errors (caller handles retry).
    """
    _base = friday_base or _IMAGE_GEN_BASE_DEFAULT
    url = f'{_base}/v1/openai/native/images/generations'
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {api_key}',
    }
    if extra_headers:
        headers.update(extra_headers)

    size = _OPENAI_SIZE_MAP.get(aspect_ratio, '1024x1024')

    body = {
        'model': model,
        'prompt': prompt,
        'n': 1,
        'size': size,
        'quality': 'auto',
    }

    t0 = time.time()
    resp = requests.post(url, headers=headers, json=body, proxies=_proxies_for(url), timeout=timeout)
    elapsed = time.time() - t0

    if resp.status_code == 429:
        raise _RateLimitError(f'429 from OpenAI image API after {elapsed:.1f}s')

    if resp.status_code != 200:
        raise _HttpError(resp.status_code, resp.text[:300], elapsed)

    data = resp.json()
    items = data.get('data', [])
    if not items:
        return {'ok': False, 'error': 'Empty response from OpenAI image API'}

    item = items[0]
    image_b64 = item.get('b64_json', '')
    image_url = item.get('url', '')
    revised = item.get('revised_prompt', '') or ''
    mime_type = 'image/png'

    # Download from URL if no inline base64
    if image_url and not image_b64:
        image_b64, mime_type = _download_image(image_url)

    if image_b64:
        logger.info('[ImageGen] ✅ OpenAI generated image: model=%s %.1fs b64=%d chars',
                     model, elapsed, len(image_b64))
        return {
            'ok': True,
            'image_b64': image_b64,
            'image_url': image_url,
            'mime_type': mime_type,
            'text': revised,
        }

    return {'ok': False, 'error': 'No image data in OpenAI response'}


# ══════════════════════════════════════════════════════════════
#  OpenAI Image Edit: POST /v1/openai/native/images/edits
# ══════════════════════════════════════════════════════════════

def _generate_openai_edit(
    prompt: str,
    model: str,
    api_key: str,
    aspect_ratio: str,
    resolution: str,
    timeout: int,
    source_images: list[dict],
    friday_base: str = '',
    extra_headers: dict | None = None,
) -> dict:
    """Edit an image using the OpenAI images/edits API.

    Sends the source image(s) and prompt via multipart form data to the
    ``/v1/openai/native/images/edits`` endpoint.

    Args:
        prompt: Edit instruction (e.g. "change background to blue").
        model: OpenAI model (gpt-image-1, gpt-image-1.5, etc.).
        api_key: API key.
        aspect_ratio: Aspect ratio hint.
        resolution: Resolution hint.
        timeout: HTTP timeout.
        source_images: List of ``{image_b64, mime_type}`` dicts.
        friday_base: FRIDAY API base URL.
        extra_headers: Extra HTTP headers.

    Returns:
        dict with 'ok', 'image_b64', 'mime_type', 'text', 'error'.
        Raises on HTTP errors (caller handles retry).
    """
    _base = friday_base or _IMAGE_GEN_BASE_DEFAULT
    url = f'{_base}/v1/openai/native/images/edits'
    headers = {
        'Authorization': f'Bearer {api_key}',
    }
    if extra_headers:
        headers.update(extra_headers)

    size = _OPENAI_SIZE_MAP.get(aspect_ratio, '1024x1024')

    # Build multipart form data
    # The OpenAI edits endpoint expects: image (file), prompt (text), model, size, n
    files = []
    ext_map = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/webp': '.webp'}

    for i, img in enumerate(source_images):
        b64 = img.get('image_b64', '')
        mime = img.get('mime_type', 'image/png')
        if not b64:
            continue
        ext = ext_map.get(mime, '.png')
        filename = f'source_{i}{ext}'
        raw_bytes = _b64.b64decode(b64)
        # OpenAI expects image[] for multiple images, or image for single
        if len(source_images) > 1:
            files.append(('image[]', (filename, raw_bytes, mime)))
        else:
            files.append(('image', (filename, raw_bytes, mime)))

    if not files:
        return {'ok': False, 'error': 'No valid source images for editing'}

    data = {
        'model': model,
        'prompt': prompt,
        'n': '1',
        'size': size,
        'quality': 'auto',
    }

    t0 = time.time()
    resp = requests.post(url, headers=headers, files=files, data=data,
                         proxies=_proxies_for(url), timeout=timeout)
    elapsed = time.time() - t0

    if resp.status_code == 429:
        raise _RateLimitError(f'429 from OpenAI image edit API after {elapsed:.1f}s')

    if resp.status_code != 200:
        raise _HttpError(resp.status_code, resp.text[:300], elapsed)

    result_data = resp.json()
    items = result_data.get('data', [])
    if not items:
        return {'ok': False, 'error': 'Empty response from OpenAI image edit API'}

    item = items[0]
    image_b64 = item.get('b64_json', '')
    image_url = item.get('url', '')
    revised = item.get('revised_prompt', '') or ''
    mime_type = 'image/png'

    # Download from URL if no inline base64
    if image_url and not image_b64:
        image_b64, mime_type = _download_image(image_url)

    if image_b64:
        logger.info('[ImageGen] ✅ OpenAI edited image: model=%s %.1fs b64=%d chars',
                     model, elapsed, len(image_b64))
        return {
            'ok': True,
            'image_b64': image_b64,
            'image_url': image_url,
            'mime_type': mime_type,
            'text': revised,
        }

    return {'ok': False, 'error': 'No image data in OpenAI edit response'}


# ══════════════════════════════════════════════════════════════
#  Multi-turn contents builder
# ══════════════════════════════════════════════════════════════

def _build_multiturn_contents(
    prompt: str,
    history: list[dict] | None = None,
    source_images: list[dict] | None = None,
) -> list:
    """Build the ``contents`` array for a Gemini multi-turn image request.

    Uses the Google-native **role-based** format (verified working with FRIDAY
    proxy 2026-03-30).  Each history entry is ``{prompt, image_b64, text}``
    representing one completed user→model round.

    Required format::

        contents: [
          {role: "user",  parts: [{text: "draw a cat"}]},
          {role: "model", parts: [{text: "Here is a cat."}, {inlineData: {mimeType, data}}]},
          {role: "user",  parts: [{text: "make it blue"}]},
        ]

    For image editing, ``source_images`` are included as ``inlineData`` parts
    in the current user turn alongside the text prompt::

        contents: [
          {parts: [
            {text: "change the background to a beach"},
            {inlineData: {mimeType: "image/png", data: "<base64>"}}
          ]}
        ]

    The model turn **must** include the image as base64 ``inlineData`` — URL
    references do NOT work with the FRIDAY proxy for multi-turn.

    Args:
        prompt: Current user prompt.
        history: Prior conversation turns (oldest first).
            Each entry: ``{prompt: str, image_b64: str, text: str, mime_type: str}``.
            ``image_b64`` is required; if only ``image_url`` is available the
            caller (route) must resolve it to base64 before passing here.
        source_images: Images to edit (for image editing mode).
            Each entry: ``{image_b64: str, mime_type: str}``.
            When provided, these are added as ``inlineData`` parts in the
            current user turn.

    Returns:
        List of content dicts for the ``contents`` field.
    """
    # ── Build current user turn parts ──
    current_parts = [{'text': prompt}]

    # Add source images as inlineData parts for image editing
    if source_images:
        for img in source_images:
            b64 = img.get('image_b64', '')
            mime = img.get('mime_type', 'image/png')
            if b64:
                current_parts.append({
                    'inlineData': {'mimeType': mime, 'data': b64},
                })
        logger.info('[ImageGen] Added %d source images to user turn for editing',
                    len(source_images))

    if not history:
        # Single-turn (generation or editing)
        return [{'parts': current_parts}]

    contents = []

    for turn in history:
        h_prompt = turn.get('prompt', '')
        h_image_b64 = turn.get('image_b64', '')
        h_text = turn.get('text', '') or ''
        h_mime = turn.get('mime_type', 'image/png')

        # ── User turn ──
        contents.append({'role': 'user', 'parts': [{'text': h_prompt}]})

        # ── Model turn ──
        model_parts = []
        if h_text:
            model_parts.append({'text': h_text})
        if h_image_b64:
            model_parts.append({
                'inlineData': {'mimeType': h_mime, 'data': h_image_b64},
            })
        if model_parts:
            contents.append({'role': 'model', 'parts': model_parts})
        else:
            logger.warning('[ImageGen] History turn missing image_b64, skipping model entry')

    # ── Current user turn (with optional source images) ──
    contents.append({'role': 'user', 'parts': current_parts})

    logger.info('[ImageGen] Built multi-turn contents: %d history turns + %d source images → %d content entries',
                len(history), len(source_images or []), len(contents))
    return contents



# ══════════════════════════════════════════════════════════════
#  Standard OpenAI-compatible chat completions image generation
# ══════════════════════════════════════════════════════════════

def _generate_chat_completions(
    prompt: str,
    model: str,
    api_key: str,
    aspect_ratio: str,
    resolution: str,
    timeout: int,
    history: list[dict] | None = None,
    source_images: list[dict] | None = None,
    api_base: str = '',
    extra_headers: dict | None = None,
) -> dict:
    """Generate an image via the standard OpenAI chat completions API.

    Used for OpenAI-compatible providers (yeysai.com, OpenRouter, etc.)
    that support image generation through ``/v1/chat/completions`` with
    ``modalities: ["text", "image"]``.

    The response image data is extracted from either:
    - ``message.content`` containing ``data:image/...;base64,...`` strings
    - ``message.images[]`` array (OpenRouter format)

    Returns dict with 'ok', 'image_b64', 'mime_type', 'text', 'error'.
    Raises _RateLimitError on 429, _HttpError on other HTTP errors.
    """
    import json as _json
    import re as _re

    _base = api_base or _IMAGE_GEN_BASE_DEFAULT
    url = f'{_base}/chat/completions'
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {api_key}',
    }
    if extra_headers:
        headers.update(extra_headers)

    # Build messages
    messages = []

    # Add history turns if present
    if history:
        for turn in history:
            h_prompt = turn.get('prompt', '')
            h_image_b64 = turn.get('image_b64', '')
            h_text = turn.get('text', '') or ''
            h_mime = turn.get('mime_type', 'image/png')

            # User turn
            messages.append({'role': 'user', 'content': h_prompt})

            # Assistant turn — include image as data URI if available
            assistant_parts = []
            if h_text:
                assistant_parts.append({'type': 'text', 'text': h_text})
            if h_image_b64:
                data_uri = f'data:{h_mime};base64,{h_image_b64}'
                assistant_parts.append({
                    'type': 'image_url',
                    'image_url': {'url': data_uri},
                })
            if assistant_parts:
                messages.append({'role': 'assistant', 'content': assistant_parts})

    # Current user turn
    user_content = []
    if source_images:
        # Image editing: include source images
        for img in source_images:
            b64 = img.get('image_b64', '')
            mime = img.get('mime_type', 'image/png')
            if b64:
                data_uri = f'data:{mime};base64,{b64}'
                user_content.append({
                    'type': 'image_url',
                    'image_url': {'url': data_uri},
                })
    user_content.append({'type': 'text', 'text': prompt})
    messages.append({'role': 'user', 'content': user_content})

    body = {
        'model': model,
        'messages': messages,
        'stream': False,
    }

    # Request image output modality — providers that support it will
    # return image data in the response; others will ignore it.
    body['modalities'] = ['text', 'image']

    t0 = time.time()
    resp = requests.post(url, headers=headers, json=body,
                         proxies=_proxies_for(url), timeout=timeout)
    elapsed = time.time() - t0

    if resp.status_code == 429:
        raise _RateLimitError(f'429 from chat completions after {elapsed:.1f}s')

    if resp.status_code != 200:
        raise _HttpError(resp.status_code, resp.text[:500], elapsed)

    data = resp.json()
    choices = data.get('choices', [])
    if not choices:
        return {'ok': False, 'error': 'Empty choices in chat completions response'}

    message = choices[0].get('message', {})
    content = message.get('content', '')
    images_field = message.get('images', [])  # OpenRouter format

    image_b64 = None
    mime_type = 'image/png'
    text_content = ''

    # ── Strategy 1: Extract from message.images[] (OpenRouter format) ──
    if images_field and isinstance(images_field, list):
        for img_entry in images_field:
            img_url_obj = img_entry.get('image_url', {})
            if isinstance(img_url_obj, dict):
                img_data_url = img_url_obj.get('url', '')
            else:
                img_data_url = str(img_url_obj)
            if img_data_url and img_data_url.startswith('data:image/'):
                # Parse data URI: data:image/png;base64,iVBOR...
                m = _re.match(r'data:(image/[^;]+);base64,(.*)', img_data_url, _re.S)
                if m:
                    mime_type = m.group(1)
                    image_b64 = m.group(2)
                    break

    # ── Strategy 2: Extract from content (may be string or list of parts) ──
    if not image_b64 and content:
        if isinstance(content, list):
            # Multi-part content: [{type: "text", text: ...}, {type: "image_url", ...}]
            for part in content:
                if isinstance(part, dict):
                    ptype = part.get('type', '')
                    if ptype == 'text':
                        text_content += part.get('text', '')
                    elif ptype == 'image_url':
                        img_url_data = part.get('image_url', {})
                        if isinstance(img_url_data, dict):
                            url_val = img_url_data.get('url', '')
                        else:
                            url_val = str(img_url_data)
                        if url_val.startswith('data:image/'):
                            m = _re.match(r'data:(image/[^;]+);base64,(.*)', url_val, _re.S)
                            if m:
                                mime_type = m.group(1)
                                image_b64 = m.group(2)
                    elif ptype == 'inline_data':
                        # Google native format via some proxies
                        inline = part.get('inline_data', {})
                        raw = inline.get('data', '')
                        if raw and not raw.startswith(('http://', 'https://')):
                            image_b64 = raw
                            mime_type = inline.get('mimeType', 'image/png')
        elif isinstance(content, str):
            # Content is a plain string — check for embedded data URI
            m = _re.search(r'data:(image/[^;]+);base64,([A-Za-z0-9+/=]+)', content)
            if m:
                mime_type = m.group(1)
                image_b64 = m.group(2)
                # Remove the data URI from text content
                text_content = content[:m.start()].strip()
            else:
                text_content = content

    if image_b64:
        logger.info('[ImageGen] ✅ Chat completions generated image: model=%s %.1fs b64=%d chars',
                     model, elapsed, len(image_b64))
        return {
            'ok': True,
            'image_b64': image_b64,
            'mime_type': mime_type,
            'text': text_content.strip(),
        }

    # No image found — return text content as error context
    if text_content.strip():
        error_msg = f'No image in response (text only): {text_content.strip()[:200]}'
    else:
        error_msg = 'No image data in chat completions response'
    logger.warning('[ImageGen] No image from chat completions model=%s: %s', model, error_msg)
    return {'ok': False, 'error': error_msg, 'text': text_content.strip()}


# ══════════════════════════════════════════════════════════════
#  Gemini async API: submit + poll (FRIDAY-specific)
# ══════════════════════════════════════════════════════════════

def _generate_gemini(
    prompt: str,
    model: str,
    api_key: str,
    aspect_ratio: str,
    resolution: str,
    timeout: int,
    history: list[dict] | None = None,
    source_images: list[dict] | None = None,
    friday_base: str = '',
    extra_headers: dict | None = None,
) -> dict:
    """Generate or edit an image using the FRIDAY Gemini async API.

    Supports multi-turn conversation via the ``history`` parameter and
    image editing via the ``source_images`` parameter.

    For image editing, source images are included as ``inlineData`` parts
    in the user turn alongside the text prompt, following the Gemini API
    multimodal input format.

    Returns dict with 'ok', 'image_b64', 'mime_type', 'text', 'error'.
    Raises _RateLimitError on 429, _HttpError on other HTTP errors.
    """
    _base = friday_base or _IMAGE_GEN_BASE_DEFAULT
    submit_url = f'{_base}/v1/google/models/{model}:imageGenerate'
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {api_key}',
    }
    if extra_headers:
        headers.update(extra_headers)

    # Build contents — single-turn, multi-turn, or editing
    contents = _build_multiturn_contents(prompt, history, source_images=source_images)

    body = {
        'contents': contents,
        'generationConfig': {'responseModalities': ['Text', 'Image']},
    }

    # imageSize and aspectRatio must be nested inside generationConfig.imageConfig
    # (NOT at the top level of generationConfig — that causes 400 Bad Request).
    # See: https://docs.cloud.google.com/vertex-ai/generative-ai/docs/reference/rest/v1beta1/GenerationConfig#ImageConfig
    image_config = {}
    if resolution and resolution.upper() in ('1K', '2K', '4K'):
        image_config['imageSize'] = resolution.upper()
    if aspect_ratio:
        image_config['aspectRatio'] = aspect_ratio
    if image_config:
        body['generationConfig']['imageConfig'] = image_config

    t0 = time.time()

    # ── Step 1: Submit ──
    resp = requests.post(submit_url, headers=headers, json=body, proxies=_proxies_for(submit_url), timeout=timeout)
    submit_elapsed = time.time() - t0

    if resp.status_code == 429:
        raise _RateLimitError(f'429 from Gemini submit after {submit_elapsed:.1f}s')
    if resp.status_code != 200:
        raise _HttpError(resp.status_code, resp.text[:300], submit_elapsed)

    task_id = resp.text.strip().strip('"')
    if not task_id:
        return {'ok': False, 'error': 'Empty task ID from Gemini submit'}

    logger.info('[ImageGen] Gemini task submitted: task_id=%s model=%s (%.1fs)',
                task_id, model, submit_elapsed)

    # ── Step 2: Poll ──
    poll_url = f'{_base}/v1/google/models/{task_id}:imageGenerateQuery'
    poll_headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
    }
    if extra_headers:
        poll_headers.update(extra_headers)

    poll_start = time.time()
    result_data = None
    fail_msg = None

    while time.time() - poll_start < _POLL_MAX_WAIT:
        time.sleep(_POLL_INTERVAL)
        try:
            poll_resp = requests.get(poll_url, headers=poll_headers, proxies=_proxies_for(poll_url), timeout=30)
            if poll_resp.status_code != 200:
                logger.warning('[ImageGen] Poll HTTP %d for task=%s', poll_resp.status_code, task_id)
                continue

            poll_data = poll_resp.json()
            status = poll_data.get('status', 0)

            if status == 1:
                result_data = poll_data.get('data', {})
                break
            elif status == -1:
                fail_data = poll_data.get('data', 'Unknown generation failure')
                fail_msg = str(fail_data)[:500] if fail_data else 'Unknown generation failure'
                logger.error('[ImageGen] Task %s failed (status=-1): %s — full_keys=%s',
                             task_id, fail_msg, list(poll_data.keys()) if isinstance(poll_data, dict) else '?')
                break
        except Exception as poll_e:
            logger.warning('[ImageGen] Poll error for task=%s: %s', task_id, poll_e)
            continue

    total_elapsed = time.time() - t0

    if result_data is None:
        error = fail_msg or f'Timed out after {_POLL_MAX_WAIT}s waiting for task {task_id}'
        return {'ok': False, 'error': error}

    # ── Extract image ──
    image_b64 = None
    image_url = None
    mime_type = 'image/png'
    text_content = ''
    block_reason = ''

    candidates = result_data.get('candidates', [])

    # Check for safety / content block at top level
    if result_data.get('promptFeedback', {}).get('blockReason'):
        block_reason = result_data['promptFeedback']['blockReason']
        safety_ratings = result_data['promptFeedback'].get('safetyRatings', [])
        logger.warning('[ImageGen] Prompt blocked: reason=%s ratings=%s task=%s',
                       block_reason, safety_ratings, task_id)

    if candidates:
        cand = candidates[0]
        # Check candidate-level finish/block reason
        finish_reason = cand.get('finishReason', '')
        cand_block = cand.get('blockReason', '')
        if finish_reason and finish_reason not in ('STOP', 'MAX_TOKENS'):
            if not block_reason:
                block_reason = finish_reason
            logger.warning('[ImageGen] Candidate finishReason=%s blockReason=%s task=%s',
                           finish_reason, cand_block, task_id)

        parts = cand.get('content', {}).get('parts', [])
        for part in parts:
            if 'text' in part:
                if part.get('thought'):
                    continue  # skip model thinking
                text_content += part['text']
            elif 'inlineData' in part:
                inline = part['inlineData']
                raw_data = inline.get('data', '')
                mime_type = inline.get('mimeType', 'image/png')
                if raw_data.startswith(('http://', 'https://')):
                    image_url = raw_data
                else:
                    image_b64 = raw_data
            elif 'image_url' in part:
                # FRIDAY may also return image_url.uri format
                uri = part['image_url'].get('uri', '') if isinstance(part['image_url'], dict) else str(part['image_url'])
                if uri:
                    image_url = uri

    # Download S3 URL if needed
    if image_url and not image_b64:
        image_b64, mime_type = _download_image(image_url, mime_type)

    if image_b64 or image_url:
        logger.info('[ImageGen] ✅ Gemini generated image: model=%s task=%s %.1fs b64=%d',
                     model, task_id, total_elapsed, len(image_b64 or ''))
        return {
            'ok': True,
            'image_b64': image_b64 or '',
            'image_url': image_url or '',
            'mime_type': mime_type,
            'text': text_content.strip(),
        }

    # Build informative error for no-image responses
    if block_reason:
        error_msg = f'Image generation blocked ({block_reason})'
        if text_content.strip():
            error_msg += f': {text_content.strip()[:200]}'
    elif text_content.strip():
        error_msg = f'No image in response (text only): {text_content.strip()[:200]}'
    elif not candidates:
        error_msg = 'No image in response (empty candidates — likely content policy block)'
    else:
        error_msg = 'No image in response (unknown reason)'

    logger.warning('[ImageGen] No image from task=%s model=%s: %s (raw_keys=%s)',
                   task_id, model, error_msg, list(result_data.keys())[:10])
    return {
        'ok': False,
        'error': error_msg,
        'text': text_content.strip(),
    }


# ══════════════════════════════════════════════════════════════
#  Helper errors & utilities
# ══════════════════════════════════════════════════════════════

class _RateLimitError(Exception):
    """429 rate limit — triggers retry without counting as hard error."""
    pass

class _HttpError(Exception):
    """Non-429 HTTP error."""
    def __init__(self, status_code, body, elapsed):
        self.status_code = status_code
        self.body = body
        self.elapsed = elapsed
        super().__init__(f'HTTP {status_code}: {body}')


def _download_image(url: str, default_mime: str = 'image/png') -> tuple:
    """Download an image URL and return (base64_str, mime_type)."""
    try:
        import base64 as _b64
        logger.info('[ImageGen] Downloading image from URL: %.120s', url)
        img_resp = requests.get(url, proxies=_proxies_for(url), timeout=30)
        img_resp.raise_for_status()
        image_b64 = _b64.b64encode(img_resp.content).decode('ascii')
        ct = img_resp.headers.get('Content-Type', '')
        if ct.startswith('image/'):
            mime = ct.split(';')[0].strip()
        elif url.endswith(('.jpg', '.jpeg')):
            mime = 'image/jpeg'
        elif url.endswith('.webp'):
            mime = 'image/webp'
        else:
            mime = default_mime
        logger.info('[ImageGen] Downloaded %d bytes → %d chars b64, mime=%s',
                    len(img_resp.content), len(image_b64), mime)
        return image_b64, mime
    except Exception as dl_e:
        logger.error('[ImageGen] Failed to download image from %s: %s', url, dl_e, exc_info=True)
        return None, default_mime


# ══════════════════════════════════════════════════════════════
#  Public API
# ══════════════════════════════════════════════════════════════

def generate_image(
    prompt: str,
    model: str = '',
    aspect_ratio: str = '1:1',
    resolution: str = '1K',
    history: list[dict] | None = None,
    source_images: list[dict] | None = None,
    timeout: int = 120,
    max_retries: int = 3,
) -> dict:
    """Generate or edit an image using the best available image_gen slot.

    Dispatch picks from all image_gen slots (Gemini + OpenAI models) and
    cycles rapidly on 429.  Each slot has ~10 RPM and the dispatcher
    applies a 0.5s cooldown on 429'd slots, so the next pick naturally
    lands on a different (key, model) pair.

    429 retries are aggressive (0.3s sleep) and unlimited (up to 120
    cycles safety cap).  Only non-429 errors count toward ``max_retries``.

    Args:
        prompt: Text description of the image to generate, or edit instruction.
        model: Force a specific model (empty = let dispatch pick best).
        aspect_ratio: Aspect ratio hint.
        resolution: Resolution hint — "1K" or "2K" (Gemini: imageConfig.imageSize,
            OpenAI: mapped via size param).
        history: Prior conversation turns for multi-turn image editing.
            Each entry: ``{prompt: str, image_url: str, text: str}``.
            Only used for Gemini models (OpenAI images API is single-turn).
        source_images: Images to edit (image editing mode).
            Each entry: ``{image_b64: str, mime_type: str}``.
            When provided, the prompt is treated as an edit instruction.
            For Gemini: images are sent as inlineData parts in the user turn.
            For OpenAI: uses the /v1/images/edits endpoint.
        timeout: HTTP request timeout in seconds.
        max_retries: Number of retry attempts on non-429 failures.

    Returns:
        dict with keys:
            ok: bool — whether generation succeeded
            image_b64: str — base64-encoded image data (if ok)
            mime_type: str — MIME type of the image (if ok)
            text: str — text response from the model (if any)
            error: str — error message (if not ok)
            model: str — model that was used
            aspect_ratio: str — aspect ratio used
    """
    last_error = 'No image_gen slot available'
    first_real_error = ''   # first non-429 error (the real cause)
    first_real_text = ''    # model text from the first real failure (e.g. safety refusal)
    hard_attempts = 0       # non-429 error count
    _429_count = 0          # 429 cycle count
    _429_max = 120          # safety cap

    while hard_attempts <= max_retries:
        api_key, slot_model, slot = _pick_image_slot(prefer_model=model)
        if not api_key:
            logger.warning('[ImageGen] No image_gen slot available, hard=%d/%d 429s=%d',
                           hard_attempts, max_retries, _429_count)
            hard_attempts += 1
            if hard_attempts <= max_retries:
                time.sleep(0.5)
                continue
            return {'ok': False, 'error': 'No image generation model available — check dispatch config'}

        use_model = model or slot_model
        is_friday = _is_friday_provider(slot)
        friday_base = _friday_base_from_slot(slot) if is_friday else ''
        api_base = _api_base_from_slot(slot) if not is_friday else ''

        try:
            is_edit = bool(source_images)
            _display_base = friday_base or api_base
            logger.info('[ImageGen] Attempt: hard=%d/%d 429s=%d model=%s ar=%s base=%s edit=%s friday=%s prompt="%.80s"',
                        hard_attempts, max_retries, _429_count, use_model, aspect_ratio,
                        _display_base, is_edit, is_friday, prompt[:80])
            t0 = time.time()

            _slot_hdrs = slot.extra_headers if slot else None
            if is_friday:
                # ── FRIDAY proprietary API paths ──
                if _is_openai_model(use_model):
                    if is_edit:
                        result = _generate_openai_edit(
                            prompt, use_model, api_key, aspect_ratio,
                            resolution, timeout, source_images=source_images,
                            friday_base=friday_base, extra_headers=_slot_hdrs)
                    else:
                        result = _generate_openai(prompt, use_model, api_key, aspect_ratio,
                                                   resolution, timeout, friday_base=friday_base,
                                                   extra_headers=_slot_hdrs)
                else:
                    result = _generate_gemini(prompt, use_model, api_key, aspect_ratio,
                                               resolution, timeout, history=history,
                                               source_images=source_images,
                                               friday_base=friday_base,
                                               extra_headers=_slot_hdrs)
            else:
                # ── Standard OpenAI-compatible chat completions API ──
                result = _generate_chat_completions(
                    prompt, use_model, api_key, aspect_ratio,
                    resolution, timeout, history=history,
                    source_images=source_images,
                    api_base=api_base, extra_headers=_slot_hdrs)

            elapsed = time.time() - t0

            if result.get('ok'):
                if slot:
                    slot.record_success(elapsed * 1000)
                result['model'] = use_model
                result['provider_id'] = slot.provider_id if slot else '?'
                result['aspect_ratio'] = aspect_ratio
                result['resolution'] = resolution
                if _429_count > 0:
                    logger.info('[ImageGen] Succeeded after %d 429-retries provider=%s',
                                _429_count, slot.provider_id if slot else '?')
                return result
            else:
                # Model returned but no image (e.g. text-only, safety block)
                if slot:
                    slot.record_error()
                last_error = result.get('error', 'Unknown error')
                if not first_real_error:
                    first_real_error = last_error
                    first_real_text = result.get('text', '')
                    logger.warning('[ImageGen] First real error: %s (text=%.200s)',
                                   first_real_error, first_real_text)
                hard_attempts += 1
                if hard_attempts <= max_retries:
                    time.sleep(0.5)
                    continue
                result['model'] = use_model
                return result

        except _RateLimitError:
            _429_count += 1
            if slot:
                slot.record_error(is_rate_limit=True)
            logger.info('[ImageGen] 429 rate-limited model=%s (429_count=%d), cycling slot',
                        use_model, _429_count)
            if _429_count >= _429_max:
                if first_real_error:
                    error_msg = '%s (then rate limited after %d retries)' % (first_real_error, _429_count)
                else:
                    error_msg = 'Rate limited (429) after %d retries — all slots RPM exhausted' % _429_count
                logger.warning('[ImageGen] Exhausted %d 429-retry cycles, giving up. first_real_error=%s',
                               _429_count, first_real_error or '(none)')
                return {'ok': False,
                        'error': error_msg,
                        'text': first_real_text,
                        'model': use_model, 'rate_limited': True}
            # Aggressive short sleep — dispatch cooldown (0.5s) already steers
            # to a different slot.  Keep trying fast.
            time.sleep(0.3)
            continue  # does NOT increment hard_attempts

        except _HttpError as he:
            if slot:
                # 401/403 = permanent auth failure — exclude this slot aggressively
                if he.status_code in (401, 403):
                    slot.record_error()
                    slot.record_error()  # double-penalize to push it far down
                    logger.warning('[ImageGen] Auth failure (HTTP %d) for model=%s provider=%s — slot penalized',
                                   he.status_code, use_model, slot.provider_id if slot else '?')
                else:
                    slot.record_error()
            logger.error('[ImageGen] HTTP %d model=%s (%.1fs): %s',
                         he.status_code, use_model, he.elapsed, he.body)
            last_error = f'HTTP {he.status_code}: {he.body}'
            if not first_real_error:
                first_real_error = last_error
            hard_attempts += 1
            if hard_attempts <= max_retries:
                time.sleep(1)
                continue
            return {'ok': False, 'error': last_error, 'model': use_model}

        except requests.exceptions.Timeout:
            if slot:
                slot.record_error()
            logger.warning('[ImageGen] Timeout model=%s hard=%d 429s=%d', use_model, hard_attempts, _429_count)
            last_error = f'Timeout after {timeout}s'
            if not first_real_error:
                first_real_error = last_error
            hard_attempts += 1
            if hard_attempts <= max_retries:
                continue
            return {'ok': False, 'error': last_error, 'model': use_model}

        except Exception as e:
            if slot:
                slot.record_error()
            logger.error('[ImageGen] Error hard=%d: %s', hard_attempts, e, exc_info=True)
            last_error = str(e)
            if not first_real_error:
                first_real_error = last_error
            hard_attempts += 1
            if hard_attempts <= max_retries:
                continue
            return {'ok': False, 'error': last_error, 'model': use_model}

    return {'ok': False, 'error': last_error}
