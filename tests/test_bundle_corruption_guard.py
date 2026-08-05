"""Regression test for the JS-bundler "corrupt source poisons the bundle" trap.

The production symptom (reported repeatedly on fresh installs / self-updates):

    App initialization failed. ( Error: bundle-XXXXXXXX.js:NNNNN
      Uncaught SyntaxError: Unexpected token '-' )

Root cause: ``build_bundle()`` blindly concatenated every source file. If ONE
file on the target machine was corrupt — a git merge-conflict marker left by an
interrupted / conflicted ``git pull`` during self-update, or a truncated /
partial write (NUL bytes) — the stray token glued straight into the bundle and
white-screened the ENTIRE app with no recovery path.

The fix (lib/js_bundler.py):
  1. ``_scan_source_corruption`` rejects a corrupt file BEFORE it enters the
     bundle → one bad file degrades to "that module absent", not "app dead".
  2. A ``\\n;\\n`` boundary guard between files so an unterminated tail can't
     glue onto the next file's first token.
  3. ``_node_syntax_ok`` — a best-effort ``node --check`` gate on the
     concatenated bundle.
  4. ``_find_syntax_broken_sources`` (2026-08-04 sidebar incident) — when the
     whole-bundle gate fails on a SCANNER-CLEAN tree (a brace-unbalanced file
     left by an interrupted edit), this per-file bisect attributes the
     breakage and the bundle is re-assembled WITHOUT the broken non-critical
     source(s): degrade to "module absent" instead of refusing the bundle and
     pushing every user onto the dev-fallback, which serves the same broken
     file raw. Fatal only when a CRITICAL file is broken or the re-assembled
     bundle still fails.

These tests bite: neutering the scanner (making it always return None) makes
the corrupt-file cases fail.
"""
from __future__ import annotations

import os
import shutil

import pytest


# ── Pure-function scanner tests (no filesystem, dependency-free) ──────────

def test_scan_detects_conflict_markers():
    from lib.js_bundler import _scan_source_corruption
    src = 'var a = 1;\n<<<<<<< HEAD\nvar b = 2;\n=======\nvar b = 3;\n>>>>>>> other\n'
    reason = _scan_source_corruption('x.js', src)
    assert reason and 'conflict' in reason.lower()


def test_scan_detects_nul_byte():
    from lib.js_bundler import _scan_source_corruption
    reason = _scan_source_corruption('x.js', 'var a = 1;\x00')
    assert reason and 'NUL' in reason


def test_scan_passes_clean_file():
    from lib.js_bundler import _scan_source_corruption
    assert _scan_source_corruption('x.js', 'var a = 1;\n// trailing comment\n') is None


def test_scan_no_false_positive_on_midline_gtgt():
    """A legitimate '>>>>>>>' appearing MID-line (e.g. inside a string) must
    not be mistaken for a conflict marker — only a line-anchored marker is
    corruption."""
    from lib.js_bundler import _scan_source_corruption
    assert _scan_source_corruption('x.js', 'var s = "arrow >>>>>>> here";\n') is None


# ── End-to-end build tests against a temp JS dir ──────────────────────────

def _make_js_tree(tmp_path, files: dict):
    """Write {name: content} into a temp static/js dir; return the dir path."""
    js_dir = tmp_path / 'js'
    js_dir.mkdir()
    for name, content in files.items():
        p = js_dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding='utf-8')
    return str(js_dir)


def _reset_state(monkeypatch, js_dir, files):
    from lib import js_bundler
    monkeypatch.setattr(js_bundler, 'JS_DIR', js_dir)
    monkeypatch.setattr(js_bundler, '_BUNDLE_FILES', list(files.keys()))
    monkeypatch.setattr(js_bundler, '_bundle_filename', None)
    monkeypatch.setattr(js_bundler, '_bundle_mtime', 0)
    # These tests assert the DEPENDENCY-FREE _minify_js output shape (per-file
    # boundary headers, whitespace-preserving `window.A = 1;`). Force the
    # optional esbuild enhancer off so they stay deterministic whether or not
    # esbuild is installed — they exercise the corruption scanner / boundary
    # guard / critical tier, not which minifier runs.
    monkeypatch.setattr(js_bundler, '_resolve_esbuild', lambda: None)


