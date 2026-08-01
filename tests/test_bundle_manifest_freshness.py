#!/usr/bin/env python3
"""tests/test_bundle_manifest_freshness.py — the STALE-MANIFEST guard.

The defect class, fired twice in production:

* 2026-07-24 — commit e0a49243 added ``core/model_caps.js`` to
  ``_BUNDLE_FILES``; a long-running server had imported the OLD list, so
  its (correctly-triggered) rebuilds assembled the import-time-frozen
  manifest and the shipped bundle silently lacked the file. Every model
  picker threw ``ReferenceError: isChatModel is not defined``.
* 2026-07-31 — SAME mechanism, second casualty: ``core/conv_save.js``
  (``ReferenceError: saveConversations is not defined`` at 108 call
  sites) and ``core/conv_verify_retry.js`` were both absent from the
  served bundle even though the on-disk manifest listed them.

The 7-24 fix guarded ONE instance (hard-coded ``_MODEL_CAPS_SIGNATURES``
in test_frontend_model_caps_bundled.py) — necessary, not sufficient: a
whitelist cannot cover the NEXT added leaf. This suite guards the CLASS:

1. ``test_build_with_stale_in_memory_manifest_still_ships_current_disk_list``
   — the 7-31 incident replayed: in-memory lists deliberately stale, build
   must still ship the on-disk manifest. Pre-fix this is red for exactly
   the production reason.
2. ``test_every_manifest_entry_leaves_fingerprint_in_built_artifacts`` —
   EVERY manifest entry (core + deferred) must leave a minification-stable
   fingerprint in its built artifact. Derived from each entry's own source,
   so future entries are covered the moment they are added — no per-file
   signatures to maintain.
3. ``test_source_max_mtime_covers_the_manifest_file_itself`` — the gate
   half of the class: a membership-only edit must bump _source_max_mtime.
4. ``test_refresh_keeps_last_known_good_on_broken_manifest`` +
   ``test_extractor_rejects_non_literal_manifest`` — the fail-safe: a
   broken / cleverly-rewritten manifest fails LOUDLY and never poisons the
   last-known-good lists.
5. ``test_refresh_runs_before_i18n_pack_emission`` — ordering: the pack
   extractor reads ``js_bundler._BUNDLE_FILES`` per call, so the refresh
   must land before pack emission inside build_bundle().

NEUTER expectation (what bites what): removing the ``_refresh_manifest()``
call from ``build_bundle()`` flips tests 1 and 5 red (2 RED); tests 2/3/4
stay green in a fresh process (their in-memory manifest equals disk), which
is exactly why 1 exists — the class only appears in a LONG-RUNNING process,
and test 1 simulates that process.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_bundle_manifest_freshness.py -v
"""
from __future__ import annotations

import logging
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

# The PRODUCTION js dir, captured at import BEFORE the autouse fixture
# below redirects js_bundler.JS_DIR to the symlink farm. Guard 6
# snapshots this dir around a build to prove the suite leaves it
# untouched.
from lib import js_bundler as _jb_at_import
_REAL_JS_DIR = _jb_at_import.JS_DIR


@pytest.fixture(autouse=True)
def _build_in_symlink_farm(monkeypatch, tmp_path):
    """Redirect every bundle build in this suite into a THROWAWAY
    symlink farm — never the production ``static/js`` tree.

    Why (2026-08-01 incident): guards 1/2/5 call ``build_bundle()``
    directly. Before this fixture, the builds ran against the REAL
    ``js_bundler.JS_DIR``: they published new-shape bundles into the
    served directory AND ``_clean_old_bundles`` deleted the artifacts
    the live server was currently advertising
    (``feature-8204ccdc.js`` → 404 for every deferred feature until a
    manual rebuild re-emitted it). A test run must be able to break
    NOTHING in production serving.

    The farm symlinks each manifest entry (+ i18n.js for the pack
    emitter) so reads resolve to the real sources, while every WRITE
    (bundle publish, pack emit, old-bundle sweep, build lock) lands in
    ``tmp_path``. ``_BUILD_LOCK_PATH`` is a module-level constant, so
    it is redirected explicitly alongside ``JS_DIR``.
    """
    from lib import js_bundler

    real_dir = js_bundler.JS_DIR
    farm = tmp_path / 'jsfarm'
    farm.mkdir()
    entries = set(js_bundler._BUNDLE_FILES) | set(js_bundler._DEFERRED_FILES)
    entries.add('i18n.js')
    for entry in entries:
        src = os.path.join(real_dir, entry)
        if not os.path.exists(src):
            continue
        dst = farm / entry
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.symlink_to(src)
    monkeypatch.setattr(js_bundler, 'JS_DIR', str(farm))
    monkeypatch.setattr(js_bundler, '_BUILD_LOCK_PATH',
                        str(tmp_path / '.bundle-build.lock'))
    yield


