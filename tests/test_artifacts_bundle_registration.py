"""Regression test for the JS-bundler silent-no-op trap (CLAUDE.md §3.2.1).

Verifies that ``static/js/artifacts.js`` is included in
``lib.js_bundler._BUNDLE_FILES`` AND has a corresponding ``<script>`` tag
in ``index.html`` for the dev fallback path.

If a future commit forgets one half, this test fails loudly instead of
the file silently loading as a no-op in production.
"""
from __future__ import annotations

import os
import re

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_artifacts_in_bundle():
    from lib.js_bundler import _BUNDLE_FILES
    assert 'artifacts.js' in _BUNDLE_FILES, (
        'artifacts.js must be registered in lib/js_bundler.py:_BUNDLE_FILES '
        'so it ends up in the production bundle.'
    )
    # Ordering: must come BEFORE the ui/ subpackage (which calls Artifacts.*
    # at runtime but ui/sse_pipeline.js looks up attachToMessage at SSE
    # handling time). The legacy monolithic `ui.js` was split into the
    # `ui/` subpackage on 2026-05-28; we now check the FIRST entry of the
    # subpackage.
    first_ui = next(f for f in _BUNDLE_FILES if f.startswith('ui/'))
    assert _BUNDLE_FILES.index('artifacts.js') < _BUNDLE_FILES.index(first_ui), (
        f'artifacts.js must precede the ui/ subpackage (first: {first_ui}).'
    )
    # And after core.js (uses renderMarkdown, escapeHtml, apiUrl).
    assert _BUNDLE_FILES.index('core.js') < _BUNDLE_FILES.index('artifacts.js'), (
        'artifacts.js depends on core.js (renderMarkdown, escapeHtml).'
    )


def test_artifacts_script_tag_in_index_html():
    """Dev-mode fallback: when bundling fails, individual <script> tags
    are served.  The artifacts tag must be present so the dev path works."""
    with open(os.path.join(PROJECT_ROOT, 'index.html'), encoding='utf-8') as f:
        html = f.read()
    assert re.search(
        r'<script[^>]+src="static/js/artifacts\.js[^"]*"', html
    ), 'index.html must include a <script> tag for static/js/artifacts.js'


def test_bundle_audit_parity():
    """Every static/js/<name>.js referenced in index.html must appear in
    _BUNDLE_FILES — guards against the trap CLAUDE.md §3.2.1 documents."""
    from lib.js_bundler import _BUNDLE_FILES

    with open(os.path.join(PROJECT_ROOT, 'index.html'), encoding='utf-8') as f:
        html = f.read()
    referenced = set(re.findall(r'static/js/([a-z0-9_.-]+\.js)', html))
    # Strip vendor + bundle artifact filenames.
    referenced = {
        n for n in referenced
        if not n.startswith('bundle-') and n != 'compaction-viewer.js'
        # compaction-viewer is intentionally excluded — see lib/js_bundler.py
    }
    bundled = set(_BUNDLE_FILES)
    missing = referenced - bundled
    # ``compaction-viewer.js`` was historically excluded; allow it.
    missing.discard('compaction-viewer.js')
    assert not missing, (
        f'Files referenced in index.html but missing from _BUNDLE_FILES: '
        f'{sorted(missing)}'
    )
