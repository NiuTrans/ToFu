"""routes/upload.py — Image upload/serve, image generation, PDF parsing endpoints."""

import base64
import io
import os
import time
import tempfile
from typing import BinaryIO

from quart import Blueprint, request

from lib.api_v4 import raw_success_response
from lib.openapi import media_responses, api_meta
from lib.quart_sync import request_files, request_form, send_file

# ── Magic-bytes image-type detector (imghdr replacement, Py 3.13+ safe) ──
# Returns one of {'png','jpeg','gif','webp','bmp'} or None.
def _detect_image_format(head: bytes) -> str | None:
    if not head or len(head) < 4:
        return None
    if head.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png'
    if head[:3] == b'\xff\xd8\xff':
        return 'jpeg'
    if head[:6] in (b'GIF87a', b'GIF89a'):
        return 'gif'
    if len(head) >= 12 and head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'webp'
    if head[:2] == b'BM':
        return 'bmp'
    return None

from lib.log import get_logger
from lib.api_response import (
    api_bad_request as api_bad_request, api_error as api_error, api_internal_error as api_internal_error, api_not_found as api_not_found, api_ok as api_ok,
    api_payload as api_payload, api_service_unavailable as api_service_unavailable, api_typed_error as api_typed_error,
)
from lib.request_parser import parse_body

logger = get_logger(__name__)

upload_bp = Blueprint('upload', __name__)
# v1 blueprint for the JSON routes (the 5 carve-outs above stay on upload_bp).
from routes.api_v1.uploads import api_v1_uploads_bp  # noqa: E402
from routes.api_v1.auth import current_auth, request_user_id  # noqa: E402

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# User-uploaded images are USER STATE and must live under the resolved
# runtime base (co-located with the DB that references them by /api/images/
# URL), NOT the code tree — see lib/runtime_paths.uploads_root(). In the
# default in-tree layout this is byte-identical to <repo>/uploads/images.
from lib.runtime.runtime_paths import uploads_root  # noqa: E402
# TODO(enterprise, I6): flat single-tenant directory — serve_image performs
# no ownership check; namespace keys per principal and authorize reads.
# docs/ENTERPRISE_EVOLUTION.md
UPLOAD_DIR = os.path.join(uploads_root(), 'images')
os.makedirs(UPLOAD_DIR, exist_ok=True)


# ══════════════════════════════════════════════════════
#  SSRF / transport guard for user-supplied image URLs (B10)
# ══════════════════════════════════════════════════════
# /api/images/generate accepts arbitrary `image_url` strings in its
# `history` and `source_images` payloads. Without guards a tunnel-
# authenticated client can probe internal services or cloud-metadata
# endpoints. File-backed intake avoids accumulating download chunks in RAM.
# ``_safe_image_fetch``
# is the single chokepoint: every place that resolves a remote
# user-supplied image URL goes through it.
#
# Default deny: loopback, link-local, RFC1918, multicast, reserved.
# Override with ``TOFU_IMAGE_FETCH_ALLOW_HOSTS`` (comma-separated
# exact-match hostnames) when self-hosting requires reaching a
# private endpoint.

_DEFAULT_IMAGE_UPLOAD_MAX_PIXELS = 40_000_000       # ~160 MiB RGBA decode


class _ImageFetchError(Exception):
    """Raised when a user-supplied image URL fails the SSRF / transport guard."""


def _image_upload_max_pixels() -> int:
    raw = (os.environ.get('TOFU_IMAGE_UPLOAD_MAX_PIXELS') or '').strip()
    try:
        value = int(raw) if raw else _DEFAULT_IMAGE_UPLOAD_MAX_PIXELS
    except ValueError as exc:
        logger.debug('[ImageUpload] Invalid TOFU_IMAGE_UPLOAD_MAX_PIXELS=%r: %s',
                     raw, exc)
        value = _DEFAULT_IMAGE_UPLOAD_MAX_PIXELS
    return max(1_000_000, min(value, _DEFAULT_IMAGE_UPLOAD_MAX_PIXELS))


def _image_source_length(source):
    if isinstance(source, bytes):
        return len(source)
    source.seek(0, 2)
    length = source.tell()
    source.seek(0)
    return length


def _image_decode_source(source):
    if isinstance(source, bytes):
        return io.BytesIO(source)
    source.seek(0)
    return source


def _write_image_source(source, output):
    if isinstance(source, bytes):
        output.write(source)
        return
    source.seek(0)
    while True:
        chunk = source.read(64 * 1024)
        if not chunk:
            return
        output.write(chunk)