def _production_bundle_set():
    """The set of built artifacts currently in the PRODUCTION js dir
    (bundle-*/feature-*/i18n-*.js). Guard 6 asserts a suite build
    leaves it byte-for-byte untouched."""
    from lib import js_bundler
    out = set()
    for name in os.listdir(_REAL_JS_DIR):
        if js_bundler._BUILT_BUNDLE_RE.match(name):
            out.add(name)
    return out


def _disk_manifest():
    """The manifest as the NEXT process import will see it — parsed from
    disk via the production extractor, never from a (possibly stale)
    in-process binding."""
    from lib import js_bundler
    return js_bundler._extract_manifest_from_source(js_bundler.__file__)


def _read_artifact(filename):
    from lib import js_bundler
    with open(os.path.join(js_bundler.JS_DIR, filename), encoding='utf-8') as f:
        return f.read()


_FUNC_DECL_RE = re.compile(r'^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(', re.M)
_TOPLEVEL_VAR_RE = re.compile(r'^(?:var|let|const)\s+([A-Za-z_$][\w$]*)\s*=', re.M)
_WINDOW_ASSIGN_RE = re.compile(r'^window\.([A-Za-z_$][\w$]*)\s*=', re.M)
_STRING_LIT_RE = re.compile(r"'([^'\\\n]{16,})'|\"([^\"\\\n]{16,})\"")


def _entry_markers(entry):
    """Minification-stable fingerprints for one manifest entry, derived from
    the entry's OWN source — never a hard-coded whitelist.

    These constructs survive BOTH minify paths: script-mode esbuild never
    renames top-level globals and always preserves ``window.X`` member
    names and string contents (see js_bundler's safety argument);
    ``_minify_js`` only strips comments/whitespace.

    Returns a list of compiled regexes; ANY ONE match in the artifact
    proves the entry was concatenated.
    """
    from lib import js_bundler
    path = os.path.join(js_bundler.JS_DIR, entry)
    with open(path, encoding='utf-8') as f:
        src = f.read()
    markers = []
    # window.X = … — the member name is never renamed (even when nested),
    # so a commented-out assignment can't false-mark: scan the
    # comment-stripped source (indentation dropped → ^ matches any depth).
    stripped = js_bundler._minify_js(src)
    for name in _WINDOW_ASSIGN_RE.findall(stripped):
        markers.append(re.compile(r'window\.' + re.escape(name) + r'\s*='))
    # Top-level function declarations — column-0 in the ORIGINAL source
    # (nested decls are indented by house style; esbuild mangles only
    # function-locals, and those would be indented here).
    for name in _FUNC_DECL_RE.findall(src):
        markers.append(re.compile(r'(?:async\s+)?function\s+'
                                  + re.escape(name) + r'\s*\('))
    # Top-level var/let/const declarations — same survival argument (a
    # decl-only stub like settings.js has no functions at all).
    for name in _TOPLEVEL_VAR_RE.findall(src):
        markers.append(re.compile(r'(?:var|let|const)\s+' + re.escape(name) + r'\s*='))
    if not markers:
        # Last resort for decl-free files: distinctive string literals —
        # contents survive both minifiers verbatim (quote style may change,
        # so match the raw content).
        for m in _STRING_LIT_RE.finditer(src):
            lit = m.group(1) or m.group(2)
            if lit.isprintable():
                markers.append(re.compile(re.escape(lit)))
            if len(markers) >= 3:
                break
    return markers


# ── Guard 1: the incident, replayed ═════════════════════════════════════

