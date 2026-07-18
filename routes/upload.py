"""routes/upload.py — Image upload/serve, image generation, PDF parsing endpoints."""

import base64
import io
import ipaddress
import os
import socket
import time
from urllib.parse import urlparse

from flask import Blueprint, jsonify, request, send_file

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

from lib.env_compat import getenv_compat
from lib.log import get_logger
from lib.api_response import api_bad_request, api_error, api_internal_error, api_not_found
from lib.request_parser import parse_body

logger = get_logger(__name__)

upload_bp = Blueprint('upload', __name__)
# v1 blueprint for the JSON routes (the 5 carve-outs above stay on upload_bp).
from routes.api_v1.uploads import api_v1_uploads_bp  # noqa: E402

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# User-uploaded images are USER STATE and must live under the resolved
# runtime base (co-located with the DB that references them by /api/images/
# URL), NOT the code tree — see lib/runtime_paths.uploads_root(). In the
# default in-tree layout this is byte-identical to <repo>/uploads/images.
from lib.runtime_paths import uploads_root  # noqa: E402
UPLOAD_DIR = os.path.join(uploads_root(), 'images')
os.makedirs(UPLOAD_DIR, exist_ok=True)


# ══════════════════════════════════════════════════════
#  SSRF / size guard for user-supplied image URLs (B10)
# ══════════════════════════════════════════════════════
# /api/images/generate accepts arbitrary `image_url` strings in its
# `history` and `source_images` payloads. Without guards a tunnel-
# authenticated client can probe internal services or cloud-metadata
# endpoints, or stream multi-GB bodies into RAM. ``_safe_image_fetch``
# is the single chokepoint: every place that resolves a remote
# user-supplied image URL goes through it.
#
# Default deny: loopback, link-local, RFC1918, multicast, reserved.
# Override with ``TOFU_IMAGE_FETCH_ALLOW_HOSTS`` (comma-separated
# exact-match hostnames) when self-hosting requires reaching a
# private endpoint.

_DEFAULT_IMAGE_FETCH_MAX_BYTES = 10 * 1024 * 1024  # 10 MB


def _image_fetch_max_bytes() -> int:
    raw = (getenv_compat('TOFU_IMAGE_FETCH_MAX_BYTES') or '').strip()
    if not raw:
        return _DEFAULT_IMAGE_FETCH_MAX_BYTES
    try:
        n = int(raw)
        if n > 0:
            return n
    except ValueError as e:
        logger.debug('[ImageFetch] Invalid TOFU_IMAGE_FETCH_MAX_BYTES=%r: %s', raw, e)
    return _DEFAULT_IMAGE_FETCH_MAX_BYTES


def _image_fetch_allowed_hosts() -> set[str]:
    raw = (getenv_compat('TOFU_IMAGE_FETCH_ALLOW_HOSTS') or '').strip()
    if not raw:
        return set()
    return {h.strip().lower() for h in raw.split(',') if h.strip()}


class _ImageFetchError(Exception):
    """Raised when a user-supplied image URL fails the SSRF / size guard."""


