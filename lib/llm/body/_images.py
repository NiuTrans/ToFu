# HOT_PATH
"""Image handling for request bodies — MIME sniffing, validation, downscaling.

Cohesive group:
  - sniff_image_mime(head) -> str|None       (lightweight shared re-export)
  - _validate_image_blocks(messages) -> messages
  - _downscale_oversized_images(messages, model) -> messages
  - _project_images_for_text_model(messages, model, ...) -> int
  - _IMAGE_MAGICS                            (compatibility export)
"""

import base64 as _b64
import io
import os

from lib.image_mime import (
    IMAGE_MIME_MAGICS,
    sniff_image_mime,
)
from lib.log import get_logger
from lib.model_info import claude_line_version, is_claude

logger = get_logger(__name__)
_IMAGE_MAGICS = IMAGE_MIME_MAGICS

# ── Claude image limits ──
# https://platform.claude.com/docs/en/build-with-claude/vision (2026-10-23):
#   • Claude 4.7 and later models use a high-resolution tier of up to 2,576 px
#     on the long edge (4,784 visual tokens, automatic); every other Claude
#     model is downscaled to 1,568 px, so pixels beyond that are wire waste;
#   • a request with more than 20 images must keep EVERY image at or under
#     2,000 px, on every platform;
#   • one image may carry at most 10 MB of base64 on the Claude API and 5 MB
#     on Amazon Bedrock; gateway routes are Bedrock-backed, so 5 MB applies.
# The caps are COUNT-INDEPENDENT. 4.7+ gets 2,000 px rather than 2,576 px
# because the 21st image would otherwise force every earlier image to be
# retro-shrunk on that round — a guaranteed prompt-cache miss (the cliff the
# old 7999/1999 px two-tier design hit at its 5th image). With a fixed
# per-model cap and byte budget an image is re-encoded at most ONCE and then
# skipped by the idempotent check forever, however many images accumulate.
_CLAUDE_STANDARD_IMAGE_MAX_PX = 1568
_CLAUDE_HIGH_RES_IMAGE_MAX_PX = 2000
_CLAUDE_HIGH_RES_GENERATION = (4, 7)
_CLAUDE_LINES = ('opus', 'sonnet', 'haiku', 'fable', 'mythos')
_CLAUDE_IMAGE_MAX_BASE64_BYTES = 5 * 1024 * 1024


def _claude_image_max_px(model: str) -> int:
    """Long-edge cap for *model*: high-res from Claude generation 4.7 on."""
    for line in _CLAUDE_LINES:
        version = claude_line_version(model, line)
        if version is not None and version >= _CLAUDE_HIGH_RES_GENERATION:
            return _CLAUDE_HIGH_RES_IMAGE_MAX_PX
    return _CLAUDE_STANDARD_IMAGE_MAX_PX