def test_corrupt_file_skipped_bundle_still_builds(tmp_path, monkeypatch):
    """A conflict-marker file is skipped; the bundle builds from the rest and
    does NOT contain the stray marker."""
    from lib import js_bundler

    files = {
        'good_a.js': 'window.A = 1;\n',
        'bad.js': 'window.B = 2;\n<<<<<<< HEAD\nwindow.B = -;\n=======\nwindow.B = 3;\n>>>>>>> x\n',
        'good_c.js': 'window.C = 4;\n',
    }
    js_dir = _make_js_tree(tmp_path, files)
    _reset_state(monkeypatch, js_dir, files)

    name = js_bundler.build_bundle()
    assert name and name.startswith('bundle-'), 'bundle must still build with one corrupt file'

    bundle_text = (tmp_path / 'js' / name).read_text(encoding='utf-8')
    assert '<<<<<<<' not in bundle_text and '>>>>>>>' not in bundle_text, (
        'corrupt conflict markers must NOT leak into the bundle'
    )
    # The header comment for bad.js must also be absent (whole file skipped).
    assert 'window.B' not in bundle_text
    # The healthy files survive.
    assert 'window.A = 1;' in bundle_text
    assert 'window.C = 4;' in bundle_text


def test_neutered_scanner_leaks_corruption(tmp_path, monkeypatch):
    """NC bite: if the scanner is neutered (always None), the conflict marker
    leaks into the bundle — proving the scanner is what prevents the white
    screen."""
    from lib import js_bundler

    files = {
        'good_a.js': 'window.A = 1;\n',
        'bad.js': 'window.B = 2;\n<<<<<<< HEAD\n=======\n>>>>>>> x\n',
    }
    js_dir = _make_js_tree(tmp_path, files)
    _reset_state(monkeypatch, js_dir, files)
    monkeypatch.setattr(js_bundler, '_scan_source_corruption', lambda name, content: None)
    # Also neuter the node gate so we can observe the raw (broken) bundle;
    # otherwise the gate would delete it and return None.
    monkeypatch.setattr(js_bundler, '_node_syntax_ok', lambda path: (True, ''))

    name = js_bundler.build_bundle()
    bundle_text = (tmp_path / 'js' / name).read_text(encoding='utf-8')
    assert '<<<<<<<' in bundle_text, (
        'without the scanner, the conflict marker leaks — this is the bug the '
        'guard fixes'
    )


def test_boundary_guard_separates_files(tmp_path, monkeypatch):
    """A file ending in a line comment with no trailing newline must not
    swallow the next file — the boundary guard inserts a newline + ';'."""
    from lib import js_bundler

    files = {
        'a.js': 'window.A = 1; // note',   # no trailing newline
        'b.js': 'window.B = 2;\n',
    }
    js_dir = _make_js_tree(tmp_path, files)
    _reset_state(monkeypatch, js_dir, files)

    name = js_bundler.build_bundle()
    bundle_text = (tmp_path / 'js' / name).read_text(encoding='utf-8')
    # 'window.B' must appear on its OWN line, not commented out by a.js's
    # trailing '// note'.
    assert 'window.B = 2;' in bundle_text
    idx = bundle_text.index('window.B = 2;')
    line_start = bundle_text.rfind('\n', 0, idx) + 1
    line = bundle_text[line_start:idx]
    assert '//' not in line, 'window.B must not be glued into a.js line comment'


# ── Critical-file tier: a corrupt/missing load-bearing module is FATAL ────

def test_critical_files_subset_of_bundle_files():
    """INVARIANT: every _CRITICAL_FILES entry must exist in _BUNDLE_FILES, so a
    rename that drops a file from the manifest can't silently empty the set."""
    from lib.js_bundler import _BUNDLE_FILES, _CRITICAL_FILES
    assert _CRITICAL_FILES, '_CRITICAL_FILES must not be empty'
    missing = _CRITICAL_FILES - set(_BUNDLE_FILES)
    assert not missing, (
        'critical files not in _BUNDLE_FILES (rename drift): %s' % sorted(missing)
    )


