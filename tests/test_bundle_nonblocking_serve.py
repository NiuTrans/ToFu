"""Regression test: `GET /` must NEVER rebuild the JS bundle on the request thread.

Production incident (2026-07-19): after an overnight idle, a sibling conversation
had ``git mv``'d two source files, so the still-running process's in-memory
``_BUNDLE_FILES`` pointed at now-missing paths. Every first ``GET /`` after that
saw "source newer than bundle" and ran a FULL ``build_bundle()`` (node --check
subprocess + minify + content-hash) INLINE on the sync request thread. Layered
under a reconnect thundering-herd, this stalled the event loop and produced a
95-second ``GET / 200 SLOW``.

The fix: ``index_page`` calls the *non-blocking* accessors
(``get_bundle_filename_nonblocking`` / ``get_bundle_script_tag_nonblocking``),
which serve the last-good bundle immediately and schedule any rebuild in a
daemon thread. Only a genuine cold start with no serviceable bundle on disk
still blocks.

These tests bite: point ``index_page`` back at the blocking accessor, or make
the non-blocking accessor call ``build_bundle()`` inline when a serviceable
bundle exists, and the assertions below fail.
"""
from __future__ import annotations

import os
import threading
import time

import pytest

pytestmark = pytest.mark.unit


def _make_js_tree(tmp_path, files: dict):
    js_dir = tmp_path / 'js'
    js_dir.mkdir()
    for name, content in files.items():
        p = js_dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding='utf-8')
    return str(js_dir)


def _point_bundler_at(monkeypatch, js_dir, files):
    from lib import js_bundler
    monkeypatch.setattr(js_bundler, 'JS_DIR', js_dir)
    monkeypatch.setattr(js_bundler, '_BUNDLE_FILES', list(files.keys()))
    monkeypatch.setattr(js_bundler, '_DEFERRED_FILES', [])
    monkeypatch.setattr(js_bundler, '_bundle_filename', None)
    monkeypatch.setattr(js_bundler, '_feature_filename', None)
    monkeypatch.setattr(js_bundler, '_bundle_mtime', 0)
    monkeypatch.setattr(js_bundler, '_CRITICAL_FILES', frozenset())
    monkeypatch.setattr(js_bundler, '_resolve_esbuild', lambda: None)
    monkeypatch.setattr(js_bundler, '_BUILD_LOCK_PATH',
                        os.path.join(js_dir, '.bundle-build.lock'))
    monkeypatch.setattr(js_bundler, '_bg_rebuild_active', False)


def test_nonblocking_serves_lastgood_and_never_builds_inline_when_stale(tmp_path, monkeypatch):
    """When a source file changed but a serviceable bundle is on disk, the
    non-blocking accessor returns the OLD bundle WITHOUT calling build_bundle()
    on the caller — it schedules the rebuild in the background instead."""
    from lib import js_bundler

    files = {'a.js': 'window.A = 1;\n'}
    js_dir = _make_js_tree(tmp_path, files)
    _point_bundler_at(monkeypatch, js_dir, files)

    # Initial (startup-style) build so a last-good bundle exists on disk.
    first = js_bundler.build_bundle()
    assert first and first.startswith('bundle-')

    # Make a source file appear newer than the bundle (the "stale" condition
    # that made the OLD accessor rebuild inline).
    future = time.time() + 1000
    os.utime(os.path.join(js_dir, 'a.js'), (future, future))

    # Tripwire: build_bundle must NOT run inline on the CALLER's thread. The
    # legitimate rebuild is dispatched via _schedule_background_rebuild (stubbed
    # here to a flag), so any build_bundle call now would be a synchronous
    # request-path rebuild — the exact 95s-stall regression.
    caller_thread = threading.current_thread()
    inline_calls = {'n': 0}
    real_build = js_bundler.build_bundle

    def _tripwire_build():
        if threading.current_thread() is caller_thread:
            inline_calls['n'] += 1
        return real_build()

    scheduled = {'n': 0}
    monkeypatch.setattr(js_bundler, 'build_bundle', _tripwire_build)
    monkeypatch.setattr(js_bundler, '_schedule_background_rebuild',
                        lambda: scheduled.__setitem__('n', scheduled['n'] + 1))

    served = js_bundler.get_bundle_filename_nonblocking()
    # It handed back the last-good bundle immediately.
    assert served == first, 'must serve the last-good bundle, got %r' % served
    # No synchronous rebuild on the request thread.
    assert inline_calls['n'] == 0, (
        'non-blocking accessor must not call build_bundle() inline when a '
        'serviceable bundle exists (it stalled GET / for 95s in prod)'
    )
    # The rebuild was handed off to the background instead.
    assert scheduled['n'] == 1, (
        'a stale-but-serviceable bundle must schedule exactly one background '
        'rebuild, got %d' % scheduled['n']
    )


def test_nonblocking_fresh_bundle_schedules_nothing(tmp_path, monkeypatch):
    """When the bundle is up-to-date, the non-blocking accessor is a pure
    read — it neither rebuilds nor schedules a background rebuild."""
    from lib import js_bundler

    files = {'a.js': 'window.A = 1;\n'}
    js_dir = _make_js_tree(tmp_path, files)
    _point_bundler_at(monkeypatch, js_dir, files)

    first = js_bundler.build_bundle()
    assert first

    scheduled = {'n': 0}
    monkeypatch.setattr(js_bundler, '_schedule_background_rebuild',
                        lambda: scheduled.__setitem__('n', scheduled['n'] + 1))

    served = js_bundler.get_bundle_filename_nonblocking()
    assert served == first
    assert scheduled['n'] == 0, 'a fresh bundle must not schedule a rebuild'


def test_nonblocking_cold_start_still_builds(tmp_path, monkeypatch):
    """With NO serviceable bundle on disk (cold start / dev first boot), the
    non-blocking accessor may build inline — there is nothing to serve
    otherwise. This is the one allowed blocking path."""
    from lib import js_bundler

    files = {'a.js': 'window.A = 1;\n'}
    js_dir = _make_js_tree(tmp_path, files)
    _point_bundler_at(monkeypatch, js_dir, files)

    served = js_bundler.get_bundle_filename_nonblocking()
    assert served and served.startswith('bundle-'), (
        'cold start must still produce a bundle to serve, got %r' % served
    )


def test_index_page_uses_nonblocking_accessors():
    """routes.common.index_page must import the NON-blocking accessors, so a
    stale bundle can never rebuild on the GET / request thread."""
    import routes.common as common
    from lib import js_bundler

    # The module-level aliases index_page calls must be the non-blocking ones.
    assert common._get_bundle_tag is js_bundler.get_bundle_script_tag_nonblocking, (
        'index_page must serve via get_bundle_script_tag_nonblocking'
    )
    assert common._get_feature_bundle_filename is js_bundler.get_feature_bundle_filename_nonblocking, (
        'index_page must resolve the feature bundle via the non-blocking accessor'
    )
