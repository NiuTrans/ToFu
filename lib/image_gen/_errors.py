"""lib/image_gen/_errors.py — shared error types + the image-download helper.

Extracted verbatim from the former flat ``lib/image_gen.py``. These are the
cross-generator primitives: the two exception classes the retry orchestrator
in ``_generate.py`` catches, and ``_download_image`` which every provider
generator falls back to when the API returns a URL instead of inline base64.
"""

import os

from lib.http_client import http_get
from lib.log import get_logger

logger = get_logger(__name__)

# Image-gen API base — fallback only; prefer slot-derived base from dispatch.
# Used when no provider-specific base_url is available from the slot.
_IMAGE_GEN_BASE_DEFAULT = os.environ.get('IMAGE_GEN_BASE_URL', '')


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
        img_resp = http_get(url, timeout=30)
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
