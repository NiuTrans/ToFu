"""lib/image_gen/_errors.py — shared error types + the image-download helper.

Extracted verbatim from the former flat ``lib/image_gen.py``. These are the
cross-generator primitives: the two exception classes the retry orchestrator
in ``_generate.py`` catches, and ``_download_image`` which every provider
generator falls back to when the API returns a URL instead of inline base64.
"""

import os

from lib.log import get_logger

logger = get_logger(__name__)

# Image-gen API base — fallback only; prefer slot-derived base from dispatch.
# Used when no provider-specific base_url is available from the slot.
_IMAGE_GEN_BASE_DEFAULT = os.environ.get('IMAGE_GEN_BASE_URL', '')
_MAX_GENERATED_IMAGE_BYTES = 32 * 1024 * 1024


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
        from lib.http_client import http_stream
        logger.info('[ImageGen] Downloading image from URL: %.120s', url)
        raw = bytearray()
        with http_stream('GET', url, timeout=30) as img_resp:
            img_resp.raise_for_status()
            declared_raw = img_resp.headers.get('Content-Length')
            try:
                declared = (int(declared_raw)
                            if declared_raw is not None else None)
            except (TypeError, ValueError):
                declared = None
            if declared is not None and declared > _MAX_GENERATED_IMAGE_BYTES:
                raise ValueError(
                    f'generated image declares {declared} bytes; limit is '
                    f'{_MAX_GENERATED_IMAGE_BYTES}')
            for chunk in img_resp.iter_content(chunk_size=256 * 1024):
                if not chunk:
                    continue
                if len(raw) + len(chunk) > _MAX_GENERATED_IMAGE_BYTES:
                    raise ValueError(
                        f'generated image stream exceeds '
                        f'{_MAX_GENERATED_IMAGE_BYTES} bytes')
                raw.extend(chunk)
            ct = img_resp.headers.get('Content-Type', '')
        if not raw:
            raise ValueError('generated image download was empty')
        image_b64 = _b64.b64encode(raw).decode('ascii')
        if ct.startswith('image/'):
            mime = ct.split(';')[0].strip()
        elif url.endswith(('.jpg', '.jpeg')):
            mime = 'image/jpeg'
        elif url.endswith('.webp'):
            mime = 'image/webp'
        else:
            mime = default_mime
        logger.info('[ImageGen] Downloaded %d bytes → %d chars b64, mime=%s',
                    len(raw), len(image_b64), mime)
        return image_b64, mime
    except Exception as dl_e:
        logger.error('[ImageGen] Failed to download image from %s: %s', url, dl_e, exc_info=True)
        return None, default_mime
