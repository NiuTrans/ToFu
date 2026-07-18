# HOT_PATH
"""Image handling for request bodies — MIME sniffing, validation, downscaling.

Cohesive group:
  - sniff_image_mime(head) -> str|None       (magic-byte MIME detection)
  - _validate_image_blocks(messages) -> messages
  - _downscale_oversized_images(messages, model) -> None
  - _CLAUDE_IMAGE_MAX_PX / _IMAGE_MAGICS      (module constants)
"""

import base64 as _b64
import io
import os

from lib.log import get_logger
from lib.model_info import is_claude

logger = get_logger(__name__)

# ── Claude image dimension cap ──
# Claude's vision tower internally downscales EVERY image to a ~1568px long
# edge (~1.15 MP) before tokenization — https://docs.claude.com/en/docs/
# build-with-claude/vision — so anything larger conveys the SAME information
# to the model while wasting wire bytes. A single uniform cap at 1568px:
#   • is well under BOTH hard-reject limits (8000px single / 2000px many-image),
#     so neither HTTP 400 can ever fire;
#   • loses NO quality (the model never sees more than 1568px anyway);
#   • is COUNT-INDEPENDENT, which removes the retroactive re-encode cliff the
#     old two-tier design hit: previously an image rode at up to 7999px until
#     the 5th image arrived, then ALL images were retro-shrunk to 1999px on
#     that round → a guaranteed prompt-cache miss (and, in image-gen chats
#     that cross 5 images fast, per-round churn). At a fixed cap an image is
#     shrunk at most ONCE and then skipped by the idempotent size check
#     forever, regardless of how many images accumulate.
_CLAUDE_IMAGE_MAX_PX = 1568

# Plain-prefix magics for the formats whose signature is a fixed head.
# WebP is deliberately NOT here: its magic is a RIFF container whose 4-byte
# head 'RIFF' is shared with WAV / AVI / ANI, so a bare startswith(b'RIFF')
# would mislabel any RIFF payload as image/webp. WebP is sniffed separately in
# sniff_image_mime() by the full RIFF....WEBP signature. Anthropic accepts
# exactly PNG / JPEG / GIF / WebP, which is the full set covered here.
_IMAGE_MAGICS = {
    b'\x89PNG':    'image/png',
    b'\xff\xd8':   'image/jpeg',
    b'GIF8':       'image/gif',
}


def sniff_image_mime(head: bytes):
    """Return the true image MIME from magic bytes, or None if unrecognized.

    The single source of truth for "what image format are these bytes really?"
    — used both by the OpenAI-side validator (``_validate_image_blocks``) and
    by the Anthropic boundary transform (``anthropic_outbound._media_type_and_data``)
    so the two paths cannot drift.

    WebP requires the full RIFF-container check (bytes 0-3 == 'RIFF' AND bytes
    8-11 == 'WEBP'); a bare 'RIFF' prefix also matches WAV/AVI and must NOT be
    treated as an image.
    """
    if not isinstance(head, (bytes, bytearray)):
        return None
    for magic, mtype in _IMAGE_MAGICS.items():
        if head.startswith(magic):
            return mtype
    if len(head) >= 12 and head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'image/webp'
    return None


