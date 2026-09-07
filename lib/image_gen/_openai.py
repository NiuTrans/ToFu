"""lib/image_gen/_openai.py — FRIDAY OpenAI-native image generate + edit.

Extracted verbatim from the former flat ``lib/image_gen.py``:
  POST /v1/openai/native/images/generations  (generate)
  POST /v1/openai/native/images/edits         (edit — JSON raw-base64 body)
"""

import time

from lib.http_client import http_post
from lib.log import get_logger

from ._errors import _IMAGE_GEN_BASE_DEFAULT, _HttpError, _RateLimitError, _download_image
from ._slots import _OPENAI_SIZE_MAP

logger = get_logger(__name__)


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
    resp = http_post(url, headers=headers, json=body, timeout=timeout)
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


def _edit_openai(
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
    """Edit image(s) using the OpenAI images/edits API.

    The FRIDAY proxy's ``/v1/openai/native/images/edits`` does NOT accept the
    standard ``multipart/form-data`` upload (it returns HTTP 400 "Content type
    multipart/form-data not supported"). Instead it accepts a JSON body where
    ``image`` is **raw base64** (or an array of raw base64 strings for
    multi-image edits) — a ``data:`` URI prefix is rejected with
    "Illegal base64 character 3a". Verified empirically against
    your-llm-gateway.example.com 2026-06-10 with gpt-image-1 / gpt-image-1.5.

    Returns dict with 'ok', 'image_b64', 'mime_type', 'text', 'error'.
    Raises _RateLimitError on 429, _HttpError on other HTTP errors.
    """
    _base = friday_base or _IMAGE_GEN_BASE_DEFAULT
    url = f'{_base}/v1/openai/native/images/edits'
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {api_key}',
    }
    if extra_headers:
        headers.update(extra_headers)

    # Collect raw base64 strings (strip any data: prefix the caller passed).
    images_b64 = []
    for img in (source_images or []):
        b64 = (img.get('image_b64') or '').strip()
        if b64.startswith('data:'):
            # 'data:image/png;base64,XXXX' → 'XXXX'
            b64 = b64.split(',', 1)[-1]
        if b64:
            images_b64.append(b64)
    if not images_b64:
        return {'ok': False, 'error': 'No source image data for OpenAI edit'}

    size = _OPENAI_SIZE_MAP.get(aspect_ratio, '1024x1024')
    # Single image → bare string; multiple → array (gpt-image-1 multi-image edit).
    image_field = images_b64 if len(images_b64) > 1 else images_b64[0]

    body = {
        'model': model,
        'prompt': prompt,
        'image': image_field,
        'n': 1,
        'size': size,
        'quality': 'auto',
    }

    t0 = time.time()
    resp = http_post(url, headers=headers, json=body, timeout=timeout)
    elapsed = time.time() - t0

    if resp.status_code == 429:
        raise _RateLimitError(f'429 from OpenAI image edit API after {elapsed:.1f}s')

    if resp.status_code != 200:
        raise _HttpError(resp.status_code, resp.text[:300], elapsed)

    data = resp.json()
    items = data.get('data', [])
    if not items:
        return {'ok': False, 'error': 'Empty response from OpenAI image edit API'}

    item = items[0]
    image_b64 = item.get('b64_json', '')
    image_url = item.get('url', '')
    revised = item.get('revised_prompt', '') or ''
    mime_type = 'image/png'

    if image_url and not image_b64:
        image_b64, mime_type = _download_image(image_url)

    if image_b64:
        logger.info('[ImageGen] ✅ OpenAI edited image: model=%s %.1fs b64=%d chars n_src=%d',
                    model, elapsed, len(image_b64), len(images_b64))
        return {
            'ok': True,
            'image_b64': image_b64,
            'image_url': image_url,
            'mime_type': mime_type,
            'text': revised,
        }

    return {'ok': False, 'error': 'No image data in OpenAI edit response'}
