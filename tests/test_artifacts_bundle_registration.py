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


def test_stale_manifest_entry_does_not_kill_bundle(monkeypatch):
    """A missing file in _BUNDLE_FILES must degrade gracefully.

    Regression for the 2026-06-03 production incident where a renamed JS
    file left a stale entry (``api-keys.js``) in _BUNDLE_FILES; the old
    build_bundle() returned None for ANY missing file, forcing the
    dev-fallback path and shipping a blank UI. The bundler must now skip
    the missing file (loud warning) and still produce a bundle from the
    rest. Only an entirely-empty manifest is fatal.
    """
    from lib import js_bundler

    monkeypatch.setattr(
        js_bundler, '_BUNDLE_FILES',
        ['i18n.js', '__definitely_missing_file__.js', 'core.js'],
    )
    # Reset module bundle state so build_bundle() actually rebuilds.
    monkeypatch.setattr(js_bundler, '_bundle_filename', None)
    monkeypatch.setattr(js_bundler, '_bundle_mtime', 0)

    name = js_bundler.build_bundle()
    assert name and name.startswith('bundle-'), (
        'bundle must still build when one manifest entry is missing'
    )


def test_empty_manifest_is_fatal(monkeypatch):
    """When NO source file resolves, build_bundle() returns None so the
    caller falls back to serving individual <script> tags."""
    from lib import js_bundler

    monkeypatch.setattr(js_bundler, '_BUNDLE_FILES', ['__missing_a__.js'])
    monkeypatch.setattr(js_bundler, '_bundle_filename', None)
    monkeypatch.setattr(js_bundler, '_bundle_mtime', 0)
    assert js_bundler.build_bundle() is None


def test_sse_pipeline_siblings_registered():
    """The two files split out of ui/sse_pipeline.js (2026-06) must be in
    _BUNDLE_FILES AND in index.html, loaded AFTER sse_pipeline.js.

    The generic ``test_bundle_audit_parity`` regex (``[a-z0-9_.-]+``) does
    NOT match ``ui/`` subdir paths, so subpackage files are invisible to it.
    This explicit check guards the split against the silent-no-op trap.
    """
    from lib.js_bundler import _BUNDLE_FILES

    for sib in ('ui/sse_poll_fallback.js', 'ui/send_button.js'):
        assert sib in _BUNDLE_FILES, (
            f'{sib} (split from ui/sse_pipeline.js) must be in _BUNDLE_FILES.'
        )
        assert _BUNDLE_FILES.index('ui/sse_pipeline.js') < _BUNDLE_FILES.index(sib), (
            f'{sib} must load AFTER ui/sse_pipeline.js.'
        )

    with open(os.path.join(PROJECT_ROOT, 'index.html'), encoding='utf-8') as f:
        html = f.read()
    for sib in ('ui/sse_poll_fallback.js', 'ui/send_button.js'):
        assert re.search(
            r'<script[^>]+src="static/js/' + re.escape(sib) + r'[^"]*"', html
        ), f'index.html must include a <script> tag for static/js/{sib} (dev fallback).'


def test_sse_handler_files_registered():
    """The property-only handlers extracted from dispatchSSEEvent (2026-06)
    must be in _BUNDLE_FILES AND index.html, loaded BEFORE sse_pipeline.js
    (the dispatcher calls _handleToolStart/_handleSwarmPhase/etc.)."""
    from lib.js_bundler import _BUNDLE_FILES

    _handlers = ('ui/sse_handlers_tool.js', 'ui/sse_handlers_swarm.js',
                 'ui/sse_handlers_io.js', 'ui/sse_handlers_misc.js',
                 'ui/sse_handlers_lifecycle.js')
    for h in _handlers:
        assert h in _BUNDLE_FILES, (
            f'{h} (extracted from dispatchSSEEvent) must be in _BUNDLE_FILES.'
        )
        assert _BUNDLE_FILES.index(h) < _BUNDLE_FILES.index('ui/sse_pipeline.js'), (
            f'{h} must load BEFORE ui/sse_pipeline.js (dispatcher calls it).'
        )

    with open(os.path.join(PROJECT_ROOT, 'index.html'), encoding='utf-8') as f:
        html = f.read()
    for h in _handlers:
        assert re.search(
            r'<script[^>]+src="static/js/' + re.escape(h) + r'[^"]*"', html
        ), f'index.html must include a <script> tag for static/js/{h}.'


def test_streaming_swarm_panel_registered():
    """The swarm-panel cluster split out of ui/streaming_ui.js (2026-06-27)
    must be in _BUNDLE_FILES AND index.html, loaded BEFORE streaming_ui.js
    (whose _syncToolRoundsDOM/updateStreamingUI call _buildSwarmPanelHTML /
    _buildSwarmInboxChipsHTML at runtime).

    The generic ``test_bundle_audit_parity`` regex does NOT match ``ui/``
    subdir paths, so this explicit check guards the split."""
    from lib.js_bundler import _BUNDLE_FILES

    sib = 'ui/streaming_swarm_panel.js'
    assert sib in _BUNDLE_FILES, (
        f'{sib} (split from ui/streaming_ui.js) must be in _BUNDLE_FILES.'
    )
    assert _BUNDLE_FILES.index(sib) < _BUNDLE_FILES.index('ui/streaming_ui.js'), (
        f'{sib} must load BEFORE ui/streaming_ui.js.'
    )

    with open(os.path.join(PROJECT_ROOT, 'index.html'), encoding='utf-8') as f:
        html = f.read()
    assert re.search(
        r'<script[^>]+src="static/js/' + re.escape(sib) + r'[^"]*"', html
    ), f'index.html must include a <script> tag for static/js/{sib} (dev fallback).'


def test_stream_lifecycle_registered():
    """The stream-lifecycle cluster split out of ui/streaming_ui.js
    (2026-06-27) must be in _BUNDLE_FILES AND index.html, loaded AFTER
    streaming_ui.js (finishStream/showStreamingUIForConv call updateStreamingUI
    at runtime).

    The generic ``test_bundle_audit_parity`` regex does NOT match ``ui/``
    subdir paths, so this explicit check guards the split."""
    from lib.js_bundler import _BUNDLE_FILES

    sib = 'ui/stream_lifecycle.js'
    assert sib in _BUNDLE_FILES, (
        f'{sib} (split from ui/streaming_ui.js) must be in _BUNDLE_FILES.'
    )
    assert _BUNDLE_FILES.index('ui/streaming_ui.js') < _BUNDLE_FILES.index(sib), (
        f'{sib} must load AFTER ui/streaming_ui.js.'
    )

    with open(os.path.join(PROJECT_ROOT, 'index.html'), encoding='utf-8') as f:
        html = f.read()
    assert re.search(
        r'<script[^>]+src="static/js/' + re.escape(sib) + r'[^"]*"', html
    ), f'index.html must include a <script> tag for static/js/{sib} (dev fallback).'


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