def _validate_image_blocks(messages: list) -> list:
    """Validate image_url blocks, replacing invalid ones with text placeholders.

    Handles:
      1. Local ``/api/images/`` URLs — resolve from disk to inline base64
      2. Corrupted base64
      3. Unrecognized formats
      4. Unknown URL schemes

    Mutates messages in-place.
    """
    # Resolve the uploads dir via the single runtime-base authority so local
    # /api/images/ URLs resolve on a relocated install (data dir off the code
    # tree). Falls back to the legacy in-tree path if runtime_paths is
    # somehow unavailable (byte-identical in the default layout).
    try:
        from lib.runtime_paths import uploads_root
        _IMAGES_DIR = os.path.join(uploads_root(), 'images')
    except Exception as _rp_e:
        logger.debug('[ImageValidation] uploads_root() unavailable, using in-tree: %s', _rp_e)
        _IMAGES_DIR = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
            'uploads', 'images')
    _MIN_IMAGE_BYTES = 32
    _EXT_MIME = {
        '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
        '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp',
    }
    _sniff_mime = sniff_image_mime
    _dropped = 0
    _resolved = 0

    for msg in messages:
        content = msg.get('content')
        if not isinstance(content, list):
            continue
        new_blocks = []
        for block in content:
            if not isinstance(block, dict) or block.get('type') != 'image_url':
                new_blocks.append(block)
                continue
            url = block.get('image_url', {}).get('url', '')

            # Case 1: Local /api/images/ URL
            _local_prefix = '/api/images/'
            _local_idx = url.find(_local_prefix)
            if _local_idx >= 0:
                filename = url[_local_idx + len(_local_prefix):]
                filename = filename.split('?')[0].split('#')[0]
                filename = os.path.basename(filename)
                filepath = os.path.join(_IMAGES_DIR, filename)
                try:
                    with open(filepath, 'rb') as f:
                        raw = f.read()
                    mime = _sniff_mime(raw)
                    if not mime:
                        ext = os.path.splitext(filename)[1].lower()
                        mime = _EXT_MIME.get(ext, 'image/png')
                    b64 = _b64.b64encode(raw).decode('ascii')
                    block['image_url']['url'] = f'data:{mime};base64,{b64}'
                    _resolved += 1
                    logger.info('[ImageValidation] Resolved local image %s '
                                '(%d bytes) to inline base64', filename, len(raw))
                    new_blocks.append(block)
                except FileNotFoundError:
                    _dropped += 1
                    logger.warning('[ImageValidation] Local image file not found: %s', filepath)
                    new_blocks.append({
                        'type': 'text',
                        'text': f'[Image removed — file not found: {filename}]',
                    })
                except Exception as e:
                    _dropped += 1
                    logger.warning('[ImageValidation] Failed to read local image %s: %s', filepath, e)
                    new_blocks.append({
                        'type': 'text',
                        'text': '[Image removed — could not read image file]',
                    })
                continue

            # Case 2: Remote https:// URL — pass through
            if url.startswith('http://') or url.startswith('https://'):
                new_blocks.append(block)
                continue

            # Case 3: data: URI — validate base64 content
            if url.startswith('data:'):
                try:
                    parts = url.split(',', 1)
                    if len(parts) != 2 or not parts[1]:
                        raise ValueError('Missing base64 data after comma')
                    b64_data = parts[1]
                    if len(b64_data) < 50:
                        raise ValueError(f'Base64 data too short ({len(b64_data)} chars)')
                    sample = _b64.b64decode(b64_data[:1364])
                    if len(sample) < _MIN_IMAGE_BYTES:
                        raise ValueError(f'Decoded image too small ({len(sample)} bytes)')
                    _true_mime = _sniff_mime(sample)
                    if not _true_mime:
                        raise ValueError(f'Unrecognized image format (magic: {sample[:4].hex()})')
                    # Reconcile the declared media type with the real bytes.
                    # A mislabeled data URI (e.g. PNG bytes tagged image/jpeg)
                    # is accepted by every OpenAI-compat gateway but HARD-
                    # REJECTED by the Anthropic Messages API with HTTP 400
                    # ("messages.N.content.0.image.source.base64: The image was
                    # specified using the image/jpeg media type, but the image
                    # does not appear to be in that format."), killing the turn.
                    # Rewrite the header to the sniffed type so the outbound
                    # data URI is self-consistent for every provider.
                    _header = parts[0]  # e.g. 'data:image/jpeg;base64'
                    _declared_mime = _header[len('data:'):].split(';', 1)[0].strip().lower()
                    if _declared_mime != _true_mime:
                        _suffix = _header[len('data:') + len(_declared_mime):] if _declared_mime else ';base64'
                        block['image_url']['url'] = f'data:{_true_mime}{_suffix},{b64_data}'
                        logger.warning(
                            '[ImageValidation] Corrected mislabeled image media '
                            'type %r → %r (bytes sniffed as %s) to satisfy strict '
                            'validators (Anthropic Messages API)',
                            _declared_mime or '(none)', _true_mime, _true_mime)
                    new_blocks.append(block)
                except Exception as e:
                    _dropped += 1
                    logger.warning('[ImageValidation] Dropping invalid base64 image: %s '
                                   '(data_len=%d)', e, len(url))
                    new_blocks.append({
                        'type': 'text',
                        'text': '[Image removed — invalid or corrupted image data]',
                    })
                continue

            # Case 4: Unknown URL scheme
            if url:
                _dropped += 1
                logger.warning('[ImageValidation] Dropping unresolvable image URL: %.100s', url)
                new_blocks.append({
                    'type': 'text',
                    'text': '[Image removed — unresolvable image URL]',
                })
            else:
                _dropped += 1
                new_blocks.append({
                    'type': 'text',
                    'text': '[Image removed — empty image URL]',
                })

        msg['content'] = new_blocks

    if _resolved:
        logger.info('[ImageValidation] Resolved %d local image(s) to inline base64', _resolved)
    if _dropped:
        logger.info('[ImageValidation] Replaced %d invalid image block(s) with text placeholders', _dropped)

    return messages