def test_corrupt_critical_file_is_fatal(tmp_path, monkeypatch):
    """A CORRUPT critical file must make build_bundle() return None (fall back
    to individual <script> tags) rather than ship a crippled bundle."""
    from lib import js_bundler

    files = {
        'i18n.js': 'window.t = function(k){return k;};\n',
        'push.js': 'window.B = 2;\n<<<<<<< HEAD\n=======\n>>>>>>> x\n',  # critical + corrupt
        'main.js': 'window.M = 1;\n',
    }
    js_dir = _make_js_tree(tmp_path, files)
    _reset_state(monkeypatch, js_dir, files)
    monkeypatch.setattr(js_bundler, '_CRITICAL_FILES', frozenset({'push.js'}))

    assert js_bundler.build_bundle() is None, (
        'a corrupt CRITICAL file must be fatal (fall back to <script> tags)'
    )


def test_missing_critical_file_is_fatal(tmp_path, monkeypatch):
    """A MISSING critical file (stale manifest / interrupted pull) must make
    build_bundle() return None rather than ship a crippled bundle."""
    from lib import js_bundler

    files = {
        'i18n.js': 'window.t = function(k){return k;};\n',
        'main.js': 'window.M = 1;\n',
    }
    js_dir = _make_js_tree(tmp_path, files)
    # Manifest lists push.js but we never write it → missing on disk.
    monkeypatch.setattr(js_bundler, 'JS_DIR', js_dir)
    monkeypatch.setattr(js_bundler, '_BUNDLE_FILES', ['i18n.js', 'push.js', 'main.js'])
    monkeypatch.setattr(js_bundler, '_bundle_filename', None)
    monkeypatch.setattr(js_bundler, '_bundle_mtime', 0)
    monkeypatch.setattr(js_bundler, '_CRITICAL_FILES', frozenset({'push.js'}))

    assert js_bundler.build_bundle() is None, (
        'a missing CRITICAL file must be fatal (fall back to <script> tags)'
    )


def test_corrupt_noncritical_file_still_builds(tmp_path, monkeypatch):
    """Regression guard: a corrupt NON-critical file keeps the skip-and-continue
    degradation — the bundle still builds without it. Contrast with the two
    critical-file tests above (this is the NC boundary for the tier)."""
    from lib import js_bundler

    files = {
        'i18n.js': 'window.t = function(k){return k;};\n',
        'timer.js': 'window.B = 2;\n<<<<<<< HEAD\n=======\n>>>>>>> x\n',  # non-critical + corrupt
        'main.js': 'window.M = 1;\n',
    }
    js_dir = _make_js_tree(tmp_path, files)
    _reset_state(monkeypatch, js_dir, files)
    monkeypatch.setattr(js_bundler, '_CRITICAL_FILES', frozenset({'i18n.js', 'main.js'}))

    name = js_bundler.build_bundle()
    assert name and name.startswith('bundle-'), (
        'a corrupt NON-critical file must NOT abort the bundle'
    )
    bundle_text = (tmp_path / 'js' / name).read_text(encoding='utf-8')
    assert '<<<<<<<' not in bundle_text
    assert 'window.t' in bundle_text and 'window.M = 1;' in bundle_text


