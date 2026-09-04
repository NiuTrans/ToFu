"""Bounded stock-media provider port with a Pexels implementation.

Responsibility: satisfy renderer-neutral media queries with one local,
attributed asset. Provider credentials remain request-external environment
configuration; returned records contain bounded bytes plus public attribution.
The capability caller owns lifecycle-bound materialisation and persistence.
"""

from __future__ import annotations

import json
import os
from urllib.parse import urlparse

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['resolve_stock_media']

_PEXELS_PHOTO_SEARCH = 'https://api.pexels.com/v1/search'
_PEXELS_VIDEO_SEARCH = 'https://api.pexels.com/v1/videos/search'
_MAX_IMAGE_BYTES = 16 * 1024 * 1024
_MAX_VIDEO_BYTES = 32 * 1024 * 1024
_MAX_SEARCH_RESPONSE_BYTES = 2 * 1024 * 1024
_ALLOWED_MEDIA_HOST_SUFFIXES = ('.pexels.com', '.pexelsusercontent.com')


def _trusted_media_url(value) -> str:
    url = str(value or '').strip()
    parsed = urlparse(url)
    host = (parsed.hostname or '').lower()
    if (parsed.scheme != 'https' or not host
            or not any(host == suffix[1:] or host.endswith(suffix)
                       for suffix in _ALLOWED_MEDIA_HOST_SUFFIXES)):
        raise ValueError('stock provider returned an untrusted media URL')
    return url


def _download(url: str, *, maximum: int, expected_prefix: str) -> tuple[bytes, str]:
    from lib.http_client import http_stream

    chunks: list[bytes] = []
    total = 0
    with http_stream('GET', _trusted_media_url(url), timeout=30) as response:
        response.raise_for_status()
        content_type = str(response.headers.get('content-type') or '') \
            .split(';', 1)[0].strip().lower()
        if not content_type.startswith(expected_prefix):
            raise ValueError(
                f'stock media has unexpected content type {content_type!r}')
        try:
            declared = int(response.headers.get('content-length') or 0)
        except (TypeError, ValueError):
            declared = 0
        if declared > maximum:
            raise ValueError(f'stock media exceeds {maximum} bytes')
        for chunk in response.iter_content(chunk_size=256 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > maximum:
                raise ValueError(f'stock media exceeds {maximum} bytes')
            chunks.append(chunk)
    if not chunks:
        raise ValueError('stock provider returned an empty file')
    return b''.join(chunks), content_type


def _video_file(video: dict) -> str:
    files = [item for item in (video.get('video_files') or [])
             if isinstance(item, dict)
             and str(item.get('file_type') or '').lower() == 'video/mp4'
             and item.get('link')]
    if not files:
        return ''
    # Prefer a bounded HD rendition: large enough for 1080p crops, avoiding a
    # needless 4K download on personal-computer budgets.
    def score(item):
        width = int(item.get('width') or 0)
        return (0 if 960 <= width <= 1920 else 1, abs(width - 1280))
    return str(min(files, key=score).get('link') or '')


def resolve_stock_media(request: dict, *, orientation: str = 'portrait') -> dict:
    """Resolve one normalized media query into bounded bytes + attribution.

    Returns ``{'ok': bool, ...}``; provider absence/outage is a normal degraded
    result, never an exception escaping the production asset preflight.
    """
    key = str(os.environ.get('PEXELS_API_KEY') or '').strip()
    if not key:
        return {'ok': False, 'reason': 'PEXELS_API_KEY is not configured'}
    kind = str(request.get('kind') or 'image').strip().lower()
    query = str(request.get('query') or '').strip()
    if not query:
        return {'ok': False, 'reason': 'stock media query is empty'}
    if kind == 'webpage':
        return {'ok': False,
                'reason': 'webpage capture requires a browser-capture adapter'}
    search_kind = 'video' if kind in ('video', 'gif') else 'image'
    try:
        from lib.http_client import http_get
        endpoint = (_PEXELS_VIDEO_SEARCH if search_kind == 'video'
                    else _PEXELS_PHOTO_SEARCH)
        response = http_get(
            endpoint, headers={'Authorization': key}, timeout=20,
            params={'query': query, 'orientation': orientation,
                    'per_page': 3, 'page': 1})
        response.raise_for_status()
        raw_payload = response.content
        if len(raw_payload) > _MAX_SEARCH_RESPONSE_BYTES:
            raise ValueError('Pexels search response exceeds 2 MiB')
        payload = json.loads(raw_payload)
        if search_kind == 'video':
            items = payload.get('videos') if isinstance(payload, dict) else []
            item = next((value for value in (items or [])
                         if isinstance(value, dict) and _video_file(value)), None)
            if item is None:
                raise ValueError('Pexels returned no usable MP4 result')
            remote_url = _video_file(item)
            data, content_type = _download(
                remote_url, maximum=_MAX_VIDEO_BYTES, expected_prefix='video/')
            suffix = '.mp4'
            creator = item.get('user') if isinstance(item.get('user'), dict) else {}
            creator_name = str(creator.get('name') or '')
            creator_url = str(creator.get('url') or '')
        else:
            items = payload.get('photos') if isinstance(payload, dict) else []
            item = next((value for value in (items or [])
                         if isinstance(value, dict)
                         and isinstance(value.get('src'), dict)
                         and value['src'].get('large2x')), None)
            if item is None:
                raise ValueError('Pexels returned no usable photo result')
            remote_url = str(item['src']['large2x'])
            data, content_type = _download(
                remote_url, maximum=_MAX_IMAGE_BYTES, expected_prefix='image/')
            suffix = {
                'image/png': '.png', 'image/webp': '.webp',
                'image/avif': '.avif',
            }.get(content_type, '.jpg')
            creator_name = str(item.get('photographer') or '')
            creator_url = str(item.get('photographer_url') or '')
        return {
            'ok': True,
            'data': data,
            'suffix': suffix,
            'provider': 'Pexels',
            'requested_kind': kind,
            'media_kind': search_kind,
            'query': query,
            'page_url': str(item.get('url') or ''),
            'creator': creator_name,
            'creator_url': creator_url,
            'provider_url': 'https://www.pexels.com',
            'license_hint': 'Pexels API terms',
        }
    except Exception as exc:
        logger.warning('[StockMedia] Pexels %s query failed: %s',
                       search_kind, exc)
        return {'ok': False,
                'reason': f'Pexels {search_kind} retrieval failed: {exc}'}