def test_build_with_stale_in_memory_manifest_still_ships_current_disk_list(monkeypatch):
    """This process "imported" the manifest BEFORE the 7-31 casualty files
    were added (its in-memory _BUNDLE_FILES lacks them), yet the build MUST
    still ship them — build_bundle() re-reads the manifest from disk.
    Pre-fix this fails exactly as production did: the artifact silently
    lacks both files while every source-level guard stays green."""
    from lib import js_bundler

    bundle_files, _deferred, _entry_points, _critical = _disk_manifest()
    casualties = [e for e in ('core/conv_save.js', 'core/conv_verify_retry.js')
                  if e in bundle_files]
    assert casualties == ['core/conv_save.js', 'core/conv_verify_retry.js'], (
        'the 2026-07-31 casualty files must be in the manifest for this replay')
    stale = [e for e in bundle_files if e not in casualties]

    # The long-running process: binding frozen BEFORE the casualties landed,
    # watermark older than the file, no usable bundle manifest in memory.
    monkeypatch.setattr(js_bundler, '_BUNDLE_FILES', stale)
    monkeypatch.setattr(js_bundler, '_manifest_source_mtime', 0.0)
    monkeypatch.setattr(js_bundler, '_bundle_filename', None)
    monkeypatch.setattr(js_bundler, '_bundle_mtime', 0)

    name = js_bundler.build_bundle()
    assert name, 'build_bundle() returned None — core bundle failed to build'
    artifact = _read_artifact(name)
    for entry in casualties:
        markers = _entry_markers(entry)
        assert markers, f'no extractable fingerprint for {entry}'
        assert any(m.search(artifact) for m in markers), (
            f'{entry} is in the on-disk manifest but left NO fingerprint in '
            f'the built bundle — the build assembled the stale in-memory '
            f'manifest (the 2026-07-31 ReferenceError class).')
    # …and the refresh must have healed the in-memory binding itself.
    assert list(js_bundler._BUNDLE_FILES) == bundle_files, (
        '_refresh_manifest() did not re-bind _BUNDLE_FILES to the on-disk manifest')


# ── Guard 2: class-level artifact assertion (no whitelist) ══════════════

def test_every_manifest_entry_leaves_fingerprint_in_built_artifacts():
    """Class-level successor to the instance-level model_caps guard: derive
    fingerprints from the CURRENT manifest and require EVERY entry — core
    and deferred — to be detectable in its built artifact. A future Epic-E
    leaf needs no new test line: it is covered the moment it is listed."""
    from lib import js_bundler

    bundle_files, deferred_files, _entry_points, _critical = _disk_manifest()
    core_name = js_bundler.build_bundle()
    assert core_name, 'core bundle build failed'
    artifacts = {'core': _read_artifact(core_name)}
    if js_bundler._feature_filename:
        artifacts['feature'] = _read_artifact(js_bundler._feature_filename)

    missing = []
    for entry in (*bundle_files, *deferred_files):
        kind = 'core' if entry in bundle_files else 'feature'
        if entry == 'i18n.js' and not js_bundler._bundle_includes_i18n:
            continue  # shipped as single-language packs in this build (by design)
        if kind == 'feature' and 'feature' not in artifacts:
            continue  # feature bundle failed to build — non-fatal by design
        markers = _entry_markers(entry)
        if not markers:
            missing.append((entry, 'no extractable fingerprint in source'))
        elif not any(m.search(artifacts[kind]) for m in markers):
            missing.append((entry, f'fingerprint absent from {kind} artifact'))
    assert not missing, (
        'manifest entries that left no fingerprint in the built artifact '
        '(stale-manifest / silent-drop class): '
        + ', '.join(f'{e} ({r})' for e, r in missing[:10]))


# ── Guard 6: a suite build never touches the production tree ════════════

def test_build_never_touches_production_js_dir():
    """The 2026-08-01 incident, pinned: a suite-run build must publish
    NOTHING into the production js dir and delete NOTHING from it. The
    symlink-farm fixture (autouse) is what makes this pass; without it,
    ``build_bundle()`` publishes into static/js and _clean_old_bundles
    deletes the artifacts the live server is currently advertising —
    the feature-bundle 404 chain."""
    from lib import js_bundler

    before = _production_bundle_set()
    name = js_bundler.build_bundle()
    assert name, 'build_bundle() returned None'
    after = _production_bundle_set()
    assert after == before, (
        'a suite build mutated the production js dir '
        f'(added={sorted(after - before)}, removed={sorted(before - after)}) '
        '— the symlink-farm fixture must redirect ALL build writes')


# ── Guard 3: the gate half of the class ═════════════════════════════════

