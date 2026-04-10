"""JS bundler — concatenate app scripts into a single bundle at startup.

Eliminates the HTTP/1.1 waterfall problem where browsers limit to 6 concurrent
connections per host, causing 18 JS files to download in 3-4 serial waves.
With the bundle, the browser fetches 1 file (gzip ~250KB) in a single request.

The bundle is rebuilt at startup and whenever any source file changes.
No npm/webpack/build step required — pure Python concatenation.
"""
import hashlib
import os
import time

from lib.log import get_logger

logger = get_logger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_DIR = os.path.join(BASE_DIR, 'static', 'js')

# ── Load order MUST match index.html (dependencies flow top → bottom) ──
_BUNDLE_FILES = [
    'i18n.js',         # MUST be first — t() is used by all other modules
    'idb-cache.js',
    'core.js',
    'export-images.js',
    'branch.js',
    'ui.js',
    # Feature modules (order-independent, but keep stable for cache)
    'log-clean.js',
    'translation.js',
    'upload.js',
    'image-gen.js',
    'paper-reader.js',
    'project.js',
    'memory.js',
    'scheduler.js',
    'timer.js',
    'myday.js',
    'settings.js',
    # Orchestrator (MUST be last)
    'main.js',
]

# Global state
_bundle_filename = None   # e.g. 'bundle-a3f8b2c1.js'
_bundle_mtime = 0         # max mtime of source files when bundle was built


def _source_max_mtime():
    """Get the newest mtime among all source JS files."""
    max_mt = 0
    for name in _BUNDLE_FILES:
        path = os.path.join(JS_DIR, name)
        try:
            mt = os.path.getmtime(path)
            if mt > max_mt:
                max_mt = mt
        except OSError as e:
            logger.debug('[Bundle] Cannot stat %s: %s', name, e)
    return max_mt


def _clean_old_bundles(keep_filename):
    """Remove old bundle-*.js files."""
    try:
        for f in os.listdir(JS_DIR):
            if f.startswith('bundle-') and f.endswith('.js') and f != keep_filename:
                try:
                    os.remove(os.path.join(JS_DIR, f))
                except OSError as e:
                    logger.debug('[Bundle] Failed to remove old bundle %s: %s', f, e)
    except OSError as e:
        logger.debug('Failed to clean old bundles: %s', e)


def build_bundle():
    """Concatenate all app JS files into a single bundle with content hash.

    Returns:
        The bundle filename (e.g. 'bundle-a3f8b2c1.js') or None on failure.
    """
    global _bundle_filename, _bundle_mtime

    t0 = time.time()
    parts = []
    total_size = 0
    missing = []

    for name in _BUNDLE_FILES:
        path = os.path.join(JS_DIR, name)
        try:
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
            # Wrap each file in a comment header + newline separator
            # This helps with debugging stack traces
            parts.append(f'// ═══ {name} ═══\n')
            parts.append(content)
            parts.append('\n')
            total_size += len(content)
        except OSError as e:
            logger.warning('[Bundle] Missing source file %s: %s', name, e)
            missing.append(name)

    if missing:
        logger.error('[Bundle] Cannot build bundle — %d file(s) missing: %s',
                      len(missing), ', '.join(missing))
        return None

    bundle_content = ''.join(parts)

    # Content hash for cache busting (first 8 chars of SHA-256)
    content_hash = hashlib.sha256(bundle_content.encode('utf-8')).hexdigest()[:8]
    filename = f'bundle-{content_hash}.js'
    bundle_path = os.path.join(JS_DIR, filename)

    # Skip write if unchanged
    if filename == _bundle_filename and os.path.exists(bundle_path):
        logger.debug('[Bundle] Already up to date: %s', filename)
        return filename

    # Write the bundle
    try:
        with open(bundle_path, 'w', encoding='utf-8') as f:
            f.write(bundle_content)
    except OSError as e:
        logger.error('[Bundle] Failed to write %s: %s', bundle_path, e)
        return None

    _clean_old_bundles(filename)
    _bundle_filename = filename
    _bundle_mtime = _source_max_mtime()

    elapsed = time.time() - t0
    logger.info('[Bundle] Built %s (%d files, %dKB raw) in %.1fms',
                filename, len(_BUNDLE_FILES), total_size // 1024, elapsed * 1000)
    return filename


def get_bundle_filename():
    """Get the current bundle filename, rebuilding if source files changed.

    Returns:
        Bundle filename string, or None if bundling failed.
    """
    global _bundle_filename, _bundle_mtime

    # Check if any source file is newer than the bundle
    current_mtime = _source_max_mtime()
    if _bundle_filename and current_mtime <= _bundle_mtime:
        # Bundle path might have been deleted (e.g., manual cleanup)
        if os.path.exists(os.path.join(JS_DIR, _bundle_filename)):
            return _bundle_filename

    # Rebuild
    return build_bundle()


def get_bundle_script_tag():
    """Get the HTML script tag for the bundle.

    Returns:
        HTML string like '<script defer src="static/js/bundle-a3f8b2c1.js" ...></script>'
        or None if bundle is not available.
    """
    filename = get_bundle_filename()
    if not filename:
        return None
    return (f'<script defer src="static/js/{filename}"'
            f' onload="_onScriptLoad()" onerror="_onScriptError(event)"></script>')
