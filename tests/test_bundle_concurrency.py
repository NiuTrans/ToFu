"""Regression test for the JS-bundler CONCURRENT-BUILD race (2026-07-10 incident).

Production symptom (root-caused from logs): ~96 pytest-xdist workers all imported
``server.py`` at once, and ``server.py`` called ``build_bundle()`` at MODULE
IMPORT time. The bundler wrote the content-hash-named ``bundle-<hash>.js``
NON-atomically (``open(path,'w')`` → separate ``node --check`` → ``os.remove`` on
failure) with NO lock, so the concurrent builders clobbered the SAME path. Two
failure signatures appeared in the same window:

  * ``FAILED syntax check`` — ``node --check`` read a file another worker was
    still writing (truncated mid-string → SyntaxError).
  * ``MODULE_NOT_FOUND`` — ``node --check`` ran after a DIFFERENT worker's
    failure branch had already ``os.remove()``'d the file.

Both refused to serve the bundle → the frontend "wouldn't start".

The fix (two independent defenses, both asserted here):

  1. ``_assemble_bundle`` publishes ATOMICALLY: write a unique temp file in the
     same dir → gate the temp → ``os.rename`` into the hash path only on
     success, and short-circuit when the hash path already exists. A reader
     never sees a partial/absent hash path; a failed gate deletes only the
     private temp.
  2. ``build_bundle`` runs under a cross-process ``flock`` so simultaneous
     builders serialize, then adopt the just-published artifact.

  3. Importing ``server`` no longer builds the bundle (the build moved into the
     server startup path), so a mere ``import server`` has no side effect on the
     live ``static/js/`` tree.

These tests bite: revert the temp+rename to a direct in-place write, or remove
the flock, and the concurrency assertions fail; restore the import-time
``build_bundle()`` call and the no-side-effect test fails.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading

import pytest

pytestmark = pytest.mark.unit

# ── Shared temp-tree helpers (mirror test_bundle_corruption_guard.py) ──────


def _make_js_tree(tmp_path, files: dict):
    js_dir = tmp_path / 'js'
    js_dir.mkdir()
    for name, content in files.items():
        p = js_dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding='utf-8')
    return str(js_dir)


def _point_bundler_at(monkeypatch, js_dir, files):
    """Repoint the bundler module globals at a throwaway JS dir."""
    from lib import js_bundler
    monkeypatch.setattr(js_bundler, 'JS_DIR', js_dir)
    monkeypatch.setattr(js_bundler, '_BUNDLE_FILES', list(files.keys()))
    monkeypatch.setattr(js_bundler, '_DEFERRED_FILES', [])
    monkeypatch.setattr(js_bundler, '_bundle_filename', None)
    monkeypatch.setattr(js_bundler, '_feature_filename', None)
    monkeypatch.setattr(js_bundler, '_bundle_mtime', 0)
    monkeypatch.setattr(js_bundler, '_CRITICAL_FILES', frozenset())
    # Keep output deterministic regardless of whether esbuild is installed.
    monkeypatch.setattr(js_bundler, '_resolve_esbuild', lambda: None)
    # The build lock must live in the throwaway dir, not the real static/js.
    monkeypatch.setattr(js_bundler, '_BUILD_LOCK_PATH',
                        os.path.join(js_dir, '.bundle-build.lock'))


# ── 1. Atomic publish: a failed node gate never deletes/creates a hash path
#      another builder is using; only a private temp is dropped. ────────────

def test_failed_gate_only_removes_private_temp(tmp_path, monkeypatch):
    from lib import js_bundler

    files = {'a.js': 'window.A = 1;\n'}
    js_dir = _make_js_tree(tmp_path, files)
    _point_bundler_at(monkeypatch, js_dir, files)

    # Force the syntax gate to fail for THIS build.
    monkeypatch.setattr(js_bundler, '_node_syntax_ok', lambda path: (False, 'forced'))

    name, size = js_bundler._assemble_bundle(list(files.keys()), 'bundle-', critical=False)
    assert name is None, 'a failed gate must not publish a bundle'

    # No hash-named bundle and no leftover temp should remain. The private
    # temp is named ".bundle-<hash>.<rand>.js" (leading dot keeps it out of the
    # served set); it must be cleaned up on failure.
    leftovers = os.listdir(js_dir)
    assert not any(f.startswith('bundle-') for f in leftovers), (
        'failed build must not leave a bundle-<hash>.js: %s' % leftovers
    )
    assert not any(f.startswith('.bundle-') and f.endswith('.js') for f in leftovers), (
        'failed build must clean up its private temp file: %s' % leftovers
    )


def test_second_build_short_circuits_on_existing_hash(tmp_path, monkeypatch):
    """A second build of identical content adopts the on-disk file without
    re-writing it — the node gate is not even invoked the second time."""
    from lib import js_bundler

    files = {'a.js': 'window.A = 1;\n'}
    js_dir = _make_js_tree(tmp_path, files)
    _point_bundler_at(monkeypatch, js_dir, files)

    name1, _ = js_bundler._assemble_bundle(list(files.keys()), 'bundle-', critical=False)
    assert name1 and name1.startswith('bundle-')

    calls = {'n': 0}
    real_gate = js_bundler._node_syntax_ok

    def _counting_gate(path):
        calls['n'] += 1
        return real_gate(path)

    monkeypatch.setattr(js_bundler, '_node_syntax_ok', _counting_gate)
    name2, _ = js_bundler._assemble_bundle(list(files.keys()), 'bundle-', critical=False)
    assert name2 == name1, 'identical content must resolve to the same hash file'
    assert calls['n'] == 0, (
        'the second build must short-circuit on the existing hash path, not '
        're-gate/re-write it'
    )


# ── 2. Concurrency: N threads building the SAME tree at once must produce one
#      valid bundle and log NO "FAILED syntax check". ────────────────────────

def test_concurrent_builds_produce_one_valid_bundle(tmp_path, monkeypatch, caplog):
    from lib import js_bundler

    # A realistic-ish payload so writing takes non-zero time (widens the race
    # window the old in-place write lost).
    big = 'window.X = "' + ('a' * 40000) + '";\n'
    files = {'a.js': 'window.A = 1;\n', 'big.js': big, 'c.js': 'window.C = 3;\n'}
    js_dir = _make_js_tree(tmp_path, files)
    _point_bundler_at(monkeypatch, js_dir, files)

    results: list = []
    errors: list = []

    def _worker():
        try:
            results.append(js_bundler.build_bundle())
        except Exception as e:  # noqa: BLE001 - surface any thread crash
            errors.append(e)

    caplog.set_level(logging.CRITICAL, logger='lib.js_bundler')

    threads = [threading.Thread(target=_worker) for _ in range(24)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, 'no builder thread may raise: %s' % errors
    assert all(r and r.startswith('bundle-') for r in results), (
        'every concurrent builder must return a valid bundle name: %s' % results
    )
    assert len(set(results)) == 1, (
        'all builders must converge on ONE content-hash: %s' % set(results)
    )

    # The incident signature: a refused bundle.
    assert not any('FAILED syntax check' in r.getMessage() for r in caplog.records), (
        'no build may log "FAILED syntax check" under concurrency'
    )

    # Exactly one bundle survives; no temp files linger.
    survivors = [f for f in os.listdir(js_dir) if f.startswith('bundle-')]
    assert len(survivors) == 1, 'exactly one bundle must survive: %s' % survivors
    assert not any(f.startswith('.bundle-') and f.endswith('.js') for f in os.listdir(js_dir)), (
        'no temp files may linger after concurrent builds'
    )

    # The surviving bundle is complete (contains all sources, no truncation).
    text = (tmp_path / 'js' / survivors[0]).read_text(encoding='utf-8')
    assert 'window.A = 1;' in text and 'window.C = 3;' in text
    assert text.count('a' * 40000) == 1, 'big payload must be present and intact'


# ── 3. No import-time side effect: importing `server` must NOT build a bundle
#      into the live static/js/ tree. ─────────────────────────────────────────

def test_importing_server_does_not_build_bundle():
    """`import server` must be side-effect-free w.r.t. the JS bundle.

    Run in a SUBPROCESS (importing server in-process would boot half the app).
    The child monkeypatches ``lib.js_bundler.build_bundle`` to a tripwire BEFORE
    importing server; if import calls it, the child exits non-zero.
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    child = (
        "import sys\n"
        "import lib.js_bundler as jb\n"
        "_calls = []\n"
        "jb.build_bundle = lambda *a, **k: _calls.append(1)\n"
        "import server  # noqa: F401 — importing must not build the bundle\n"
        "sys.exit(3 if _calls else 0)\n"
    )
    env = dict(os.environ)
    # Keep the import cheap/quiet and off the real DB/network.
    env.setdefault('TOFU_DB_BACKEND', 'sqlite')
    env.setdefault('PYTEST_DISABLE_PLUGIN_AUTOLOAD', '1')
    proc = subprocess.run(
        [sys.executable, '-c', child],
        cwd=repo_root, env=env, capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode != 3, (
        'importing server must NOT call build_bundle() (import-time side effect '
        'reintroduced).\nstdout:\n%s\nstderr:\n%s' % (proc.stdout[-2000:], proc.stderr[-2000:])
    )
    # returncode 0 = clean; any other non-3 failure means the import itself
    # broke — surface it so we don't silently pass on a broken import.
    assert proc.returncode == 0, (
        'server import failed (rc=%s):\nstdout:\n%s\nstderr:\n%s'
        % (proc.returncode, proc.stdout[-2000:], proc.stderr[-2000:])
    )