def _upload_image_dimension_error(image_bytes: bytes | BinaryIO) -> tuple[str, bool] | None:
    """Return ``(message, oversized)`` before any full Pillow decode."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(_image_decode_source(image_bytes)) as image:
            width, height = image.size
    except Exception as exc:
        logger.debug('[ImageUpload] image header validation failed: %s', exc)
        return (f'Invalid or corrupt image: {exc}', False)
    pixels = int(width) * int(height)
    limit = _image_upload_max_pixels()
    if width <= 0 or height <= 0:
        return ('Invalid image dimensions', False)
    if pixels > limit:
        return (
            f'Image dimensions {width}x{height} exceed the {limit:,}-pixel limit',
            True,
        )
    return None


def _is_blocked_ip(ip_str: str) -> bool:
    """Return True if ``ip_str`` is loopback / RFC1918 / link-local / multicast /
    reserved / unspecified. These addresses must not be reachable through a
    user-supplied URL."""
    from lib.safe_fetch import ip_is_public
    return not ip_is_public(ip_str)


def _safe_image_fetch(url: str, *, timeout: int = 30) -> tuple[BinaryIO, str]:
    """Return a caller-owned complete image file through public egress checks.

    Validate every redirect hop; keep per-read network timeouts. Source bytes
    and ongoing download duration have no private admission ceiling.
    """
    from lib.safe_fetch import SafeFetchError, fetch_public_file
    try:
        source, content_type, _final_url = fetch_public_file(
            url, timeout=timeout, max_redirects=3,
            allow_hosts_env='TOFU_IMAGE_FETCH_ALLOW_HOSTS',
            enforce_total_timeout=False)
        return source, content_type
    except SafeFetchError as exc:
        raise _ImageFetchError(str(exc)) from exc


def _encode_image_source(source: BinaryIO) -> tuple[str, int]:
    """Encode complete file bytes without retaining a decoded source copy."""
    source.seek(0)
    encoded = io.StringIO()
    total = 0
    # Every non-final block is divisible by three; padding occurs only at EOF.
    with encoded:
        while True:
            chunk = source.read(65535)
            if not chunk:
                return encoded.getvalue(), total
            total += len(chunk)
            encoded.write(base64.b64encode(chunk).decode('ascii'))


# ══════════════════════════════════════════════════════
#  Upload-time image shrink
# ══════════════════════════════════════════════════════
# Motivation: The upstream LLM gateway (openresty) rejects request bodies
# that exceed its `client_max_body_size` with HTTP 413, regardless of how
# few upstream *tokens* the images actually cost. Claude's vision tokenizer
# internally downsamples to ~1568 px long side anyway, so sending 4K photos
# is pure wire-size waste. Re-encode oversized uploads ONCE on disk.
#
# The real-world 413 offender (conv=mofu0tfayzvuv1, 2026-04-29) was a
# 1024×510 screenshot at 442 KB PNG — would easily drop to ~100 KB after
# this step with no visible quality loss.
#
# See §10.1: these constants are tuned conservatively to preserve quality.
MAX_UPLOAD_LONG_SIDE_PX = 2048
"""Maximum long-side pixel dimension. Above this, images are downscaled.
Claude internally resizes to ~1568 px, so 2048 leaves a small quality buffer."""

JPEG_REENCODE_QUALITY = 90
"""JPEG quality when re-encoding non-alpha images. q=90 preserves text-on-
screenshot fidelity while still shrinking 4-5× vs raw PNG."""

SHRINK_SKIP_LONG_SIDE_PX = 1600
"""If the image is already ≤ this long side AND ≤ SHRINK_SKIP_MAX_BYTES,
skip re-encoding entirely (preserves original perfect quality)."""

SHRINK_SKIP_MAX_BYTES = 400 * 1024
"""Skip re-encoding if already under this byte size (and within dims)."""


def get_upload_policy() -> dict:
    """Public view of the upload-shrink constants.

    The frontend's ``compressImage()`` mirrors this exact policy so both
    sides agree on when to re-encode and at what quality. Returned fields:

    * ``max_long_side_px`` — hard cap on long side; larger images are
      LANCZOS-downscaled before re-encode.
    * ``jpeg_quality`` — JPEG quality (1-100) used when re-encoding opaque
      images.
    * ``skip_long_side_px`` / ``skip_max_bytes`` — if an image is already
      smaller than BOTH, it passes through untouched (lossless preservation).
    """
    return {
        'max_long_side_px': MAX_UPLOAD_LONG_SIDE_PX,
        'jpeg_quality': JPEG_REENCODE_QUALITY,
        'skip_long_side_px': SHRINK_SKIP_LONG_SIDE_PX,
        'skip_max_bytes': SHRINK_SKIP_MAX_BYTES,
    }


def _shrink_upload_image(img_bytes: bytes | BinaryIO, detected_fmt: str) -> tuple[bytes | BinaryIO, str, dict]:
    """Downscale + re-encode an uploaded image if it exceeds wire-size targets.

    Never modifies GIFs (they may be animated) or BMPs (edge case, rare).
    Preserves PNG format when the source has an alpha channel; otherwise
    re-encodes photographic/opaque content as JPEG q=90 for better wire size.

    Returns:
        (new_bytes, new_ext, info_dict). If unchanged, returns the original
        bytes, the original extension, and info with ``shrunk=False``.
    """
    source_bytes = _image_source_length(img_bytes)
    info = {'shrunk': False, 'reason': '', 'original_bytes': source_bytes}

    # Never touch GIF (animated) or BMP (uncommon, tiny population)
    if detected_fmt in ('gif', 'bmp'):
        info['reason'] = f'format={detected_fmt} skipped'
        ext = '.' + ('jpg' if detected_fmt == 'jpeg' else detected_fmt)
        return img_bytes, ext, info

    try:
        from PIL import Image
    except ImportError as e:
        logger.debug('[Upload] PIL unavailable, using fallback: %s', e)
        info['reason'] = 'pillow_missing'
        ext = '.' + ('jpg' if detected_fmt == 'jpeg' else detected_fmt)
        return img_bytes, ext, info

    try:
        img = Image.open(_image_decode_source(img_bytes))
        img.load()  # force full decode so we catch corrupt files here
    except Exception as e:
        logger.warning('[UploadShrink] PIL open failed (%s) — keeping original', e)
        info['reason'] = f'pil_error:{e}'
        ext = '.' + ('jpg' if detected_fmt == 'jpeg' else detected_fmt)
        return img_bytes, ext, info

    w, h = img.size
    long_side = max(w, h)
    has_alpha = (img.mode in ('RGBA', 'LA')) or (
        img.mode == 'P' and 'transparency' in img.info
    )

    # Screenshots are frequently saved as RGBA but with a fully-opaque alpha
    # channel (e.g. macOS screen-capture). The alpha is useless, and keeping
    # PNG-with-alpha means we lose the ~3× JPEG wire-size win.  Detect a
    # degenerate alpha channel and treat the image as opaque.
    if has_alpha:
        try:
            probe = img if img.mode in ('RGBA', 'LA') else img.convert('RGBA')
            alpha_channel = probe.split()[-1]
            alpha_min, alpha_max = alpha_channel.getextrema()
            if alpha_min == 255 and alpha_max == 255:
                has_alpha = False
                # Strip the redundant alpha so the JPEG branch below works
                img = img.convert('RGB')
        except Exception as e:
            logger.debug('[UploadShrink] alpha probe failed (%s) — keeping alpha path', e)

    # Skip if already small enough
    if long_side <= SHRINK_SKIP_LONG_SIDE_PX and source_bytes <= SHRINK_SKIP_MAX_BYTES:
        info['reason'] = (f'already_small dims={w}x{h} bytes={source_bytes}')
        ext = '.' + ('jpg' if detected_fmt == 'jpeg' else detected_fmt)
        return img_bytes, ext, info

    # Downscale if over the long-side limit
    new_w, new_h = w, h
    if long_side > MAX_UPLOAD_LONG_SIDE_PX:
        scale = MAX_UPLOAD_LONG_SIDE_PX / long_side
        new_w = max(1, int(round(w * scale)))
        new_h = max(1, int(round(h * scale)))
        try:
            img = img.resize((new_w, new_h), Image.LANCZOS)
        except Exception as e:
            logger.warning('[UploadShrink] resize failed (%s) — keeping original', e)
            info['reason'] = f'resize_error:{e}'
            ext = '.' + ('jpg' if detected_fmt == 'jpeg' else detected_fmt)
            return img_bytes, ext, info

    # Re-encode
    buf = io.BytesIO()
    try:
        if has_alpha:
            # Preserve alpha → stay PNG. optimize=True strips metadata + picks
            # best filters; typically cuts screenshot PNGs by 30-50%.
            if img.mode == 'P':
                img = img.convert('RGBA')
            img.save(buf, format='PNG', optimize=True)
            new_ext = '.png'
            new_fmt = 'png'
        else:
            # No alpha → JPEG q=90 gives best shrink ratio at invisible quality loss
            if img.mode != 'RGB':
                img = img.convert('RGB')
            img.save(buf, format='JPEG', quality=JPEG_REENCODE_QUALITY,
                     optimize=True, progressive=True)
            new_ext = '.jpg'
            new_fmt = 'jpeg'
    except Exception as e:
        logger.warning('[UploadShrink] re-encode failed (%s) — keeping original', e)
        info['reason'] = f'encode_error:{e}'
        ext = '.' + ('jpg' if detected_fmt == 'jpeg' else detected_fmt)
        return img_bytes, ext, info

    new_bytes = buf.getvalue()

    # Sanity check: if re-encode somehow got LARGER (rare — tiny already-JPEGs
    # with high-entropy noise), keep original.
    if len(new_bytes) >= source_bytes and (new_w, new_h) == (w, h):
        info['reason'] = f'reencode_bigger {len(new_bytes)}>={source_bytes}'
        ext = '.' + ('jpg' if detected_fmt == 'jpeg' else detected_fmt)
        return img_bytes, ext, info

    info.update({
        'shrunk': True,
        'reason': 'resized' if (new_w, new_h) != (w, h) else 'reencoded',
        'from_dims': f'{w}x{h}',
        'to_dims': f'{new_w}x{new_h}',
        'from_fmt': detected_fmt,
        'to_fmt': new_fmt,
        'new_bytes': len(new_bytes),
        'ratio': round(len(new_bytes) / max(1, source_bytes), 3),
    })
    return new_bytes, new_ext, info


# ══════════════════════════════════════════════════════
#  Image Upload / Serve
# ══════════════════════════════════════════════════════

@upload_bp.route('/api/images/upload', methods=['POST'])
def upload_image():
    if request.is_json:
        data = parse_body(exact=('base64', 'mediaType',))
        encoded = data.get('base64', '')
        if not isinstance(encoded, str) or not encoded:
            return api_bad_request('Invalid base64 payload')
        media_type = data.get('mediaType', 'image/png')
        if media_type not in ('image/png', 'image/jpeg', 'image/jpg', 'image/gif', 'image/webp', 'image/bmp'):
            return api_error('Unsupported image type — SVG uploads are disabled for security. '
                             'Allowed: png, jpeg, gif, webp, bmp.', status=400)
        with tempfile.TemporaryFile(mode='w+b') as source:
            try:
                # Decode aligned base64 chunks to the existing local-file path;
                # retain the complete source without a second whole byte copy.
                for offset in range(0, len(encoded), 64 * 1024):
                    source.write(base64.b64decode(encoded[offset:offset+64 * 1024], validate=True))
            except (ValueError, TypeError) as error:
                logger.warning('[upload_image] base64 decode failed: %s',error)
                return api_bad_request('Invalid base64 payload')
            return _persist_uploaded_image(source)

    files = request_files()
    if 'file' not in files:
        return api_bad_request('No file')
    upload = files['file']
    if not upload.filename:
        return api_bad_request('No filename')
    extension = os.path.splitext(upload.filename)[1].lower()
    if extension not in ('.png','.jpg','.jpeg','.gif','.webp','.bmp'):
        return api_error('Unsupported image type — SVG uploads are disabled for security. '
                         'Allowed: .png, .jpg, .jpeg, .gif, .webp, .bmp.',status=400)
    return _persist_uploaded_image(upload.stream, upload.filename)


def _persist_uploaded_image(source, original_filename=None):
    source.seek(0)
    detected = _detect_image_format(source.read(32))
    if detected not in ('png','jpeg','gif','webp','bmp'):
        return api_bad_request('Payload does not match any supported image format')
    dimension_error = _upload_image_dimension_error(source)
    if dimension_error:
        message, oversized = dimension_error
        return api_error(message,status=413 if oversized else 400)
    try:
        image_source, extension, shrink_info = _shrink_upload_image(source,detected)
    except Exception as error:
        logger.warning('[upload_image] shrink failed: %s — keeping original',error,exc_info=True)
        image_source,extension,shrink_info = source,'.'+('jpg' if detected=='jpeg' else detected),{'shrunk':False}
    stem = ''
    if original_filename is not None:
        from werkzeug.utils import secure_filename
        value = os.path.splitext(secure_filename(os.path.basename(original_filename)))[0]
        stem = '_' + (value or 'image')[:80]
    filename = f"{int(time.time()*1000)}_{os.urandom(4).hex()}{stem}{extension}"
    filepath = os.path.join(UPLOAD_DIR,filename)
    try:
        os.makedirs(UPLOAD_DIR,exist_ok=True)
        with open(filepath,'wb') as output:
            _write_image_source(image_source,output)
    except Exception as error:
        logger.error('[upload_image] save failed: %s',error,exc_info=True)
        try:
            os.unlink(filepath)
        except OSError as cleanup_error:
            logger.warning('[upload_image] partial cleanup failed: %s', cleanup_error)
        return api_internal_error('internal_error')
    logger.info('[upload_image] Saved %s (%d source bytes, shrunk=%s)',filename,
                _image_source_length(source),shrink_info.get('shrunk',False))
    return api_ok({'url':f'/api/images/{filename}','filename':filename})


@upload_bp.route('/api/images/<filename>')
@api_meta(
    responses=media_responses('image/*'),
)
@raw_success_response
def serve_image(filename):
    safe = os.path.basename(filename)
    filepath = os.path.join(UPLOAD_DIR, safe)
    if not os.path.isfile(filepath):
        return api_not_found('Not found')
    return send_file(filepath)


# ══════════════════════════════════════════════════════
#  Image Generation
# ══════════════════════════════════════════════════════

@api_v1_uploads_bp.route('/api/v1/images/generate', methods=['POST'])
def generate_image_route():
    """Generate or edit an image from a text prompt.

    Body JSON:
        prompt: str           — required
        model: str            — optional, dispatch auto-selects
        aspect_ratio: str     — "1:1" | "16:9" | "9:16" | "4:3" | "3:4"
        resolution: str       — "1K" | "2K"
        save: bool            — save to disk (default True)
        history: list[dict]   — optional, prior image gen turns for multi-turn editing
            Each entry: {prompt: str, image_url: str, text: str}
            image_url can be remote (https://...) or local (/api/images/...).
        source_images: list[dict] — optional, images to edit
            Each entry: {image_url: str} or {image_b64: str, mime_type: str}
            image_url can be remote (https://...) or local (/api/images/...).
            When provided, prompt is treated as an edit instruction.
    """
    from lib.image_gen import generate_image

    data = parse_body(exact=('aspect_ratio', 'history', 'model', 'prompt', 'provider_id', 'resolution', 'save', 'source_images',))
    prompt = (data.get('prompt') or '').strip()
    if not prompt:
        return api_bad_request('Missing prompt')

    save_to_disk = data.get('save', True)
    aspect_ratio = data.get('aspect_ratio', '1:1')
    resolution = data.get('resolution', '1K')
    model = (data.get('model') or '').strip()
    history = data.get('history') or None
    raw_source_images = data.get('source_images') or None

    # ── Resolve history image URLs to base64 for multi-turn ──
    # The Gemini API requires inlineData base64 in model turns (URL refs don't work).
    if history:
        resolved_history = _resolve_history_images(history)
        if not resolved_history:
            logger.warning('[ImageGen] All history images failed to resolve, falling back to single-turn')
            history = None
        else:
            history = resolved_history

    # ── Resolve source images for editing ──
    source_images = None
    if raw_source_images:
        source_images = _resolve_history_images(raw_source_images)
        if not source_images:
            logger.warning('[ImageGen] All source images failed to resolve, falling back to generation')

    is_edit = bool(source_images)

    if history:
        logger.info('[ImageGen] Route called: prompt=%.60s model=%s aspect=%s res=%s history_turns=%d edit=%s',
                    prompt[:60], model or '(auto)', aspect_ratio, resolution, len(history), is_edit)
    else:
        logger.info('[ImageGen] Route called: prompt=%.60s model=%s aspect=%s res=%s edit=%s',
                    prompt[:60], model or '(auto)', aspect_ratio, resolution, is_edit)
    t0 = time.time()
    auth = current_auth()
    result = generate_image(
        prompt,
        model=model,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        history=history,
        source_images=source_images,
        owner_user_id=int(request_user_id()),
        tenant_id=auth.tenant_id if auth else None,
        preferred_provider_id=str(data.get('provider_id') or ''),
    )
    elapsed = time.time() - t0

    if not result.get('ok'):
        logger.warning('[ImageGen] Route failed after %.1fs: %s', elapsed, result.get('error', '?')[:200])
        # ── Classify error type for frontend state awareness ──
        error_type = 'generation_failed'
        if result.get('rate_limited'):
            error_type = 'rate_limited'
        elif result.get('block_reason'):
            error_type = 'content_blocked'
        elif 'blocked' in (result.get('error') or '').lower() or 'content policy' in (result.get('error') or '').lower():
            error_type = 'content_blocked'
        elif 'timeout' in (result.get('error') or '').lower():
            error_type = 'timeout'
        elif 'no image_gen slot' in (result.get('error') or '').lower() or 'no image generation model' in (result.get('error') or '').lower():
            error_type = 'no_slot'
        # Pick HTTP status code:
        # - 4xx from provider (client_error True) → surface as 400 (bad prompt/params).
        # - rate_limited → 503 (service unavailable, retry later).
        # - otherwise 500.
        if result.get('client_error'):
            status_code = 400
            if not error_type or error_type == 'generation_failed':
                error_type = 'client_error'
        elif result.get('rate_limited'):
            status_code = 503
        else:
            status_code = 500
        return api_payload({
            'ok': False,
            'error': result.get('error', 'Unknown error'),
            'error_type': error_type,
            'rate_limited': result.get('rate_limited', False),
            'block_reason': result.get('block_reason', ''),
            'text': result.get('text', ''),
            'history_resolved': len(history) if history else 0,
            'provider_status_code': result.get('status_code'),
        }, status_code)

    image_b64 = result.get('image_b64', '')
    image_url = result.get('image_url', '')
    mime_type = result.get('mime_type', 'image/png')
    ext = '.png' if 'png' in mime_type else '.jpg' if 'jpeg' in mime_type or 'jpg' in mime_type else '.webp' if 'webp' in mime_type else '.png'

    response_data = {
        'ok': True,
        'text': result.get('text', ''),
        'mime_type': mime_type,
        'model': result.get('model', ''),
        'provider_id': result.get('provider_id', ''),
        'history_resolved': len(history) if history else 0,
    }

    # Preserve the remote S3 URL for multi-turn history (frontend stores it for next round)
    if image_url and image_url.startswith(('http://', 'https://')):
        response_data['remote_image_url'] = image_url

    if save_to_disk and image_b64:
        raw_bytes = base64.b64decode(image_b64)

        filename = f"gen_{int(time.time()*1000)}{ext}"
        filepath = os.path.join(UPLOAD_DIR, filename)
        try:
            os.makedirs(UPLOAD_DIR, exist_ok=True)
            with open(filepath, 'wb') as f:
                f.write(raw_bytes)
            file_size = len(raw_bytes)
            response_data['image_url'] = f'/api/images/{filename}'
            response_data['filename'] = filename
            response_data['file_size'] = file_size
            logger.info('[ImageGen] Route success: saved %s (%d KB, %.1fs, res=%s%s)',
                        filename, file_size // 1024, elapsed, resolution,
                        ', multi-turn=%d' % len(history) if history else '')
        except Exception as e:
            logger.error('[ImageGen] Save failed: %s', e, exc_info=True)
            response_data['image_b64'] = image_b64
    elif image_b64:
        response_data['image_b64'] = image_b64
        response_data['file_size'] = len(image_b64) * 3 // 4   # approximate decoded size
    elif image_url:
        # FRIDAY returned S3 URL directly (download failed or not attempted)
        response_data['image_url'] = image_url
        logger.info('[ImageGen] Route success: using direct URL (%.1fs)', elapsed)

    return api_ok(response_data)


def _resolve_history_images(history: list) -> list:
    """Resolve image URLs in history entries to base64 for Gemini multi-turn.

    The Gemini API requires ``inlineData`` base64 in model turns (URL refs give 400).
    This function processes each history entry and ensures ``image_b64`` is populated
    by reading from local disk or downloading remote URLs through the SSRF-
    guarded ``_safe_image_fetch``.

    Args:
        history: List of dicts with ``{prompt, image_url, text}``.

    Returns:
        List of resolved history entries with ``image_b64`` populated, or empty
        list if all resolutions failed.
    """
    resolved = []
    for i, turn in enumerate(history):
        image_url = turn.get('image_url', '')
        prompt = turn.get('prompt', '')
        text = turn.get('text', '')
        image_b64 = turn.get('image_b64', '')  # frontend might send pre-resolved b64
        mime_type = 'image/png'

        if image_b64:
            # Already have base64 — just pass through
            resolved.append({
                'prompt': prompt, 'text': text,
                'image_b64': image_b64, 'mime_type': mime_type,
            })
            continue

        if not image_url:
            logger.warning('[ImageGen] History turn %d has no image_url, skipping', i)
            continue

        # ── Local file: /api/images/xxx.png → read from disk ──
        if image_url.startswith('/api/images/'):
            filename = os.path.basename(image_url)
            filepath = os.path.join(UPLOAD_DIR, filename)
            try:
                with open(filepath, 'rb') as f:
                    image_b64, source_bytes = _encode_image_source(f)
                if filename.endswith(('.jpg', '.jpeg')):
                    mime_type = 'image/jpeg'
                elif filename.endswith('.webp'):
                    mime_type = 'image/webp'
                logger.info('[ImageGen] Resolved local history image %s → %d bytes', filename, source_bytes)
            except Exception as e:
                logger.warning('[ImageGen] Failed to read local history image %s: %s', filepath, e)
                continue

        # ── Remote URL: download to an owned file with SSRF checks ──
        elif image_url.startswith(('http://', 'https://')):
            try:
                source, ct = _safe_image_fetch(image_url)
                with source:
                    image_b64, source_bytes = _encode_image_source(source)
            except _ImageFetchError as e:
                logger.warning('[ImageGen] Blocked history image %.80s: %s', image_url[:80], e)
                continue
            except Exception as e:
                logger.warning('[ImageGen] Failed to download history image %.80s: %s',
                               image_url[:80], e)
                continue
            if ct.startswith('image/'):
                mime_type = ct
            logger.info('[ImageGen] Downloaded history image %d: %.80s → %d bytes',
                        i, image_url[:80], source_bytes)
        else:
            logger.warning('[ImageGen] Unrecognized history image_url format: %.80s', image_url[:80])
            continue

        resolved.append({
            'prompt': prompt, 'text': text,
            'image_b64': image_b64, 'mime_type': mime_type,
        })

    return resolved


@api_v1_uploads_bp.route('/api/v1/images/models', methods=['GET'])
def list_image_models():
    """List runnable image Offerings from the request owner's v2 authority."""
    try:
        from lib.model_routing import (
            ModelRoutingRepository,
            OwnerBoundary,
            list_capability_routes,
        )

        auth = current_auth()
        boundary = OwnerBoundary.create(
            int(request_user_id()), auth.tenant_id if auth else None)
        models = [
            route.public_dict()
            for route in list_capability_routes(
                ModelRoutingRepository(), boundary, 'image_gen')
        ]
    except Exception as e:
        logger.warning('[ImageGen] Failed to list models: %s', e)
        models = []

    return api_ok({'models': models})


# ══════════════════════════════════════════════════════
#  PDF Parsing
# ══════════════════════════════════════════════════════

@upload_bp.route('/api/pdf/parse', methods=['POST'])
def parse_pdf():
    files = request_files()
    if 'file' not in files:
        logger.warning('[parse_pdf] No file in request (content_length=%s)',
                       request.content_length)
        return api_bad_request('No file provided')
    file = files['file']
    if not file.filename:
        logger.warning('[parse_pdf] No filename in uploaded file')
        return api_bad_request('No filename')
    pdf_bytes = file.read()
    if pdf_bytes[:5] != b'%PDF-' and not file.filename.lower().endswith('.pdf'):
        logger.warning('[parse_pdf] Not a PDF: %s (header=%.10s)', file.filename, pdf_bytes[:10])
        return api_bad_request('Not a PDF')
    from lib.pdf_parser.pool import (
            PdfParseTimeoutError,
        parse_pdf_pooled as _parse_pdf,
    )
    logger.info('[parse_pdf] Starting parse: %s (%d bytes, %.1f MB)',
                file.filename, len(pdf_bytes), len(pdf_bytes) / 1048576)
    form = request_form()
    try:
        max_text_chars = int(form.get('maxTextChars', 0))
        max_image_width = int(form.get('maxImageWidth', 1024))
        max_images = int(form['maxImages']) if 'maxImages' in form else None

    except (ValueError, TypeError) as e:
        logger.warning('[parse_pdf] Invalid numeric parameter: %s', e, exc_info=True)
        return api_bad_request(f'Invalid numeric parameter: {e}')

    # ── Text-extract strategy ──
    # Per-request override via form field `textMode`, else global env default.
    # `structured` → docling (opt-in heavy dep, falls back to pymupdf4llm if
    #                 docling is not installed or fails).
    # `rich` (default) → pymupdf4llm.
    # `fast` → raw get_text (not used by upload, kept for completeness).
    _requested_mode = (form.get('textMode') or
                       os.environ.get('PDF_TEXT_MODE') or
                       'rich').strip().lower()
    if _requested_mode not in ('rich', 'structured', 'fast'):
        logger.debug('[parse_pdf] Unknown textMode=%r, using rich', _requested_mode)
        _requested_mode = 'rich'

    t0 = time.time()
    try:
        result = _parse_pdf(
            pdf_bytes,
            max_text_chars=max_text_chars,
            max_image_width=max_image_width,
            max_images=max_images,
            text_mode=_requested_mode,
        )
    except PdfParseTimeoutError as e:
        elapsed = time.time() - t0
        logger.warning(
            '[parse_pdf] Timed out for %s (%d bytes) after %.1fs: %s',
            file.filename,
            len(pdf_bytes),
            elapsed,
            e,
        )
        return api_error(
            str(e),
            status=504,
            kind='timeout',
            retryable=True,
        )
    except Exception as e:
        elapsed = time.time() - t0
        logger.error('[parse_pdf] Failed for %s (%d bytes) after %.1fs: %s',
                     file.filename, len(pdf_bytes), elapsed, e, exc_info=True)
        return api_internal_error(f'PDF parsing failed: {str(e)}')
    elapsed = time.time() - t0
    logger.info('[parse_pdf] Success: %s (%d bytes, %.1f MB) — pages=%s, text=%s chars, method=%s, scanned=%s, elapsed=%.1fs',
                file.filename, len(pdf_bytes), len(pdf_bytes) / 1048576,
                result.get('totalPages', '?'), result.get('textLength', '?'),
                result.get('method', '?'), result.get('isScanned', '?'), elapsed)
    return api_ok({'success': True, 'filename': file.filename, 'fileSize': len(pdf_bytes), **result})


@upload_bp.route('/api/pdf/vlm-parse', methods=['POST'])
def pdf_vlm_parse():
    """Start async VLM-based PDF parsing."""
    from lib.pdf_parser.vlm import start_vlm_task

    files = request_files()
    if 'file' not in files:
        logger.warning('[VLM-Parse] No file in request (content_length=%s)',
                       request.content_length)
        return api_bad_request('No file provided')
    file = files['file']
    pdf_bytes = file.read()
    if not pdf_bytes:
        logger.warning('[VLM-Parse] Empty file: %s', file.filename)
        return api_bad_request('Empty file')

    filename = file.filename or 'document.pdf'
    try:
        task_id = start_vlm_task(
            pdf_bytes,
            filename=filename,
            user_id=int(request_user_id()),
        )
    except Exception as e:
        logger.error('[VLM-Parse] Failed to start task for %s (%d bytes): %s',
                     filename, len(pdf_bytes), e, exc_info=True)
        return api_internal_error(f'VLM parse failed to start: {str(e)}')
    logger.info('[VLM-Parse] Started task %s for %s (%d bytes)', task_id, filename, len(pdf_bytes))
    return api_ok({'taskId': task_id})


@api_v1_uploads_bp.route('/api/v1/pdf/vlm-parse/<task_id>', methods=['GET'])
def pdf_vlm_status(task_id):
    """Poll VLM parsing task status."""
    from lib.pdf_parser.vlm import get_vlm_task

    task = get_vlm_task(task_id, user_id=int(request_user_id()))
    if not task:
        return api_not_found('Task not found')
    resp = {
        'status': task['status'],
        'progress': task['progress'],
        'filename': task['filename'],
    }
    if task['status'] == 'done':
        resp['result'] = task['result']
        resp['textLength'] = len(task['result'] or '')
    if task['status'] == 'error':
        resp['error'] = task['error']
    return api_ok(resp)


@api_v1_uploads_bp.route(
    '/api/v1/pdf/vlm-parse/<task_id>', methods=['DELETE'])
def pdf_vlm_cancel(task_id):
    """Cooperatively cancel one owned queued/running VLM parse task."""
    from lib.pdf_parser.vlm import cancel_vlm_task

    cancelled = cancel_vlm_task(
        task_id,
        user_id=int(request_user_id()),
    )
    if cancelled is None:
        return api_not_found('Task not found')
    return api_ok({'cancelled': bool(cancelled)})


@api_v1_uploads_bp.route('/api/v1/pdf/vlm-tasks', methods=['GET'])
def pdf_vlm_find_tasks():
    """Find active VLM tasks by filename — used to reconnect after page refresh."""
    from lib.pdf_parser.vlm import find_vlm_tasks_by_filename

    filename = request.args.get('filename', '')
    if not filename:
        return api_bad_request('filename parameter required')
    tasks = find_vlm_tasks_by_filename(
        filename,
        user_id=int(request_user_id()),
    )
    return api_ok({'tasks': tasks})


# ══════════════════════════════════════════════════════
#  Document Parsing (Word, Excel, PowerPoint, plain text)
# ══════════════════════════════════════════════════════

@upload_bp.route('/api/doc/parse', methods=['POST'])
def parse_document():
    """Parse a non-PDF document file and extract text.

    Supports: .docx, .pptx, .xlsx, and plain text formats.
    """
    from lib.doc_parser import extract_document_text, is_supported_document

    files = request_files()
    if 'file' not in files:
        return api_bad_request('No file provided')
    file = files['file']
    if not file.filename:
        return api_bad_request('No filename')

    filename = file.filename
    if not is_supported_document(filename):
        return api_bad_request(f'Unsupported file format: {filename}')

    try:
        file_bytes = file.read()
    except Exception as exc:
        logger.error('[parse_doc] receive failed for %s: %s', filename, type(exc).__name__)
        return api_internal_error('Document receiving failed')
    if not file_bytes:
        return api_bad_request('Empty file')

    try:
        max_text_chars = int(request_form().get('maxTextChars', 0))
    except (ValueError, TypeError) as e:
        logger.warning('[parse_doc] Invalid maxTextChars: %s', e)
        max_text_chars = 0

    try:
        result = extract_document_text(file_bytes, filename, max_chars=max_text_chars)
    except Exception as e:
        logger.error('[parse_doc] Failed for %s (%d bytes): %s',
                     filename, len(file_bytes), e, exc_info=True)
        return api_internal_error(f'Document parsing failed: {str(e)}')

    logger.info('[parse_doc] Parsed %s (%d bytes), %s chars, method=%s',
                filename, len(file_bytes),
                f'{result.get("textLength", 0):,}', result.get('method', '?'))
    return api_ok({
        'success': True,
        'filename': filename,
        'fileSize': len(file_bytes),
        **result,
    })