def _downscale_oversized_images(messages: list, model: str) -> None:
    """Downscale base64 images exceeding Claude's dimension limits.

    Only applies to Claude models. Requires Pillow.
    Mutates messages in-place.
    """
    if not is_claude(model):
        return

    try:
        from PIL import Image
    except ImportError:
        logger.debug('[ImageDownscale] Pillow not installed — skipping image size check')
        return

    total_images = 0
    for msg in messages:
        content = msg.get('content')
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get('type') == 'image_url':
                total_images += 1

    # Uniform cap — count-independent (see _CLAUDE_IMAGE_MAX_PX rationale). An
    # image already at/under the cap is skipped, so the resize is idempotent
    # and a fixed-size image is never re-encoded across rounds → no cache churn.
    max_px = _CLAUDE_IMAGE_MAX_PX

    _resized = 0
    for msg in messages:
        content = msg.get('content')
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get('type') != 'image_url':
                continue
            url = block.get('image_url', {}).get('url', '')
            if not url.startswith('data:'):
                continue

            parts = url.split(',', 1)
            if len(parts) != 2:
                continue
            header, b64_data = parts
            try:
                raw_bytes = _b64.b64decode(b64_data)
                img = Image.open(io.BytesIO(raw_bytes))
                w, h = img.size

                if max(w, h) <= max_px:
                    continue

                scale = max_px / max(w, h)
                new_w = int(w * scale)
                new_h = int(h * scale)

                img = img.resize((new_w, new_h), Image.LANCZOS)
                img.info.pop('icc_profile', None)
                img.info.pop('exif', None)

                if img.mode == 'RGBA':
                    out_format = 'PNG'
                    mime = 'image/png'
                else:
                    if img.mode != 'RGB':
                        img = img.convert('RGB')
                    out_format = 'JPEG'
                    mime = 'image/jpeg'

                buf = io.BytesIO()
                img.save(buf, format=out_format, quality=85, optimize=True)
                new_b64 = _b64.b64encode(buf.getvalue()).decode('ascii')

                block['image_url']['url'] = f'data:{mime};base64,{new_b64}'
                _resized += 1
                logger.info('[ImageDownscale] Resized %dx%d → %dx%d '
                            '(max_px=%d, %d→%d bytes, images=%d)',
                            w, h, new_w, new_h, max_px,
                            len(raw_bytes), buf.tell(), total_images)

            except Exception as e:
                logger.warning('[ImageDownscale] Failed to check/resize image: %s', e)

    if _resized:
        logger.info('[ImageDownscale] Resized %d oversized image(s) for model %s '
                    '(limit=%dpx, total_images=%d)', _resized, model, max_px, total_images)
