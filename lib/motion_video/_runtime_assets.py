"""Pinned browser-side runtime assets for motion compositions.

HyperFrames renders a scene from its local directory.  Depending on a CDN at
render time makes an otherwise deterministic composition hostage to the
browser's proxy configuration (measured here as HTTP 407): the first frame
still looks valid, but ``gsap`` is undefined and the whole timeline is static.

GSAP is therefore a managed dependency, just like the renderer and scene
fonts: verify the pinned bytes once in the motion cache, copy them into every
scene, and rewrite authored HTML to the scene-local path before any gate or
render sees it.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['GSAP_REL_PATH', 'ensure_gsap', 'localise_gsap_html']

GSAP_VERSION = '3.14.2'
GSAP_FILENAME = f'gsap-{GSAP_VERSION}.min.js'
GSAP_REL_PATH = f'assets/{GSAP_FILENAME}'
GSAP_URL = f'https://cdn.jsdelivr.net/npm/gsap@{GSAP_VERSION}/dist/gsap.min.js'
GSAP_SHA256 = 'c174bfce53a729418d57a8ad8625e7247c793a22fef8e2851e3cfa3de9cd8280'
_MIN_GSAP_BYTES = 60_000


def _valid(path: str) -> bool:
    try:
        if os.path.getsize(path) < _MIN_GSAP_BYTES:
            return False
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(1 << 20), b''):
                h.update(chunk)
        return h.hexdigest() == GSAP_SHA256
    except OSError as exc:
        logger.debug('[MotionVideo] runtime asset validation failed: %s', exc)
        return False


def _atomic_bytes(path: str, data: bytes) -> None:
    from lib.json_store import write_bytes_atomic
    write_bytes_atomic(path, data)


def ensure_gsap(scene_dir: str, *, download: bool = True,
                timeout: int = 30) -> str:
    """Stage the verified GSAP runtime into ``scene_dir`` and return its path.

    Returns an empty string on failure.  The engine treats that as a managed
    dependency failure and keeps its historical HTML path available; callers
    never receive unverified JavaScript.
    """
    target = os.path.join(scene_dir, *GSAP_REL_PATH.split('/'))
    if _valid(target):
        return GSAP_REL_PATH

    from lib.motion_video._env import motion_root
    cached = os.path.join(motion_root(), 'vendor', GSAP_FILENAME)
    if not _valid(cached):
        if not download:
            return ''
        try:
            from lib.http_client import http_get
            response = http_get(GSAP_URL, timeout=timeout)
            data = getattr(response, 'content', b'') or b''
            code = int(getattr(response, 'status_code', 0) or 0)
        except Exception as e:
            logger.warning('[MotionVideo] GSAP fetch failed: %s', e)
            return ''
        digest = hashlib.sha256(data).hexdigest()
        if (code != 200 or len(data) < _MIN_GSAP_BYTES
                or digest != GSAP_SHA256):
            logger.warning('[MotionVideo] rejected GSAP runtime (HTTP %s, '
                           '%d bytes, sha256=%s)', code, len(data), digest)
            return ''
        try:
            _atomic_bytes(cached, data)
        except OSError as e:
            logger.warning('[MotionVideo] could not cache GSAP: %s', e)
            return ''

    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        from lib.json_store import atomic_output_path
        with atomic_output_path(target) as tmp:
            shutil.copy2(cached, tmp)
    except OSError as e:
        logger.warning('[MotionVideo] could not stage GSAP into %s: %s',
                       scene_dir, e)
        return ''
    return GSAP_REL_PATH if _valid(target) else ''


_REMOTE_GSAP_URL = re.compile(
    r'https?://[^"\'<>\s]*gsap[^"\'<>\s]*?\.js(?:\?[^"\'<>\s]*)?',
    re.IGNORECASE)


def localise_gsap_html(html: str, scene_dir: str) -> str:
    """Make a composition independent of CDN/network access.

    Existing authored documents may contain a second ``document.write`` CDN
    fallback.  Replacing every remote GSAP URL (rather than only the first
    script tag) closes that path too while preserving the author's markup.
    """
    rel = ensure_gsap(scene_dir)
    if not rel:
        return html
    out = _REMOTE_GSAP_URL.sub(rel, html or '')
    if rel not in out:
        tag = f'<script src="{rel}"></script>'
        if re.search(r'</head\s*>', out, flags=re.IGNORECASE):
            out = re.sub(r'</head\s*>', tag + '\n</head>', out, count=1,
                         flags=re.IGNORECASE)
        else:
            out = tag + '\n' + out
    return out
