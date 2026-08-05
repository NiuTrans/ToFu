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
    was DEFERRED 2026-08-01 (Epic-E pt_3879f00e sub-5B): it renders only for
    convs with swarm activity, and its seven call sites (streaming_ui.js ×5,
    chat_render.js, tool_rounds.js) are typeof-guarded with a generic-line
    fallback. The registration invariant is now: in _DEFERRED_FILES, NOT in
    _BUNDLE_FILES (double-load would double its two tickers), index.html
    dev-fallback tag kept. See tests/test_frontend_swarm_panel_deferred.py
    for the guard pins."""
    from lib.js_bundler import _BUNDLE_FILES, _DEFERRED_FILES

    sib = 'ui/streaming_swarm_panel.js'
    assert sib in _DEFERRED_FILES, (
        f'{sib} must be in _DEFERRED_FILES (deferred, Epic-E sub-5B).'
    )
    assert sib not in _BUNDLE_FILES, (
        f'{sib} must NOT remain in _BUNDLE_FILES — double-load would '
        'duplicate its tickers/reconciler.'
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


def test_image_fullscreen_helper_in_core_not_deferred():
    """``_openImageFullscreen`` / ``_downloadGenImage`` are called via inline
    onclick from CORE files (ui/tool_rounds.js image thumbnails,
    ui/chat_render.js image-gen cards) that render BEFORE Image-Gen mode is
    ever opened. They therefore MUST live in the CORE bundle
    (ui/image_fullscreen.js), NOT in the DEFERRED image-gen.js — otherwise the
    thumbnail "enlarge" onclick points at an undefined function until/unless
    the feature bundle loads (regression 2026-07-06)."""
    from lib.js_bundler import _BUNDLE_FILES, _DEFERRED_FILES

    assert 'ui/image_fullscreen.js' in _BUNDLE_FILES, (
        'ui/image_fullscreen.js must be in the CORE bundle so the shared '
        'image-viewer helpers are always defined.'
    )
    # Must load BEFORE the two callers (chat_render.js + tool_rounds.js).
    idx = _BUNDLE_FILES.index('ui/image_fullscreen.js')
    for caller in ('ui/chat_render.js', 'ui/tool_rounds.js'):
        assert idx < _BUNDLE_FILES.index(caller), (
            f'ui/image_fullscreen.js must load before {caller} (defines its '
            f'onclick target).'
        )
    # image-gen.js is deferred and must NOT redefine the helpers.
    assert 'image-gen.js' in _DEFERRED_FILES
    with open(os.path.join(PROJECT_ROOT, 'static', 'js', 'image-gen.js'),
              encoding='utf-8') as f:
        igsrc = f.read()
    assert 'function _openImageFullscreen' not in igsrc, (
        'image-gen.js (DEFERRED) must NOT define _openImageFullscreen — it '
        'lives in the CORE ui/image_fullscreen.js.'
    )
    # Dev-fallback <script> tag present.
    with open(os.path.join(PROJECT_ROOT, 'index.html'), encoding='utf-8') as f:
        html = f.read()
    assert re.search(
        r'<script[^>]+src="static/js/ui/image_fullscreen\.js[^"]*"', html
    ), 'index.html must include a <script> tag for static/js/ui/image_fullscreen.js.'


def test_bundle_audit_parity():
    """Every static/js/<name>.js referenced in index.html must appear in
    _BUNDLE_FILES OR _DEFERRED_FILES — guards against the trap CLAUDE.md
    §3.2.1 documents. The deferred feature bundle (paper-reader / orchestration
    / task-mode) keeps its <script> tags in index.html for the dev fallback,
    but those files live in _DEFERRED_FILES, not _BUNDLE_FILES."""
    from lib.js_bundler import _BUNDLE_FILES, _DEFERRED_FILES

    with open(os.path.join(PROJECT_ROOT, 'index.html'), encoding='utf-8') as f:
        html = f.read()
    referenced = set(re.findall(r'static/js/([a-z0-9_.-]+\.js)', html))
    # Strip vendor + built bundle artifact filenames (bundle-<hash>/feature-<hash>).
    referenced = {
        n for n in referenced
        if not re.match(r'^(?:bundle|feature)-[0-9a-f]{8}\.js$', n)
        and n != 'compaction-viewer.js'
        # compaction-viewer is intentionally excluded — see lib/js_bundler.py
    }
    known = set(_BUNDLE_FILES) | set(_DEFERRED_FILES)
    missing = referenced - known
    # ``compaction-viewer.js`` was historically excluded; allow it.
    missing.discard('compaction-viewer.js')
    assert not missing, (
        f'Files referenced in index.html but missing from _BUNDLE_FILES / '
        f'_DEFERRED_FILES: {sorted(missing)}'
    )


def test_deferred_files_registered():
    """The lazily-loaded feature modules (paper-reader / orchestration /
    task-mode) must be in _DEFERRED_FILES (built into feature-<hash>.js) AND
    have a <script> tag in index.html for the dev fallback. feature-loader.js
    (the on-demand loader) must be in _BUNDLE_FILES (CORE) so the lazy stubs
    are installed before boot. Guards the code-split against the silent-no-op
    trap in both directions."""
    from lib.js_bundler import _BUNDLE_FILES, _DEFERRED_FILES, _DEFERRED_ENTRY_POINTS

    # The loader belongs to the CORE bundle, not the deferred one.
    assert 'feature-loader.js' in _BUNDLE_FILES
    assert 'feature-loader.js' not in _DEFERRED_FILES
    # The deferred modules must NOT also be in the core bundle (would double-load).
    for f in ('paper-reader.js', 'orchestration.js', 'task-mode.js'):
        assert f in _DEFERRED_FILES, f'{f} must be in _DEFERRED_FILES'
        assert f not in _BUNDLE_FILES, f'{f} must NOT also be in _BUNDLE_FILES (double-load)'
    # Ordering within the deferred bundle: task-mode.js reads orchestration.js's
    # _ORCH_* at runtime → orchestration.js must load first.
    assert _DEFERRED_FILES.index('orchestration.js') < _DEFERRED_FILES.index('task-mode.js')

    # Dev-fallback <script> tags present for every deferred file + the loader.
    with open(os.path.join(PROJECT_ROOT, 'index.html'), encoding='utf-8') as f:
        html = f.read()
    for name in (*_DEFERRED_FILES, 'feature-loader.js'):
        assert re.search(r'<script[^>]+src="static/js/' + re.escape(name) + r'[^"]*"', html), (
            f'index.html must include a <script> tag for static/js/{name} (dev fallback).'
        )

    # Entry-point set is non-empty and all are real non-empty names (sanity).
    # (Real parity — each entry point is actually DEFINED in a _DEFERRED_FILES
    # source — lives in tests/test_bundle_manifest_parity.py, which no longer
    # hard-codes a count so it survives future deferrals like image-gen.js.)
    assert _DEFERRED_ENTRY_POINTS and all(
        isinstance(n, str) and n for n in _DEFERRED_ENTRY_POINTS
    )