def test_source_max_mtime_covers_the_manifest_file_itself():
    """_source_max_mtime() MUST be >= the bundler module's own mtime — the
    manifest lives in that file, so a membership-only edit (no .js file
    touched) must still trigger the rebuild gate."""
    from lib import js_bundler
    assert js_bundler._source_max_mtime() >= os.path.getmtime(js_bundler.__file__), (
        '_source_max_mtime() no longer stats lib/js_bundler.py — an edit to '
        '_BUNDLE_FILES would not trigger a rebuild (the gate half of the '
        'stale-manifest class).')


# ── Guard 4: fail-safe on a broken / cleverly-rewritten manifest ════════

def test_refresh_keeps_last_known_good_on_broken_manifest(monkeypatch, tmp_path, caplog):
    """A manifest that no longer parses must NEVER poison the running
    process: refresh returns False, the last-known-good lists are kept
    byte-identical, and an ERROR is logged (silent staleness is how this
    class killed prod twice; loud is the whole point)."""
    from lib import js_bundler

    broken = tmp_path / 'js_bundler.py'
    broken.write_text('_BUNDLE_FILES = [1, 2, 3]  # ints; other three missing\n',
                      encoding='utf-8')
    before = (list(js_bundler._BUNDLE_FILES), list(js_bundler._DEFERRED_FILES),
              tuple(js_bundler._DEFERRED_ENTRY_POINTS), set(js_bundler._CRITICAL_FILES))
    monkeypatch.setattr(js_bundler, '__file__', str(broken))
    monkeypatch.setattr(js_bundler, '_manifest_source_mtime', 0.0)
    with caplog.at_level(logging.ERROR, logger='lib.js_bundler'):
        assert js_bundler._refresh_manifest() is False
    after = (list(js_bundler._BUNDLE_FILES), list(js_bundler._DEFERRED_FILES),
             tuple(js_bundler._DEFERRED_ENTRY_POINTS), set(js_bundler._CRITICAL_FILES))
    assert after == before, 'a broken manifest must never rebind the lists'
    assert 're-parse failed' in caplog.text, (
        'the fail-safe must log ERROR — silent staleness is the incident class')


def test_extractor_rejects_non_literal_manifest(tmp_path):
    """The extractor only accepts PLAIN literals: anything smarter (concat,
    comprehension, conditional) raises instead of being half-evaluated.
    This is what makes a clever refactor loud rather than silently stale."""
    from lib import js_bundler

    shady = tmp_path / 'm.py'
    shady.write_text(
        '_BUNDLE_FILES = ["a.js"] + ["b.js"]\n'
        '_DEFERRED_FILES = []\n'
        '_DEFERRED_ENTRY_POINTS = ()\n'
        '_CRITICAL_FILES = frozenset({"x.js"})\n',
        encoding='utf-8')
    with pytest.raises((ValueError, TypeError)):
        js_bundler._extract_manifest_from_source(str(shady))


# ── Guard 5: refresh precedes i18n pack emission ════════════════════════

def test_refresh_runs_before_i18n_pack_emission(monkeypatch):
    """lib/i18n_boot_keys.py reads js_bundler._BUNDLE_FILES per call when
    extracting boot keys — if the refresh happened AFTER pack emission, a
    changed manifest would build the packs from the stale list (same class,
    one layer down). Spy on the emitter and capture what it saw."""
    from lib import js_bundler
    import lib.i18n_packs

    bundle_files, _deferred, _entry_points, _critical = _disk_manifest()
    captured = {}

    def spy(js_dir, source_path=None, **_kw):
        captured['files'] = list(js_bundler._BUNDLE_FILES)
        return {}  # no packs → core bundle keeps i18n.js; build proceeds

    monkeypatch.setattr(lib.i18n_packs, 'emit_pack_files', spy)
    monkeypatch.setattr(js_bundler, '_BUNDLE_FILES', ['__stale_probe__.js'])
    monkeypatch.setattr(js_bundler, '_manifest_source_mtime', 0.0)
    monkeypatch.setattr(js_bundler, '_bundle_filename', None)
    monkeypatch.setattr(js_bundler, '_bundle_mtime', 0)

    name = js_bundler.build_bundle()
    assert name, 'build_bundle() returned None'
    assert captured.get('files') == bundle_files, (
        'pack emission observed a STALE _BUNDLE_FILES — _refresh_manifest() '
        'must run before emit_pack_files inside build_bundle()')


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
