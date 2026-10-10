"""lib/image_gen/_gemini.py — FRIDAY Gemini async image API (submit + poll).

Extracted verbatim from the former flat ``lib/image_gen.py``:
  POST /v1/google/models/{model}:imageGenerate       → task id
  GET  /v1/google/models/{taskId}:imageGenerateQuery  → poll result
"""

import time

from lib.http_client import http_get, http_post
from lib.log import get_logger

from ._chat import _build_multiturn_contents
from ._errors import _IMAGE_GEN_BASE_DEFAULT, _HttpError, _RateLimitError, _download_image
from ._slots import _POLL_INTERVAL, _POLL_MAX_WAIT

logger = get_logger(__name__)


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
    resp = http_post(submit_url, headers=headers, json=body, timeout=timeout)
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
            poll_resp = http_get(poll_url, headers=poll_headers, timeout=30)
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
    result = {
        'ok': False,
        'error': error_msg,
        'text': text_content.strip(),
    }
    if block_reason:
        result['block_reason'] = block_reason
    return result
