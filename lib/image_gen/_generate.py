"""lib/image_gen/_generate.py — the public ``generate_image`` orchestrator.

Extracted verbatim from the former flat ``lib/image_gen.py``. Owns the
slot-picking retry loop: aggressive 429 cycling across slots, non-429 hard-error
budget, deterministic-4xx fail-fast, and per-provider dispatch to the OpenAI /
chat-completions / Gemini generators.
"""

import time

import requests

from lib.log import get_logger

from ._chat import _generate_chat_completions
from ._errors import _HttpError, _RateLimitError
from ._gemini import _generate_gemini
from ._openai import _edit_openai, _generate_openai
from ._slots import (
    _api_base_from_slot,
    _friday_base_from_slot,
    _is_friday_provider,
    _is_openai_model,
    _pick_image_slot,
)

logger = get_logger(__name__)


def generate_image(
    prompt: str,
    model: str = '',
    aspect_ratio: str = '1:1',
    resolution: str = '1K',
    history: list[dict] | None = None,
    source_images: list[dict] | None = None,
    timeout: int = 120,
    max_retries: int = 3,
    on_429: 'callable | None' = None,
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
        on_429: Optional callback ``fn(retry_count)`` called on each 429
            rate-limit retry.  Use this to push live progress to the UI
            so the user knows the request is rate-limited, not stuck.

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
                        # FRIDAY's /images/edits rejects multipart but accepts
                        # a JSON body with raw-base64 `image` (verified 2026-06-10).
                        result = _edit_openai(
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
                    result['_429_count'] = _429_count
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
            if on_429:
                try:
                    on_429(_429_count)
                except Exception as cb_e:
                    logger.debug('[ImageGen] on_429 callback error: %s', cb_e)
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
            # ── Deterministic client errors (400 safety violation / invalid
            #    prompt / bad param) are NOT worth retrying — the prompt is
            #    the problem, not the slot.  Retrying just burns latency and
            #    penalizes a healthy slot, causing follow-up requests to
            #    hit exponential cooldowns and eventually time out.
            _body_lc = (he.body or '').lower()
            # 4xx (except 429 which is handled as _RateLimitError) is a
            # PERMANENT client-side error — bad prompt, bad params, auth
            # failure, quota, etc.  Retrying burns 5-8 min of latency and
            # still ends in HTTP 500 for the user.  Fail fast for ANY 4xx;
            # surface the provider body so the route handler can return 400.
            is_client_4xx = 400 <= he.status_code < 500 and he.status_code != 429
            is_deterministic_400 = (
                he.status_code == 400 and (
                    'safety' in _body_lc
                    or 'safety_violations' in _body_lc
                    or 'content policy' in _body_lc
                    or 'invalid_request' in _body_lc
                    or 'image_generation_user_error' in _body_lc
                )
            )
            if slot:
                # 401/403 = permanent auth failure — exclude this slot aggressively
                if he.status_code in (401, 403):
                    slot.record_error()
                    slot.record_error()  # double-penalize to push it far down
                    logger.warning('[ImageGen] Auth failure (HTTP %d) for model=%s provider=%s — slot penalized',
                                   he.status_code, use_model, slot.provider_id if slot else '?')
                elif is_deterministic_400:
                    # Do NOT record_error — the slot is healthy; this is a user-prompt issue.
                    logger.info('[ImageGen] Skipping slot penalty for deterministic HTTP 400 (model=%s)',
                                use_model)
                elif is_client_4xx:
                    # Other 4xx — don't penalize slot for a client-side bug either.
                    logger.info('[ImageGen] Skipping slot penalty for client HTTP %d (model=%s)',
                                he.status_code, use_model)
                else:
                    slot.record_error()
            logger.error('[ImageGen] HTTP %d model=%s (%.1fs): %s',
                         he.status_code, use_model, he.elapsed, he.body)
            last_error = f'HTTP {he.status_code}: {he.body}'
            if not first_real_error:
                first_real_error = last_error
            if is_client_4xx:
                logger.warning('[ImageGen] Fail-fast on client HTTP %d — no retry. model=%s body=%.200s',
                               he.status_code, use_model, he.body)
                return {
                    'ok': False,
                    'error': last_error,
                    'model': use_model,
                    'status_code': he.status_code,
                    'client_error': True,
                    'safety_blocked': is_deterministic_400 and (
                        'safety' in _body_lc or 'image_generation_user_error' in _body_lc
                    ),
                }
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
