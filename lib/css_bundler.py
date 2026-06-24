"""CSS bundler — minify + content-hash the app stylesheet so browser caches
invalidate automatically when the source changes.

There's only ONE app stylesheet (`static/styles.css`). This module:
  1. Minifies it (string/comment-aware — see `_minify_css`) into a sibling
     file `static/styles-<hash>.css`, where `<hash>` is the first 8 chars of
     the SHA-256 of the SOURCE contents.
  2. Exposes `get_styles_link_tag()` which returns
     `<link rel="stylesheet" href="static/styles-<hash>.css">`.
  3. `routes/common.py` swaps the original `<link ... href="static/styles.css">`
     tag in the served HTML for this one via `_APP_STYLES_RE`.

The minified file is written next to the source under `/static/`, so the
existing static handler serves it directly (with the long cache lifetime in
server.py's add_cache_headers) — the content hash in the filename makes that
cache safe. The original `static/styles.css` stays the single source of
truth and is never rewritten.

Minification is deliberately conservative: it only strips `/* */` comments
and collapses runs of whitespace, while protecting the insides of `"..."` /
`'...'` strings (the file embeds 11 `url("data:image/svg+xml,...")` SVG
data-URIs whose `//`, `>`, and brace-like content must survive untouched).
Spaces are trimmed only around `{ } ;` — never around `:` or `,` — so
selectors, `calc()` operators, and pseudo-classes are left intact.
"""
import hashlib
import os
import time

from lib.log import get_logger

logger = get_logger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, 'static')
STYLES_PATH = os.path.join(STATIC_DIR, 'styles.css')

# Cached state — recomputed when the file's (mtime, size) signature changes.
# Some filesystems (FUSE / NFS / SMB) only expose 1-second mtime resolution,
# so two edits within the same second would slip past an mtime-only check.
# Pairing mtime with file size catches the vast majority of those cases —
# a CSS edit that changes content but keeps both mtime AND size identical
# would still be missed, but that's vanishingly rare for real edits.
_state = {'hash': None, 'filename': None, 'mtime': 0, 'size': -1}


def _minify_css(src: str) -> str:
    """Conservatively minify CSS.

    A single forward char-scan that tracks string state so embedded
    data-URIs and `content:` strings are never altered. Outside strings it
    drops `/* */` comments and collapses whitespace runs to one space; then a
    cheap pass trims the single spaces that are adjacent to ``{ } ;`` (safe —
    these never carry semantic whitespace). Spaces around ``:`` and ``,`` are
    left alone so ``a :hover``, ``calc(100% - 1px)`` and selector lists stay
    valid.

    Returns the source unchanged on any unexpected condition (never raises).
    """
    out = []
    i = 0
    n = len(src)
    quote = ''          # current string delimiter, '' when not in a string
    pending_ws = False  # a whitespace run is buffered (emit as single space)
    while i < n:
        c = src[i]
        if quote:
            out.append(c)
            if c == '\\' and i + 1 < n:   # escaped char inside string
                out.append(src[i + 1])
                i += 2
                continue
            if c == quote:
                quote = ''
            i += 1
            continue
        # Not in a string.
        if c in '"\'':
            if pending_ws:
                out.append(' ')
                pending_ws = False
            quote = c
            out.append(c)
            i += 1
            continue
        if c == '/' and i + 1 < n and src[i + 1] == '*':
            # Skip comment (treat as whitespace so tokens don't fuse).
            end = src.find('*/', i + 2)
            i = (end + 2) if end != -1 else n
            pending_ws = True
            continue
        if c in ' \t\r\n\f':
            pending_ws = True
            i += 1
            continue
        # A non-whitespace, non-string char: flush buffered whitespace first.
        if pending_ws:
            out.append(' ')
            pending_ws = False
        out.append(c)
        i += 1

    minified = ''.join(out)

    # Trim the single spaces hugging structural punctuation. Done with plain
    # str.replace loops (no regex) so there's zero chance of touching string
    # contents — by this point all whitespace runs are already single spaces.
    for token in ('{', '}', ';'):
        minified = minified.replace(' ' + token, token).replace(token + ' ', token)
    return minified.strip()