def _encode_within_claude_limits(img, max_px: int, max_base64_bytes: int):
    """Re-encode *img* under the long-edge cap and the base64 byte budget.

    Any image with transparency data (RGBA, LA, palette or colour-key
    transparency) becomes lossless RGBA PNG; every other mode becomes RGB
    JPEG q85. An encoding still over the budget shrinks the long edge by the
    square root of the byte ratio (bytes track pixel area) and retries, so
    the result is deterministic and passes the caller's skip check on the
    next round. Returns ``(base64_text, mime, (width, height))``.
    """
    from PIL import Image

    if img.mode != 'RGBA' and img.has_transparency_data:
        # Never flatten onto a matte: JPEG painted transparency black and
        # erased black strokes drawn on it, and white would erase white
        # text and logos. The shrink loop below fits PNG into any budget.
        img = img.convert('RGBA')
    width, height = img.size
    long_side = max(width, height)
    long_edge = min(long_side, max_px)
    while True:
        size = (max(1, width * long_edge // long_side),
                max(1, height * long_edge // long_side))
        out = img if size == img.size else img.resize(size, Image.LANCZOS)
        out.info.pop('icc_profile', None)
        out.info.pop('exif', None)
        if out.mode == 'RGBA':
            out_format, mime = 'PNG', 'image/png'
        else:
            if out.mode != 'RGB':
                out = out.convert('RGB')
            out_format, mime = 'JPEG', 'image/jpeg'
        buf = io.BytesIO()
        out.save(buf, format=out_format, quality=85, optimize=True)
        encoded = _b64.b64encode(buf.getvalue()).decode('ascii')
        if len(encoded) <= max_base64_bytes or long_edge == 1:
            return encoded, mime, size
        long_edge = max(1, int(long_edge * min(
            0.9, 0.95 * (max_base64_bytes / len(encoded)) ** 0.5)))


def _project_images_for_text_model(
        messages: list,
        _target_model: str,
        *,
        fallback_from: str = '',
) -> int:
    """Replace image blocks with bounded provenance markers for a text model.

    ``messages`` must be the request-local deep copy produced by
    ``_strip_non_api_fields``. The durable transcript is never mutated. One
    marker is inserted at the first image position in each image-bearing
    message, regardless of how many images that message carries; this keeps
    prompt growth bounded while retaining temporal/message locality. Adjacent
    captions, ``[image ref: ...]`` text, tool results, and earlier assistant
    descriptions remain byte-for-byte available as textual visual memory.

    Returns the number of image blocks removed.
    """
    total_images = 0

    for message in messages:
        content = message.get('content')
        if not isinstance(content, list):
            continue
        image_count = sum(
            1 for block in content
            if isinstance(block, dict) and block.get('type') == 'image_url'
        )
        if image_count == 0:
            continue

        total_images += image_count
        if fallback_from:
            prefix = (
                f'[Vision fallback projection: {image_count} image(s) in this '
                'message cannot be sent to the text-only fallback model. ')
        else:
            prefix = (
                f'[Text-only image projection: {image_count} image(s) in this '
                'message cannot be sent to the selected text-only model. ')
        notice = (
            prefix
            + 'The original conversation still retains the image data. '
              'Treat the pixels as unseen; rely only on adjacent captions or '
              'image references, tool results, and prior assistant descriptions.]'
        )
        marker = {'type': 'text', 'text': notice}

        projected = []
        marker_inserted = False
        for block in content:
            if isinstance(block, dict) and block.get('type') == 'image_url':
                if not marker_inserted:
                    projected.append(marker)
                    marker_inserted = True
                continue
            projected.append(block)

        if (len(projected) == 1 and isinstance(projected[0], dict)
                and projected[0].get('type') == 'text'):
            message['content'] = projected[0].get('text', '')
        else:
            message['content'] = projected

    return total_images


def _local_images_dir() -> str:
    """Directory behind local ``/api/images/`` URLs.

    Resolve the uploads dir via the single runtime-base authority so local
    /api/images/ URLs resolve on a relocated install (data dir off the code
    tree). Falls back to the legacy in-tree path if runtime_paths is
    somehow unavailable (byte-identical in the default layout).
    """
    try:
        from lib.runtime.runtime_paths import uploads_root
        return os.path.join(uploads_root(), 'images')
    except Exception as _rp_e:
        logger.debug('[ImageValidation] uploads_root() unavailable, using in-tree: %s', _rp_e)
        return os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
            'uploads', 'images')


def _with_image_url(block: dict, url: str) -> dict:
    """Copy of an image_url block whose URL is replaced."""
    return {**block, 'image_url': {**block['image_url'], 'url': url}}


def _validate_image_blocks(messages: list, *, user_id: int | None = None) -> list:
    """Validate image_url blocks, replacing invalid ones with text placeholders.

    Handles:
      1. Local ``/api/images/`` URLs — resolve from disk to inline base64
      2. Corrupted base64
      3. Unrecognized formats
      4. Unknown URL schemes

    Returns the validated list. Only changed messages and blocks are copied;
    the input, possibly shared with the caller, is never modified.
    """
    # Resolved on the first local URL only: uploads_root() creates its
    # directory, a filesystem call every model round would otherwise pay.
    _IMAGES_DIR = None
    _MIN_IMAGE_BYTES = 32
    _EXT_MIME = {
        '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
        '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp',
    }
    _sniff_mime = sniff_image_mime
    _dropped = 0
    _resolved = 0
    result = None

    for index, msg in enumerate(messages):
        content = msg.get('content')
        if not isinstance(content, list):
            continue
        new_blocks = []
        for block in content:
            if not isinstance(block, dict) or block.get('type') != 'image_url':
                new_blocks.append(block)
                continue
            url = block.get('image_url', {}).get('url', '')

            if url.startswith('tofu-media:'):
                try:
                    from lib.identity import require_user_id
                    from lib.knowledge import read_source_path
                    owner = require_user_id(user_id, context='model result image')
                    path = read_source_path(url[len('tofu-media:'):], user_id=owner)
                    if path is None:
                        raise FileNotFoundError('image source unavailable')
                    from lib.turn_image_transport import MAX_TURN_IMAGE_BYTES
                    with path.open('rb') as stream:
                        raw = stream.read(MAX_TURN_IMAGE_BYTES + 1)
                    mime = _sniff_mime(raw)
                    if len(raw) > MAX_TURN_IMAGE_BYTES or not mime:
                        raise ValueError('invalid bounded image source')
                    new_blocks.append(_with_image_url(
                        block, f'data:{mime};base64,{_b64.b64encode(raw).decode("ascii")}'))
                except Exception as exc:
                    logger.warning('[ImageValidation] retained image unavailable: %s', type(exc).__name__)
                    new_blocks.append({'type': 'text', 'text':
                        '[Image unavailable: the retained source cannot be accessed by this owner.]'})
                continue

            # Case 1: Local /api/images/ URL
            _local_prefix = '/api/images/'
            _local_idx = url.find(_local_prefix)
            if _local_idx >= 0:
                filename = url[_local_idx + len(_local_prefix):]
                filename = filename.split('?')[0].split('#')[0]
                filename = os.path.basename(filename)
                if _IMAGES_DIR is None:
                    _IMAGES_DIR = _local_images_dir()
                filepath = os.path.join(_IMAGES_DIR, filename)
                try:
                    with open(filepath, 'rb') as f:
                        from lib.turn_image_transport import MAX_TURN_IMAGE_BYTES
                        raw = f.read(MAX_TURN_IMAGE_BYTES + 1)
                    if len(raw) > MAX_TURN_IMAGE_BYTES:
                        raise ValueError('local image exceeds the byte budget')
                    mime = _sniff_mime(raw)
                    if not mime:
                        ext = os.path.splitext(filename)[1].lower()
                        mime = _EXT_MIME.get(ext, 'image/png')
                    b64 = _b64.b64encode(raw).decode('ascii')
                    _resolved += 1
                    logger.info('[ImageValidation] Resolved local image %s '
                                '(%d bytes) to inline base64', filename, len(raw))
                    new_blocks.append(_with_image_url(block, f'data:{mime};base64,{b64}'))
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
                        block = _with_image_url(
                            block, f'data:{_true_mime}{_suffix},{b64_data}')
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

        if any(new is not old for new, old in zip(new_blocks, content)):
            if result is None:
                result = list(messages)
            result[index] = {**msg, 'content': new_blocks}

    if _resolved:
        logger.info('[ImageValidation] Resolved %d local image(s) to inline base64', _resolved)
    if _dropped:
        logger.info('[ImageValidation] Replaced %d invalid image block(s) with text placeholders', _dropped)

    return messages if result is None else result


def _downscale_oversized_images(messages: list, model: str, *,
                               image_reuse=None, user_id=None) -> list:
    """Re-encode base64 images exceeding Claude's dimension or byte limits.

    Only applies to Claude models. Requires Pillow. Returns the adapted list;
    only messages with a re-encoded image are copied, the input never is.
    """
    if not is_claude(model):
        return messages

    try:
        from PIL import Image
    except ImportError:
        logger.debug('[ImageDownscale] Pillow not installed — skipping image size check')
        return messages

    total_images = 0
    for msg in messages:
        content = msg.get('content')
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get('type') == 'image_url':
                total_images += 1

    # Per-model, count-independent limits (see "Claude image limits"). An image
    # already within both is skipped, so re-encoding is idempotent and a
    # stable image is never re-encoded across rounds → no cache churn.
    max_px = _claude_image_max_px(model)
    max_base64_bytes = _CLAUDE_IMAGE_MAX_BASE64_BYTES

    _resized = 0
    result = None
    for index, msg in enumerate(messages):
        content = msg.get('content')
        if not isinstance(content, list):
            continue
        adapted = None
        for position, block in enumerate(content):
            if not isinstance(block, dict) or block.get('type') != 'image_url':
                continue
            url = block.get('image_url', {}).get('url', '')
            if not url.startswith('data:'):
                continue

            cache_key = None
            if image_reuse is not None:
                cache_key = image_reuse.key(
                    url, (model, max_px, 'lanczos-rgb-jpeg-alpha-png-v3',
                          85, True, 'strip-icc-exif', max_base64_bytes),
                    user_id=user_id)
                cached = image_reuse.get(cache_key)
                if cached is not None:
                    if cached != url:
                        adapted = adapted or list(content)
                        adapted[position] = _with_image_url(block, cached)
                    continue

            parts = url.split(',', 1)
            if len(parts) != 2:
                continue
            header, b64_data = parts
            try:
                raw_bytes = _b64.b64decode(b64_data)
                img = Image.open(io.BytesIO(raw_bytes))
                w, h = img.size

                if max(w, h) <= max_px and len(b64_data) <= max_base64_bytes:
                    img.close()
                    if cache_key is not None:
                        image_reuse.put(cache_key, url)
                    continue

                new_b64, mime, (new_w, new_h) = _encode_within_claude_limits(
                    img, max_px, max_base64_bytes)
                new_url = f'data:{mime};base64,{new_b64}'
                adapted = adapted or list(content)
                adapted[position] = _with_image_url(block, new_url)
                img.close()
                if cache_key is not None:
                    image_reuse.put(cache_key, new_url)
                _resized += 1
                logger.info('[ImageDownscale] Re-encoded %dx%d → %dx%d '
                            '(max_px=%d, %d→%d base64 bytes, images=%d)',
                            w, h, new_w, new_h, max_px,
                            len(b64_data), len(new_b64), total_images)

            except Exception as e:
                logger.warning('[ImageDownscale] Failed to check/resize image: %s', e)
        if adapted is not None:
            if result is None:
                result = list(messages)
            result[index] = {**msg, 'content': adapted}

    if _resized:
        logger.info('[ImageDownscale] Re-encoded %d oversized image(s) for model %s '
                    '(limit=%dpx/%d base64 bytes, total_images=%d)', _resized, model,
                    max_px, max_base64_bytes, total_images)
    return messages if result is None else result
