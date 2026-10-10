"""lib/image_gen/_chat.py — standard OpenAI-compatible chat-completions image
generation + the shared Gemini multi-turn contents builder.

Extracted verbatim from the former flat ``lib/image_gen.py``. Used for
OpenAI-compatible providers (yeysai.com, OpenRouter, …) that produce images via
``/v1/chat/completions`` with ``modalities: ["text","image"]``. The
``_build_multiturn_contents`` helper also feeds the Gemini generator, so it
lives here as the shared contents assembler.
"""

import time

from lib.http_client import http_post
from lib.log import get_logger

from ._errors import _IMAGE_GEN_BASE_DEFAULT, _HttpError, _RateLimitError

logger = get_logger(__name__)


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
    resp = http_post(url, headers=headers, json=body, timeout=timeout)
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