def _stat_signature():
    """Return ``(mtime, size)`` for the styles file, or ``(0, -1)`` on error."""
    try:
        st = os.stat(STYLES_PATH)
        return st.st_mtime, st.st_size
    except OSError as e:
        logger.warning('[CSSBundle] Cannot stat styles.css: %s', e)
        return 0, -1


def _clean_old_minified(keep_filename):
    """Remove stale ``styles-*.css`` files (keep only the current hash)."""
    try:
        for f in os.listdir(STATIC_DIR):
            if (f.startswith('styles-') and f.endswith('.css')
                    and f != keep_filename):
                try:
                    os.remove(os.path.join(STATIC_DIR, f))
                except OSError as e:
                    logger.debug('[CSSBundle] Failed to remove old %s: %s', f, e)
    except OSError as e:
        logger.debug('[CSSBundle] Failed to clean old minified files: %s', e)


def _build_minified():
    """Read styles.css, minify it, write ``styles-<hash>.css``.

    Returns ``(hash, filename)`` or ``(None, None)`` on read/write failure.
    """
    try:
        with open(STYLES_PATH, 'r', encoding='utf-8') as f:
            data = f.read()
    except OSError as e:
        logger.warning('[CSSBundle] Cannot read styles.css: %s', e)
        return None, None

    content_hash = hashlib.sha256(data.encode('utf-8')).hexdigest()[:8]
    filename = f'styles-{content_hash}.css'
    out_path = os.path.join(STATIC_DIR, filename)

    if not os.path.exists(out_path):
        try:
            minified = _minify_css(data)
        except Exception as e:
            # Never let a minifier edge case break the page — fall back to
            # shipping the original (still cache-busted via the hashed name).
            logger.error('[CSSBundle] Minify failed, writing raw: %s', e,
                         exc_info=True)
            minified = data
        try:
            with open(out_path, 'w', encoding='utf-8') as f:
                f.write(minified)
        except OSError as e:
            logger.error('[CSSBundle] Failed to write %s: %s', out_path, e)
            return None, None
        saved = len(data) - len(minified)
        logger.info('[CSSBundle] Built %s (%dKB → %dKB, saved %dKB)', filename,
                    len(data) // 1024, len(minified) // 1024, saved // 1024)
        _clean_old_minified(filename)
    return content_hash, filename


def get_styles_filename():
    """Return the current minified stylesheet filename (``styles-<hash>.css``),
    rebuilding when the source changed. Returns ``None`` on failure so the
    caller can fall back to the un-minified ``static/styles.css``.
    """
    mtime, size = _stat_signature()
    if (_state['filename'] is None
            or mtime != _state['mtime']
            or size != _state['size']
            or not os.path.exists(os.path.join(STATIC_DIR, _state['filename']))):
        h, fn = _build_minified()
        if fn is None:
            return None
        _state['hash'] = h
        _state['filename'] = fn
        _state['mtime'] = mtime
        _state['size'] = size
    return _state['filename']


def get_styles_hash():
    """Return the current content-hash for styles.css (back-compat helper).

    Falls back to a time-bucketed pseudo-hash on failure so we still serve a
    usable `<link>` tag and don't crash the index page.
    """
    if get_styles_filename() and _state['hash']:
        return _state['hash']
    return f'fallback-{int(time.time()) // 60}'


def get_styles_link_tag():
    """Return the full `<link>` tag pointing at the hashed minified stylesheet.

    Falls back to the un-minified source (with a `?v=` cache-buster) when
    minification/writing failed, so the page is never left without styles.
    """
    fn = get_styles_filename()
    if fn:
        return f'<link rel="stylesheet" href="static/{fn}">'
    return f'<link rel="stylesheet" href="static/styles.css?v={get_styles_hash()}">'
