"""CSS bundler — content-hash app stylesheets so browser caches invalidate
automatically when the source changes.

Mirror of `lib.js_bundler` for a much simpler case: there's only ONE app
stylesheet (`static/styles.css`), but it's referenced from `index.html`
with a hardcoded `?v=20260511a` query string. Bumping that string by hand
on every CSS edit is error-prone — when forgotten, browsers serve stale
cached CSS even after a hard refresh, because the URL hasn't changed.

This module:
  1. Hashes the on-disk styles.css contents (first 8 chars of SHA-256).
  2. Exposes `get_styles_link_tag()` which returns
     `<link rel="stylesheet" href="static/styles.css?v=<hash>">`.
  3. `routes/common.py` swaps the `?v=...` query in the served HTML using
     `_STYLES_LINK_RE` so the URL changes whenever the file content does.

No file copy or rewrite — we keep `static/styles.css` as the single source
of truth and just append a content-derived query string. That's enough
to bust the browser cache on every edit while preserving the simple
`/static/styles.css` URL for direct access.
"""
import hashlib
import os
import time

from lib.log import get_logger

logger = get_logger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STYLES_PATH = os.path.join(BASE_DIR, 'static', 'styles.css')

# Cached state — recomputed when the file's (mtime, size) signature changes.
# Some filesystems (FUSE / NFS / SMB) only expose 1-second mtime resolution,
# so two edits within the same second would slip past an mtime-only check.
# Pairing mtime with file size catches the vast majority of those cases —
# a CSS edit that changes content but keeps both mtime AND size identical
# would still be missed, but that's vanishingly rare for real edits.
_state = {'hash': None, 'mtime': 0, 'size': -1}


def _stat_signature():
    """Return ``(mtime, size)`` for the styles file, or ``(0, -1)`` on error."""
    try:
        st = os.stat(STYLES_PATH)
        return st.st_mtime, st.st_size
    except OSError as e:
        logger.warning('[CSSBundle] Cannot stat styles.css: %s', e)
        return 0, -1


def _compute_hash():
    """Read styles.css and return its content-hash. Returns None on read failure."""
    try:
        with open(STYLES_PATH, 'rb') as f:
            data = f.read()
    except OSError as e:
        logger.warning('[CSSBundle] Cannot read styles.css: %s', e)
        return None
    return hashlib.sha256(data).hexdigest()[:8]


def get_styles_hash():
    """Return the current content-hash for styles.css, refreshing if the
    file changed since the last call.

    Falls back to a time-bucketed pseudo-hash on read failure so we still
    serve a usable `<link>` tag and don't crash the index page.
    """
    mtime, size = _stat_signature()
    if (_state['hash'] is None
            or mtime != _state['mtime']
            or size != _state['size']):
        h = _compute_hash()
        if h is None:
            return f'fallback-{int(time.time()) // 60}'
        if h != _state['hash']:
            logger.info('[CSSBundle] styles.css hash → %s', h)
        _state['hash'] = h
        _state['mtime'] = mtime
        _state['size'] = size
    return _state['hash']


def get_styles_link_tag():
    """Return the full `<link>` tag with content-hashed query string."""
    return (f'<link rel="stylesheet" href="static/styles.css?v={get_styles_hash()}">')