@pytest.mark.skipif(shutil.which('node') is None, reason='node not installed')
def test_syntax_broken_noncritical_source_is_excluded_not_fatal(tmp_path, monkeypatch):
    """2026-08-04 sidebar incident contract: a source file that fails
    ``node --check`` WITHOUT any scanner-visible marker (an interrupted agent
    edit left a duplicated ``function renderMessage(`` line in chat_render.js
    → brace imbalance → EOF mid-construct) must NOT refuse the whole bundle.
    The build bisects per-file, EXCLUDES the broken source, and ships a
    degraded bundle — blast radius = the broken module, not the app. (The old
    contract — refuse the whole bundle, serve the dev-fallback — pushed every
    user onto a slower page that served the SAME broken file raw.)"""
    from lib import js_bundler

    files = {
        'a.js': 'window.A = 1;\n',
        'bad.js': 'window.B = -;\n',   # syntactically invalid, scanner-clean
    }
    js_dir = _make_js_tree(tmp_path, files)
    _reset_state(monkeypatch, js_dir, files)

    name = js_bundler.build_bundle()
    assert name and name.startswith('bundle-'), (
        'one syntax-broken NON-critical file must degrade to module-absent, '
        'not refuse the whole bundle'
    )
    bundle_text = (tmp_path / 'js' / name).read_text(encoding='utf-8')
    assert 'window.B' not in bundle_text, 'the broken file must be EXCLUDED'
    assert 'window.A = 1;' in bundle_text, 'the healthy file must survive'
    # The shipped degraded bundle must itself parse.
    ok, detail = js_bundler._node_syntax_ok(str(tmp_path / 'js' / name))
    assert ok, detail


@pytest.mark.skipif(shutil.which('node') is None, reason='node not installed')
def test_syntax_broken_critical_source_is_fatal(tmp_path, monkeypatch):
    """The flip side: when the syntax-broken file is CRITICAL, the build must
    still refuse (dev-fallback + load guard banner) — degradation must never
    ship a silently-crippled core."""
    from lib import js_bundler

    files = {
        'i18n.js': 'window.t = function(k){ return k; };\nfunction broken( {\n',
        'main.js': 'window.M = 1;\n',
    }
    js_dir = _make_js_tree(tmp_path, files)
    _reset_state(monkeypatch, js_dir, files)
    monkeypatch.setattr(js_bundler, '_CRITICAL_FILES', frozenset({'i18n.js'}))

    assert js_bundler.build_bundle() is None, (
        'a syntax-broken CRITICAL file must be fatal (dev-fallback surfaces it)'
    )
    leftovers = [f for f in os.listdir(js_dir) if f.startswith('bundle-')]
    assert not leftovers, 'the refused bundle must not stay on disk'


@pytest.mark.skipif(shutil.which('node') is None, reason='node not installed')
def test_neutered_bisect_makes_syntax_breakage_fatal(tmp_path, monkeypatch):
    """NC bite: with _find_syntax_broken_sources neutered (the gate failure
    can no longer be attributed to its file), a syntax-broken source is FATAL
    again — proving the bisect is exactly what turns this incident class from
    app-wide refusal into per-module degradation."""
    from lib import js_bundler

    files = {
        'a.js': 'window.A = 1;\n',
        'bad.js': 'window.B = -;\n',
    }
    js_dir = _make_js_tree(tmp_path, files)
    _reset_state(monkeypatch, js_dir, files)
    monkeypatch.setattr(js_bundler, '_find_syntax_broken_sources', lambda names: [])

    assert js_bundler.build_bundle() is None


@pytest.mark.skipif(shutil.which('node') is None, reason='node not installed')
def test_gate_still_failing_after_exclusion_refuses(tmp_path, monkeypatch):
    """If the re-assembled bundle STILL fails the gate after the broken source
    was excluded (a cross-file gluing bug, not a bad module), the build must
    refuse rather than serve it."""
    from lib import js_bundler

    files = {
        'a.js': 'window.A = 1;\n',
        'bad.js': 'window.B = -;\n',
    }
    js_dir = _make_js_tree(tmp_path, files)
    _reset_state(monkeypatch, js_dir, files)
    # Force the whole-bundle gate to fail on BOTH attempts; the real bisect
    # still runs and correctly excludes bad.js, but attempt 2 must refuse.
    monkeypatch.setattr(js_bundler, '_node_syntax_ok', lambda path: (False, 'forced failure'))

    assert js_bundler.build_bundle() is None
    leftovers = [f for f in os.listdir(js_dir) if f.startswith('bundle-')]
    assert not leftovers, 'a twice-failing bundle must not stay on disk'