def _is_blocked_ip(ip_str: str) -> bool:
    """Return True if ``ip_str`` is loopback / RFC1918 / link-local / multicast /
    reserved / unspecified. These addresses must not be reachable through a
    user-supplied URL."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError as _e_audit:
        logger.debug('[upload] _is_blocked_ip caught %s: %s', type(_e_audit).__name__, _e_audit)
        return True  # unparseable → deny
    return (ip.is_loopback or ip.is_private or ip.is_link_local
            or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def _safe_image_fetch(url: str, *, timeout: int = 30) -> tuple[bytes, str]:
    """Fetch ``url`` with SSRF + size guards. Returns (body_bytes, content_type).

    Raises ``_ImageFetchError`` (caught by callers) when:
      * scheme is not http/https
      * hostname resolves to a private/loopback/link-local IP (and is not in
        the explicit allow-list)
      * response body exceeds ``TOFU_IMAGE_FETCH_MAX_BYTES`` (default 10 MB)

    The request streams (``stream=True``) and aborts as soon as the size cap
    is reached, so an attacker cannot OOM the server with a huge body.
    """
    from lib.http_client import http_get

    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        raise _ImageFetchError(f'unsupported scheme: {parsed.scheme!r}')
    host = (parsed.hostname or '').lower()
    if not host:
        raise _ImageFetchError('missing hostname')

    allowed = _image_fetch_allowed_hosts()
    if host not in allowed:
        # Resolve every IP the host could land on — checking a single record
        # is not enough (DNS rebinding, multi-homed hosts).
        try:
            infos = socket.getaddrinfo(host, parsed.port,
                                       proto=socket.IPPROTO_TCP)
        except socket.gaierror as e:
            raise _ImageFetchError(f'DNS resolution failed: {e}') from e
        for info in infos:
            ip_str = info[4][0]
            if _is_blocked_ip(ip_str):
                raise _ImageFetchError(
                    f'host {host!r} resolves to blocked IP {ip_str} '
                    f'(loopback/private/link-local/reserved)'
                )

    max_bytes = _image_fetch_max_bytes()
    resp = http_get(url, timeout=timeout, stream=True)
    try:
        resp.raise_for_status()
        # Honor Content-Length if the server is honest; still re-check while
        # streaming since headers can lie.
        cl = resp.headers.get('Content-Length')
        if cl and cl.isdigit() and int(cl) > max_bytes:
            raise _ImageFetchError(
                f'response too large: Content-Length={cl} > max={max_bytes}'
            )
        chunks = []
        total = 0
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise _ImageFetchError(
                    f'response too large: streamed {total} > max={max_bytes}'
                )
            chunks.append(chunk)
        body = b''.join(chunks)
        ct = resp.headers.get('Content-Type', '') or ''
        return body, ct.split(';')[0].strip()
    finally:
        resp.close()


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


def _shrink_upload_image(img_bytes: bytes, detected_fmt: str) -> tuple[bytes, str, dict]:
    """Downscale + re-encode an uploaded image if it exceeds wire-size targets.

    Never modifies GIFs (they may be animated) or BMPs (edge case, rare).
    Preserves PNG format when the source has an alpha channel; otherwise
    re-encodes photographic/opaque content as JPEG q=90 for better wire size.

    Returns:
        (new_bytes, new_ext, info_dict). If unchanged, returns the original
        bytes, the original extension, and info with ``shrunk=False``.
    """
    info = {'shrunk': False, 'reason': '', 'original_bytes': len(img_bytes)}

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
        img = Image.open(io.BytesIO(img_bytes))
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
    if long_side <= SHRINK_SKIP_LONG_SIDE_PX and len(img_bytes) <= SHRINK_SKIP_MAX_BYTES:
        info['reason'] = (f'already_small dims={w}x{h} bytes={len(img_bytes)}')
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
    if len(new_bytes) >= len(img_bytes) and (new_w, new_h) == (w, h):
        info['reason'] = f'reencode_bigger {len(new_bytes)}>={len(img_bytes)}'
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
        'ratio': round(len(new_bytes) / max(1, len(img_bytes)), 3),
    })
    return new_bytes, new_ext, info


# ══════════════════════════════════════════════════════
#  Image Upload / Serve
# ══════════════════════════════════════════════════════

@upload_bp.route('/api/images/upload', methods=['POST'])
def upload_image():
    # ── JSON base64 upload (from frontend uploadImageToServer) ──
    if request.is_json:
        data = parse_body()
        b64_data = data.get('base64', '')
        media_type = data.get('mediaType', 'image/png')
        if not b64_data:
            return api_bad_request('No base64 data')
        # ★ SVG is intentionally excluded — user-supplied SVGs can embed
        # <script> and enable stored XSS when served inline. See §10.4.
        ext_map = {
            'image/png': '.png', 'image/jpeg': '.jpg', 'image/jpg': '.jpg',
            'image/gif': '.gif', 'image/webp': '.webp',
            'image/bmp': '.bmp',
        }
        if media_type not in ext_map:
            logger.warning('[upload_image] Rejected media_type=%s (SVG/other not allowed)', media_type)
            return api_error('Unsupported image type — SVG uploads are disabled for security. '
                             'Allowed: png, jpeg, gif, webp, bmp.', status=400)
        ext = ext_map.get(media_type, '.png')
        try:
            img_bytes = base64.b64decode(b64_data)
        except Exception as e:
            logger.warning('[upload_image] base64 decode failed: %s', e)
            return api_bad_request('Invalid base64 payload')
        # ── Magic-bytes sanity check (defence in depth against content-type spoofing) ──
        detected = _detect_image_format(img_bytes[:32])
        if detected not in ('png', 'jpeg', 'gif', 'webp', 'bmp'):
            logger.warning('[upload_image] Magic-bytes check failed: media_type=%s detected=%s len=%d',
                           media_type, detected, len(img_bytes))
            return api_bad_request('Payload does not match any supported image format')

        # Wire-size shrink (see module-level _shrink_upload_image docstring)
        try:
            img_bytes, ext, shrink_info = _shrink_upload_image(img_bytes, detected)
        except Exception as e:
            logger.warning('[upload_image] shrink unexpectedly raised: %s — keeping original',
                           e, exc_info=True)
            shrink_info = {'shrunk': False, 'reason': f'exc:{e}'}

        # Append a short random suffix so two images uploaded in the SAME
        # millisecond (the frontend now fires parallel uploads) can't collide
        # on one filename — a collision would overwrite the first file and
        # leave BOTH message rows pointing at the same image.
        filename = f"{int(time.time()*1000)}_{os.urandom(4).hex()}{ext}"
        filepath = os.path.join(UPLOAD_DIR, filename)
        try:
            with open(filepath, 'wb') as f:
                f.write(img_bytes)
        except Exception as e:
            logger.error('[Common] image upload (base64) save failed: %s', e, exc_info=True)
            return api_internal_error('internal_error')
        if shrink_info.get('shrunk'):
            logger.info('[upload_image] Saved %s — shrunk %s→%s, %d→%d bytes (ratio=%.2f, %s→%s)',
                        filename, shrink_info.get('from_dims'), shrink_info.get('to_dims'),
                        shrink_info.get('original_bytes'), shrink_info.get('new_bytes'),
                        shrink_info.get('ratio'),
                        shrink_info.get('from_fmt'), shrink_info.get('to_fmt'))
        else:
            logger.info('[upload_image] Saved %s (%d bytes) from base64 (detected=%s, shrink_skipped=%s)',
                        filename, len(img_bytes), detected, shrink_info.get('reason', '?'))
        return jsonify({'ok': True, 'url': f'/api/images/{filename}', 'filename': filename})

    # ── Multipart form upload (traditional file upload) ──
    if 'file' not in request.files:
        return api_bad_request('No file')
    file = request.files['file']
    if not file.filename:
        return api_bad_request('No filename')
    ext = os.path.splitext(file.filename)[1].lower()
    # ★ SVG is intentionally excluded — user-supplied SVGs can embed <script>
    # and enable stored XSS when served inline. See §10.4.
    if ext not in ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'):
        logger.warning('[upload_image] Rejected extension=%s (SVG/other not allowed)', ext)
        return api_error('Unsupported image type — SVG uploads are disabled for security. '
                         'Allowed: .png, .jpg, .jpeg, .gif, .webp, .bmp.', status=400)
    # ── Magic-bytes sanity check ──
    head = file.stream.read(32)
    file.stream.seek(0)
    detected = _detect_image_format(head)
    if detected not in ('png', 'jpeg', 'gif', 'webp', 'bmp'):
        logger.warning('[upload_image] Magic-bytes check failed: ext=%s detected=%s', ext, detected)
        return api_bad_request('Payload does not match any supported image format')

    # Read full bytes so we can run the wire-size shrink before writing to disk
    try:
        file.stream.seek(0)
        raw_bytes = file.stream.read()
    except Exception as e:
        logger.error('[upload_image] failed to read uploaded stream: %s', e, exc_info=True)
        return api_internal_error('internal_error')

    try:
        new_bytes, new_ext, shrink_info = _shrink_upload_image(raw_bytes, detected)
    except Exception as e:
        logger.warning('[upload_image] shrink unexpectedly raised: %s — keeping original',
                       e, exc_info=True)
        new_bytes, new_ext, shrink_info = raw_bytes, ext, {'shrunk': False, 'reason': f'exc:{e}'}

    # Preserve user-supplied filename stem but use the (possibly new) extension
    stem = os.path.splitext(file.filename)[0]
    filename = f"{int(time.time()*1000)}_{os.urandom(4).hex()}_{stem}{new_ext}"
    filepath = os.path.join(UPLOAD_DIR, filename)
    try:
        with open(filepath, 'wb') as f:
            f.write(new_bytes)
    except Exception as e:
        logger.error('[Common] image upload save failed: %s', e, exc_info=True)
        return api_internal_error('internal_error')
    if shrink_info.get('shrunk'):
        logger.info('[upload_image] Saved %s — shrunk %s→%s, %d→%d bytes (ratio=%.2f, %s→%s)',
                    filename, shrink_info.get('from_dims'), shrink_info.get('to_dims'),
                    shrink_info.get('original_bytes'), shrink_info.get('new_bytes'),
                    shrink_info.get('ratio'),
                    shrink_info.get('from_fmt'), shrink_info.get('to_fmt'))
    else:
        logger.info('[upload_image] Saved %s (%d bytes) detected=%s shrink_skipped=%s',
                    filename, len(new_bytes), detected, shrink_info.get('reason', '?'))
    return jsonify({'ok': True, 'url': f'/api/images/{filename}', 'filename': filename})


@upload_bp.route('/api/images/<filename>')
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

    data = parse_body()
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
    result = generate_image(prompt, model=model, aspect_ratio=aspect_ratio,
                            resolution=resolution, history=history,
                            source_images=source_images)
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
        return jsonify({
            'ok': False,
            'error': result.get('error', 'Unknown error'),
            'error_type': error_type,
            'rate_limited': result.get('rate_limited', False),
            'block_reason': result.get('block_reason', ''),
            'text': result.get('text', ''),
            'history_resolved': len(history) if history else 0,
            'provider_status_code': result.get('status_code'),
        }), status_code

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

    return jsonify(response_data)


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
                    raw = f.read()
                image_b64 = base64.b64encode(raw).decode('ascii')
                if filename.endswith(('.jpg', '.jpeg')):
                    mime_type = 'image/jpeg'
                elif filename.endswith('.webp'):
                    mime_type = 'image/webp'
                logger.info('[ImageGen] Resolved local history image %s → %d bytes', filename, len(raw))
            except Exception as e:
                logger.warning('[ImageGen] Failed to read local history image %s: %s', filepath, e)
                continue

        # ── Remote URL: download with SSRF + size guard ──
        elif image_url.startswith(('http://', 'https://')):
            try:
                body, ct = _safe_image_fetch(image_url)
            except _ImageFetchError as e:
                logger.warning('[ImageGen] Blocked history image %.80s: %s', image_url[:80], e)
                continue
            except Exception as e:
                logger.warning('[ImageGen] Failed to download history image %.80s: %s',
                               image_url[:80], e)
                continue
            image_b64 = base64.b64encode(body).decode('ascii')
            if ct.startswith('image/'):
                mime_type = ct
            logger.info('[ImageGen] Downloaded history image %d: %.80s → %d bytes',
                        i, image_url[:80], len(body))
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
    """List available image generation models from dispatch config."""
    models = []
    try:
        from lib.llm_dispatch import get_dispatcher
        disp = get_dispatcher()

        # Build provider_id → name mapping from saved config
        prov_names = {}
        try:
            from routes.config import _read_server_config
            saved = _read_server_config()
            for p in saved.get('providers', []):
                prov_names[p.get('id', '')] = p.get('name', p.get('id', ''))
        except Exception as e:
            logger.debug('Failed to read server config for provider names: %s', e)

        seen_per_provider = set()  # (provider_id, model) dedup — allow same model on different providers
        for slot in disp.slots:
            caps = getattr(slot, 'capabilities', set())
            if 'image_gen' not in caps:
                continue
            pid = getattr(slot, 'provider_id', 'default')
            key = (pid, slot.model)
            if key in seen_per_provider:
                continue
            seen_per_provider.add(key)
            models.append({
                'model': slot.model,
                'available': slot.is_available,
                'provider_id': pid,
                'provider_name': prov_names.get(pid, pid),
            })
    except Exception as e:
        logger.warning('[ImageGen] Failed to list models: %s', e)

    return jsonify({'models': models})


# ══════════════════════════════════════════════════════
#  PDF Parsing
# ══════════════════════════════════════════════════════

@upload_bp.route('/api/pdf/parse', methods=['POST'])
def parse_pdf():
    from lib.pdf_parser._common import MAX_PDF_BYTES
    if 'file' not in request.files:
        logger.warning('[parse_pdf] No file in request (content_length=%s)',
                       request.content_length)
        return api_bad_request('No file provided')
    if request.content_length and request.content_length > MAX_PDF_BYTES:
        logger.warning('[parse_pdf] File too large by content_length: %d bytes (%.1f MB, max %d MB)',
                       request.content_length, request.content_length / 1048576,
                       MAX_PDF_BYTES // 1048576)
        return api_bad_request(f'File too large (max {MAX_PDF_BYTES // 1048576}MB)')
    file = request.files['file']
    if not file.filename:
        logger.warning('[parse_pdf] No filename in uploaded file')
        return api_bad_request('No filename')
    pdf_bytes = file.read()
    if len(pdf_bytes) > MAX_PDF_BYTES:
        logger.warning('[parse_pdf] File too large: %s (%d bytes, max %d)',
                       file.filename, len(pdf_bytes), MAX_PDF_BYTES)
        return api_bad_request('File too large')
    if pdf_bytes[:5] != b'%PDF-' and not file.filename.lower().endswith('.pdf'):
        logger.warning('[parse_pdf] Not a PDF: %s (header=%.10s)', file.filename, pdf_bytes[:10])
        return api_bad_request('Not a PDF')
    from lib.pdf_parser.pool import parse_pdf_pooled as _parse_pdf
    logger.info('[parse_pdf] Starting parse: %s (%d bytes, %.1f MB)',
                file.filename, len(pdf_bytes), len(pdf_bytes) / 1048576)
    try:
        max_text_chars = int(request.form.get('maxTextChars', 0))
        max_image_width = int(request.form.get('maxImageWidth', 1024))
        max_images = int(request.form.get('maxImages', 20))

    except (ValueError, TypeError) as e:
        logger.warning('[parse_pdf] Invalid numeric parameter: %s', e, exc_info=True)
        return api_bad_request(f'Invalid numeric parameter: {e}')

    # ── Text-extract strategy ──
    # Per-request override via form field `textMode`, else global env default.
    # `structured` → docling (opt-in heavy dep, falls back to pymupdf4llm if
    #                 docling is not installed or fails).
    # `rich` (default) → pymupdf4llm.
    # `fast` → raw get_text (not used by upload, kept for completeness).
    _requested_mode = (request.form.get('textMode') or
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
    return jsonify({'success': True, 'filename': file.filename, 'fileSize': len(pdf_bytes), **result})


@upload_bp.route('/api/pdf/vlm-parse', methods=['POST'])
def pdf_vlm_parse():
    """Start async VLM-based PDF parsing."""
    from lib.pdf_parser import start_vlm_task
    from lib.pdf_parser._common import MAX_PDF_BYTES

    if 'file' not in request.files:
        logger.warning('[VLM-Parse] No file in request (content_length=%s)',
                       request.content_length)
        return api_bad_request('No file provided')
    if request.content_length and request.content_length > MAX_PDF_BYTES:
        logger.warning('[VLM-Parse] File too large by content_length: %d bytes (%.1f MB, max %d MB)',
                       request.content_length, request.content_length / 1048576,
                       MAX_PDF_BYTES // 1048576)
        return api_bad_request(f'File too large (max {MAX_PDF_BYTES // 1048576}MB)')
    file = request.files['file']
    pdf_bytes = file.read()
    if not pdf_bytes:
        logger.warning('[VLM-Parse] Empty file: %s', file.filename)
        return api_bad_request('Empty file')
    if len(pdf_bytes) > MAX_PDF_BYTES:
        logger.warning('[VLM-Parse] File too large after read: %s (%d bytes, %.1f MB, max %d MB)',
                       file.filename, len(pdf_bytes), len(pdf_bytes) / 1048576,
                       MAX_PDF_BYTES // 1048576)
        return api_bad_request(f'File too large (max {MAX_PDF_BYTES // 1048576}MB)')

    filename = file.filename or 'document.pdf'
    try:
        task_id = start_vlm_task(pdf_bytes, filename=filename)
    except Exception as e:
        logger.error('[VLM-Parse] Failed to start task for %s (%d bytes): %s',
                     filename, len(pdf_bytes), e, exc_info=True)
        return api_internal_error(f'VLM parse failed to start: {str(e)}')
    logger.info('[VLM-Parse] Started task %s for %s (%d bytes)', task_id, filename, len(pdf_bytes))
    return jsonify({'taskId': task_id})


@api_v1_uploads_bp.route('/api/v1/pdf/vlm-parse/<task_id>', methods=['GET'])
def pdf_vlm_status(task_id):
    """Poll VLM parsing task status."""
    from lib.pdf_parser import get_vlm_task

    task = get_vlm_task(task_id)
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
    return jsonify(resp)


@api_v1_uploads_bp.route('/api/v1/pdf/vlm-tasks', methods=['GET'])
def pdf_vlm_find_tasks():
    """Find active VLM tasks by filename — used to reconnect after page refresh."""
    from lib.pdf_parser import find_vlm_tasks_by_filename

    filename = request.args.get('filename', '')
    if not filename:
        return api_bad_request('filename parameter required')
    tasks = find_vlm_tasks_by_filename(filename)
    return jsonify({'tasks': tasks})


# ══════════════════════════════════════════════════════
#  Document Parsing (Word, Excel, PowerPoint, plain text)
# ══════════════════════════════════════════════════════

@upload_bp.route('/api/doc/parse', methods=['POST'])
def parse_document():
    """Parse a non-PDF document file and extract text.

    Supports: .docx, .pptx, .xlsx, and plain text formats.
    """
    from lib import FETCH_MAX_BYTES
    from lib.doc_parser import extract_document_text, is_supported_document

    if 'file' not in request.files:
        return api_bad_request('No file provided')
    if request.content_length and request.content_length > FETCH_MAX_BYTES:
        return api_bad_request(f'File too large (max {FETCH_MAX_BYTES // 1048576}MB)')
    file = request.files['file']
    if not file.filename:
        return api_bad_request('No filename')

    filename = file.filename
    if not is_supported_document(filename):
        return api_bad_request(f'Unsupported file format: {filename}')

    file_bytes = file.read()
    if not file_bytes:
        return api_bad_request('Empty file')
    if len(file_bytes) > FETCH_MAX_BYTES:
        return api_bad_request('File too large')

    try:
        max_text_chars = int(request.form.get('maxTextChars', 0))
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
    return jsonify({
        'success': True,
        'filename': filename,
        'fileSize': len(file_bytes),
        **result,
    })
