"""Quart-native response compression middleware.

The application assembly owns registration; this module owns compression
policy, content-addressed artifact caching, and executor offload so no
multi-megabyte body is compressed on the serving event loop.
"""

from __future__ import annotations

import asyncio
import gzip
import logging
from typing import Any

from quart import request

from lib.ttl_cache import TTLCache


try:
    import brotli
except ImportError as exc:  # optional dependency; gzip remains available
    brotli = None
    logging.getLogger('server.lifecycle').info(
        '[Compress] brotli unavailable (%s) — gzip only', exc)


COMPRESS_MIMETYPES = frozenset({
    'text/html', 'text/css', 'text/javascript',
    'application/javascript', 'application/json',
})
COMPRESS_MIN_SIZE = 256
COMPRESS_CACHE = TTLCache(ttl=6 * 3600, max_size=48)
COMPRESS_CACHE_MAX_BYTES = 8 * 1024 * 1024
BR_QUALITY_CACHED = 9
BR_QUALITY_LIVE = 4


def compress_bytes(data: bytes, encoding: str, quality: int) -> bytes:
    """Compress bytes synchronously; callers must run this off the loop."""
    if encoding == 'br' and brotli is not None:
        return brotli.compress(data, quality=quality)
    return gzip.compress(data, 6)


async def compress_response(response: Any) -> Any:
    """Compress an eligible whole response without changing wire semantics."""
    if (response.content_type
            and 'text/event-stream' in response.content_type):
        return response
    if response.content_encoding:
        return response
    # A compressed partial response no longer describes Content-Range bytes.
    if response.status_code != 200 or 'Content-Range' in response.headers:
        return response

    accept_encoding = request.headers.get('Accept-Encoding', '')
    if brotli is not None and 'br' in accept_encoding:
        encoding = 'br'
    elif 'gzip' in accept_encoding:
        encoding = 'gzip'
    else:
        return response

    mimetype = (response.content_type or '').split(';')[0].strip()
    if mimetype not in COMPRESS_MIMETYPES:
        return response
    data = await response.get_data()
    if len(data) < COMPRESS_MIN_SIZE:
        return response

    etag = response.headers.get('ETag', '')
    cache_key = None
    if (etag and len(data) <= COMPRESS_CACHE_MAX_BYTES
            and request.path.startswith('/static/')):
        cache_key = (etag, encoding)

    compressed = COMPRESS_CACHE.get(cache_key) if cache_key else None
    if compressed is None:
        quality = BR_QUALITY_CACHED if cache_key else BR_QUALITY_LIVE
        compressed = await asyncio.get_running_loop().run_in_executor(
            None, compress_bytes, data, encoding, quality)
        if cache_key:
            COMPRESS_CACHE.set(cache_key, compressed)

    if len(compressed) >= len(data):
        return response
    response.set_data(compressed)
    response.headers['Content-Encoding'] = encoding
    response.headers['Content-Length'] = len(compressed)
    response.headers.pop('Vary', None)
    response.headers['Vary'] = 'Accept-Encoding'
    return response


def register_http_compression(app: Any) -> bool:
    """Install compression once on an assembled Quart application."""
    key = 'tofu_http_compression_registered'
    if app.extensions.get(key):
        return False
    app.after_request(compress_response)
    app.extensions[key] = True
    return True


__all__ = [
    'BR_QUALITY_CACHED',
    'BR_QUALITY_LIVE',
    'COMPRESS_CACHE',
    'COMPRESS_CACHE_MAX_BYTES',
    'COMPRESS_MIMETYPES',
    'COMPRESS_MIN_SIZE',
    'brotli',
    'compress_bytes',
    'compress_response',
    'register_http_compression',
]
