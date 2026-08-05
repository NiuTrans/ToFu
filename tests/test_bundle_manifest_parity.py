# Incident anchor: born in commit ab99ef8b — checkpoint: accumulated work since last commit
# (funeral audit pt_c565a36b3e8f42e6, docs/RATCHET_AUDIT.md)
"""Closed-system parity test for the JS-bundler allowlist trap (CLAUDE.md §3.2.1).

The bundler's "which file is an app script" decision is declared in FIVE
places that must all agree, or a file silently loads as a no-op (missing from
a manifest → stripped-but-not-rebundled) or double-loads (in a manifest AND
its raw tag survives → duplicate-IIFE crash):

  1. ``_BUNDLE_FILES``        (core bundle)        — lib/js_bundler.py
  2. ``_DEFERRED_FILES``      (deferred bundle)    — lib/js_bundler.py
  3. ``_APP_SCRIPTS_RE`` strip set                 — routes/common.py
  4. ``_CRITICAL_FILES`` ⊆ ``_BUNDLE_FILES``       — lib/js_bundler.py
  5. ``_DEFERRED_ENTRY_POINTS`` ↔ deferred sources — lib/js_bundler.py

The deferred feature bundle WIDENED this trap (a file added to _DEFERRED_FILES
but forgotten in the strip set, or vice versa, is a new silent-orphan variant).
This test asserts the whole system is closed so a future edit that breaks any
edge fails loudly here instead of in production.

Only ONE top-level app script is intentionally unbundled — ``relay-admin.js``
(loads solely on the standalone ``/admin`` page). Rather than a bare exemption
that would let a genuinely-orphaned new file hide behind it, we assert the
invariant that MAKES it safe: it is a ``<script>`` tag in ``static/admin.html``
and NOT in ``index.html``.

SUBDIRECTORY BLIND SPOT (2026-07-13 paper-modes outage): the disk-orphan edge
(Edge 1) only scans TOP-LEVEL ``static/js/*.js``. A file in a SUBDIRECTORY
(e.g. ``paper/library.js``) forgotten from ``_DEFERRED_FILES`` is NOT caught by
Edge 1, so its cross-file globals go undefined in the built bundle and every
sibling that references them throws at runtime. ``paper-reader.js`` was
decomposed (Epic E) and its library state moved to ``paper/library.js``; that
file was missing from the manifest → the whole deferred bundle omitted it →
``_paperLibrary``/``_loadPaperLibrary``/``_saveActivePaperState`` were undefined
and EVERY paper sub-mode crashed on open. ``test_paper_reader_cross_file_globals_are_bundled``
closes that specific gap structurally.
"""
from __future__ import annotations

import os
import re

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_DIR = os.path.join(PROJECT_ROOT, 'static', 'js')

# The one top-level static/js/*.js NOT built into either bundle. Its safety
# invariant is asserted by test_unbundled_whitelist_is_justified — this set is
# NOT a free pass; a new orphan can only join it by also being admin-only.
_UNBUNDLED_WHITELIST = frozenset({'relay-admin.js'})

# Built bundle artifact filenames (bundle-<8hex>.js / feature-<8hex>.js /
# i18n-<lang>-<8hex>.js — the last is the single-language i18n pack emitted by
# lib/i18n_packs.py, Epic-E sub-part 1 slice 2). Kept in lockstep with
# lib/js_bundler.py::_BUILT_BUNDLE_RE.
_BUILT_BUNDLE_RE = re.compile(
    r'^(?:(?:bundle|feature)-[0-9a-f]{8}|i18n-(?:zh|en)-[0-9a-f]{8})\.js$')

# compaction-viewer.js is historically absent from the audit's index.html
# regex scope in the sibling test; here it IS in _BUNDLE_FILES so it needs no
# special-casing (it participates in every edge normally).


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def _index_html():
    return _read(os.path.join(PROJECT_ROOT, 'index.html'))


def _top_level_disk_scripts():
    """Every top-level static/js/*.js on disk, excluding built bundle outputs."""
    return {
        f for f in os.listdir(JS_DIR)
        if f.endswith('.js') and not _BUILT_BUNDLE_RE.match(f)
    }


def _index_script_srcs():
    """Every ``static/js/...`` src referenced by a <script> tag in index.html
    (raw src value incl. any ?v= suffix), paired with the bare filename path."""
    html = _index_html()
    # Capture the full src (with optional query) AND the static/js path portion.
    return re.findall(r'<script[^>]+src="(static/js/[\w./-]+\.js[^"]*)"', html)


# ── Edge 1: disk ⊆ manifests (+ justified whitelist) ──────────────────────

def test_disk_top_level_scripts_are_all_bundled_or_whitelisted():
    """A NEW top-level static/js/*.js forgotten from BOTH manifests fails here.

    This is auto-discovery-as-a-test: the runtime manifests stay hand-ordered
    (load order matters), but the disk is the ground truth for "does a file
    exist that nobody bundles". The only allowed miss is the justified
    admin-only whitelist.
    """
    from lib.js_bundler import _BUNDLE_FILES, _DEFERRED_FILES
    known = set(_BUNDLE_FILES) | set(_DEFERRED_FILES) | set(_UNBUNDLED_WHITELIST)
    orphans = _top_level_disk_scripts() - known
    assert not orphans, (
        'Top-level static/js/*.js on disk but in NEITHER _BUNDLE_FILES nor '
        '_DEFERRED_FILES (silent-orphan trap — the strip regex removes their '
        f'<script> tag but no bundle rebuilds them): {sorted(orphans)}'
    )


# ── Edge 2: manifests ⊆ disk (no stale rename entry) ──────────────────────

def test_manifest_entries_exist_on_disk():
    """Every manifest entry must resolve to a real file (catches the api-keys.js
    class: a renamed/removed file leaving a stale manifest entry)."""
    from lib.js_bundler import _BUNDLE_FILES, _DEFERRED_FILES
    missing = [
        name for name in (*_BUNDLE_FILES, *_DEFERRED_FILES)
        if not os.path.exists(os.path.join(JS_DIR, name))
    ]
    assert not missing, (
        f'Manifest entries with no file on disk (stale rename?): {sorted(missing)}'
    )


# ── Edge 3: manifest ↔ index.html strip set (BIDIRECTIONAL, via REAL regex) ─

def test_every_manifest_file_has_dev_fallback_tag():
    """Forward: every bundled file has a <script> tag in index.html so the
    dev-fallback path (bundling failed → serve individual tags) still boots."""
    from lib.js_bundler import _BUNDLE_FILES, _DEFERRED_FILES
    html = _index_html()
    for name in (*_BUNDLE_FILES, *_DEFERRED_FILES):
        assert re.search(
            r'<script[^>]+src="static/js/' + re.escape(name) + r'[^"]*"', html
        ), (
            f'{name} is bundled but has NO <script> tag in index.html — the '
            'dev fallback (individual tags when bundling fails) would drop it.'
        )


def test_every_stripped_index_script_is_rebundled():
    """Reverse (the edge that matters most): every <script> tag in index.html
    that the REAL strip regex would remove MUST be rebuilt by a manifest — else
    it's a silent orphan (stripped, never re-added). Drives the shipped
    ``is_stripped_app_script`` predicate (which shares its sub-pattern with
    ``_APP_SCRIPTS_RE``), so this closes the regex↔manifest edge structurally,
    not by a re-typed copy of the pattern."""
    from routes.common import is_stripped_app_script
    from lib.js_bundler import _BUNDLE_FILES, _DEFERRED_FILES

    rebuilt = set(_BUNDLE_FILES) | set(_DEFERRED_FILES) | set(_UNBUNDLED_WHITELIST)
    orphaned = []
    for src in _index_script_srcs():
        if not is_stripped_app_script(src):
            continue  # e.g. the built bundle-<hash>.js tag itself
        # Strip the static/js/ prefix + any ?v= query to get the bare path.
        path = re.sub(r'\?.*$', '', src[len('static/js/'):])
        if path not in rebuilt:
            orphaned.append(path)
    assert not orphaned, (
        'index.html <script> tags the strip regex REMOVES but no manifest '
        f'rebuilds (silent-orphan / would vanish from served HTML): {sorted(orphaned)}'
    )


def test_strip_regex_and_predicate_share_one_subpattern():
    """The predicate must be DERIVED from the same sub-pattern the strip regex
    uses — not a re-typed copy (which would be a sixth drift source). Assert the
    shared constant is literally embedded in the compiled strip regex, and that
    the predicate agrees with the regex on the bundle-exclusion boundary."""
    from routes.common import (
        _APP_SCRIPT_SRC_SUBPATTERN, _APP_SCRIPTS_RE, is_stripped_app_script,
    )
    assert _APP_SCRIPT_SRC_SUBPATTERN in _APP_SCRIPTS_RE.pattern, (
        'strip regex no longer built from the shared sub-pattern — predicate '
        'and regex can now drift (the exact bug this refactor removed).'
    )
    # The load-bearing (?!bundle-) boundary: an app script strips, the built
    # bundle tag does NOT (else re-serving cached HTML would strip its own tag).
    assert is_stripped_app_script('static/js/core/folders.js?v=1')
    assert not is_stripped_app_script('static/js/bundle-a3f8b2c1.js')


# ── Edge 4: _CRITICAL_FILES ⊆ _BUNDLE_FILES ───────────────────────────────

def test_critical_files_subset_of_bundle_files():
    """Mirror of the invariant in test_bundle_corruption_guard.py — asserted
    here too so the whole closed system lives in one place. A _CRITICAL_FILES
    entry not in _BUNDLE_FILES means a rename silently emptied the critical set
    (a partial bundle would then ship a crippled app)."""
    from lib.js_bundler import _BUNDLE_FILES, _CRITICAL_FILES
    assert _CRITICAL_FILES, '_CRITICAL_FILES must not be empty'
    drift = _CRITICAL_FILES - set(_BUNDLE_FILES)
    assert not drift, f'_CRITICAL_FILES not in _BUNDLE_FILES (rename drift): {sorted(drift)}'


# ── Edge 5: _DEFERRED_ENTRY_POINTS ↔ functions defined in _DEFERRED_FILES ──

def test_deferred_entry_points_are_defined_in_deferred_sources():
    """Real parity (upgrading the sibling test's len()==3 sanity check): every
    name in _DEFERRED_ENTRY_POINTS must actually be DEFINED in a deferred source
    file (as ``function name`` or ``window.name =``). feature-loader.js installs
    a lazy stub for each of these before boot; if an entry point isn't really
    defined in the deferred bundle, its stub never gets swapped for the real fn
    and the feature silently no-ops on open."""
    from lib.js_bundler import _DEFERRED_FILES, _DEFERRED_ENTRY_POINTS

    defined = set()
    for name in _DEFERRED_FILES:
        src = _read(os.path.join(JS_DIR, name))
        for fn in _DEFERRED_ENTRY_POINTS:
            if (re.search(r'\bfunction\s+' + re.escape(fn) + r'\b', src)
                    or re.search(r'\bwindow\.' + re.escape(fn) + r'\s*=', src)):
                defined.add(fn)

    missing = set(_DEFERRED_ENTRY_POINTS) - defined
    assert not missing, (
        'Declared deferred entry points NOT defined in any _DEFERRED_FILES '
        f'source (lazy stub would never resolve): {sorted(missing)}'
    )


def test_deferred_files_not_in_core_bundle():
    """A deferred file must NOT also be in the core bundle (double-load →
    duplicate-IIFE crash). The loader itself IS core."""
    from lib.js_bundler import _BUNDLE_FILES, _DEFERRED_FILES
    both = set(_BUNDLE_FILES) & set(_DEFERRED_FILES)
    assert not both, f'files in BOTH core and deferred manifests (double-load): {sorted(both)}'
    assert 'feature-loader.js' in _BUNDLE_FILES, (
        'feature-loader.js (the on-demand loader) must be in the CORE bundle so '
        'the lazy stubs are installed before boot.'
    )


# ── image-gen deferral (explicit residency pin — half-revert fails loudly) ─

_IMAGE_GEN_ENTRY_POINTS = frozenset({
    'enterImageGenMode', 'exitImageGenMode', 'generateImageDirect',
    'selectIgAspect', 'selectIgCount', 'selectIgResolution', 'toggleIgModelDropdown',
})


def test_image_gen_is_deferred_not_core():
    """image-gen.js was moved from the core bundle to the deferred feature
    bundle (2026-07-05). Pin the residency explicitly so a future half-revert
    (moving the file back to core without cleaning up the entry points, or
    dropping it from _DEFERRED_FILES) fails HERE with a targeted message rather
    than only via the generic closed-system edges."""
    from lib.js_bundler import _BUNDLE_FILES, _DEFERRED_FILES, _DEFERRED_ENTRY_POINTS
    assert 'image-gen.js' in _DEFERRED_FILES, (
        'image-gen.js must be in _DEFERRED_FILES (deferred feature bundle).'
    )
    assert 'image-gen.js' not in _BUNDLE_FILES, (
        'image-gen.js is deferred — it must NOT also be in the core _BUNDLE_FILES '
        '(double-load / duplicate-IIFE hazard, and it would negate the TTI win).'
    )
    # Every image-gen onclick entry point must be a declared deferred entry
    # point (so feature-loader.js stubs it and the first click loads the bundle).
    missing = _IMAGE_GEN_ENTRY_POINTS - set(_DEFERRED_ENTRY_POINTS)
    assert not missing, (
        'image-gen onclick entry points missing from _DEFERRED_ENTRY_POINTS '
        f'(their buttons would be dead until another entry point loads): {sorted(missing)}'
    )


# ── Project Brain deferral (residency pin + the no-load conv-switch design) ─

_PROJECT_BRAIN_FILES = ('project-brain.js', 'project-brain-peers.js',
                        'project-brain-status.js', 'project-brain-i18n.js')
# ONLY the user-triggered openers are deferred entry points (loading stubs).
_PROJECT_BRAIN_OPENERS = frozenset({
    'openProjectBrain', 'toggleProjectBrain', 'openProjectBrainInfluence',
})
# These are called on boot / conv-switch (main.js:637 projectBrainRefresh) or
# only when the panel is already open (closeProjectBrain, overlay onclick). They
# must NOT be deferred entry points — a loading stub there would fetch the whole
# feature bundle on every conversation switch, negating the deferral entirely.
_PROJECT_BRAIN_NO_LOAD = frozenset({'projectBrainRefresh', 'closeProjectBrain'})


def test_project_brain_is_deferred_not_core():
    """The Project Brain cluster was moved from core to the deferred feature
    bundle (2026-07-09, −16.2KB eager gzip). Pin the residency so a half-revert
    (a file back in core, or dropped from _DEFERRED_FILES) fails HERE."""
    from lib.js_bundler import _BUNDLE_FILES, _DEFERRED_FILES, _DEFERRED_ENTRY_POINTS
    for name in _PROJECT_BRAIN_FILES:
        assert name in _DEFERRED_FILES, f'{name} must be in _DEFERRED_FILES (deferred).'
        assert name not in _BUNDLE_FILES, (
            f'{name} is deferred — must NOT also be in core _BUNDLE_FILES '
            '(double-load, and it would negate the eager-gzip win).'
        )
    # peers/status/i18n read window.ProjectBrain._state at runtime → they must
    # order AFTER project-brain.js WITHIN the deferred list (same window scope).
    order = [n for n in _DEFERRED_FILES if n in _PROJECT_BRAIN_FILES]
    assert order[0] == 'project-brain.js', (
        f'project-brain.js must precede its siblings in _DEFERRED_FILES: {order}'
    )
    missing = _PROJECT_BRAIN_OPENERS - set(_DEFERRED_ENTRY_POINTS)
    assert not missing, (
        f'Project Brain openers missing from _DEFERRED_ENTRY_POINTS: {sorted(missing)}'
    )


def test_project_brain_conv_switch_fns_are_not_loading_stubs():
    """THE no-load design proof: projectBrainRefresh (conv-switch) and
    closeProjectBrain (overlay onclick) must NOT be deferred entry points, in
    EITHER _DEFERRED_ENTRY_POINTS list. If they were, feature-loader would install
    a bundle-loading stub for them → the first conv-switch fetches the deferred
    bundle seconds into boot → the cluster is no longer deferred at all."""
    from lib.js_bundler import _DEFERRED_ENTRY_POINTS
    loader = _read(os.path.join(JS_DIR, 'feature-loader.js'))
    for fn in _PROJECT_BRAIN_NO_LOAD:
        assert fn not in _DEFERRED_ENTRY_POINTS, (
            f'{fn} is a boot/conv-switch path fn — it must NOT be a deferred '
            'entry point (a loading stub would fetch the bundle on conv-switch).'
        )
        # It also must not be stubbed as a quoted entry-point token in the
        # feature-loader array (its _DEFERRED_ENTRY_POINTS.forEach installs stubs).
        assert not re.search(
            r"_DEFERRED_ENTRY_POINTS\s*=\s*\[[^\]]*'" + re.escape(fn) + r"'",
            loader, re.DOTALL,
        ), f"{fn} must not appear in feature-loader.js's _DEFERRED_ENTRY_POINTS array."


def test_feature_loader_entry_points_match_jsbundler():
    """feature-loader.js hard-codes its own _DEFERRED_ENTRY_POINTS array (it runs
    in the browser, can't import the Python list). Assert the two agree, so
    adding an entry point to one but not the other can't silently leave a
    button unstubbed."""
    from lib.js_bundler import _DEFERRED_ENTRY_POINTS
    loader = _read(os.path.join(JS_DIR, 'feature-loader.js'))
    for name in _DEFERRED_ENTRY_POINTS:
        assert re.search(r"'" + re.escape(name) + r"'", loader), (
            f'{name} is in lib/js_bundler._DEFERRED_ENTRY_POINTS but NOT in '
            "feature-loader.js's _DEFERRED_ENTRY_POINTS array — its lazy stub "
            'would never be installed.'
        )


# ── Paper decomposition: cross-file globals must be bundled (subdir gap) ──

# The library-state globals that moved OUT of paper-reader.js into
# paper/library.js (Epic E, 2026-07-11). paper-reader.js references these
# inside enterPaperMode / exitPaperMode / _onReady; if library.js is missing
# from the manifest they are undefined in the built bundle and every paper
# sub-mode throws on open. These three are the ones observed in the outage.
_PAPER_LIBRARY_GLOBALS = frozenset({
    '_paperLibrary', '_loadPaperLibrary', '_saveActivePaperState',
})


def test_paper_library_is_deferred_not_core():
    """paper/library.js (extracted from paper-reader.js, Epic E) must be in the
    deferred bundle alongside paper-reader.js and NOT in the core bundle. A
    half-revert (dropped from _DEFERRED_FILES) recreates the 2026-07-13 outage
    where every paper mode crashed because library.js's globals were undefined."""
    from lib.js_bundler import _BUNDLE_FILES, _DEFERRED_FILES
    assert 'paper/library.js' in _DEFERRED_FILES, (
        'paper/library.js must be in _DEFERRED_FILES — it owns _paperLibrary / '
        '_loadPaperLibrary / _saveActivePaperState which paper-reader.js '
        'references at runtime. Missing → every paper sub-mode crashes on open.'
    )
    assert 'paper/library.js' not in _BUNDLE_FILES, (
        'paper/library.js is a deferred paper leaf — it must NOT also be in the '
        'core _BUNDLE_FILES (double-load / duplicate-decl hazard).'
    )
    # library.js must precede paper-reader.js is NOT required (all cross-refs are
    # inside function bodies, runtime), but both must be in the SAME deferred
    # bundle so the concatenated window scope contains the definitions.
    assert 'paper-reader.js' in _DEFERRED_FILES


def test_paper_reader_cross_file_globals_are_bundled():
    """The structural guard for the 2026-07-13 outage class: every paper-library
    global that paper-reader.js REFERENCES but does NOT itself DEFINE must be
    DEFINED in some other file listed in _DEFERRED_FILES. Because the whole
    deferred list is concatenated into ONE window scope, "defined in a manifest
    file" is exactly the condition that keeps the reference from being a runtime
    ReferenceError.

    This catches the subdirectory blind spot Edge 1 misses: a paper leaf module
    extracted from paper-reader.js but forgotten from the manifest makes these
    globals undefined in the built bundle, and paper-reader.js throws on the
    first togglePaperMode → enterPaperMode call (taking down ALL paper modes)."""
    from lib.js_bundler import _DEFERRED_FILES

    reader_src = _read(os.path.join(JS_DIR, 'paper-reader.js'))

    def _defined_in(src, sym):
        return bool(
            re.search(r'\b(?:function|var|let|const)\s+' + re.escape(sym) + r'\b', src)
            or re.search(r'\basync\s+function\s+' + re.escape(sym) + r'\b', src)
            or re.search(r'\bwindow\.' + re.escape(sym) + r'\s*=', src)
        )

    # Sanity: the globals we guard are genuinely REFERENCED by paper-reader.js
    # (else the test is vacuous) and NOT defined there (they moved to library.js).
    for sym in _PAPER_LIBRARY_GLOBALS:
        assert re.search(r'\b' + re.escape(sym) + r'\b', reader_src), (
            f'{sym} is no longer referenced in paper-reader.js — update this '
            'guard to track the current paper cross-file globals.'
        )
        assert not _defined_in(reader_src, sym), (
            f'{sym} is now DEFINED in paper-reader.js again — the extraction '
            'was reverted; update this guard.'
        )

    # The real assertion: each referenced-but-not-locally-defined global must be
    # defined in SOME _DEFERRED_FILES source (the concatenated bundle scope).
    deferred_srcs = {
        name: _read(os.path.join(JS_DIR, name))
        for name in _DEFERRED_FILES
        if os.path.exists(os.path.join(JS_DIR, name))
    }
    undefined = []
    for sym in _PAPER_LIBRARY_GLOBALS:
        if not any(_defined_in(src, sym) for src in deferred_srcs.values()):
            undefined.append(sym)
    assert not undefined, (
        'paper-reader.js references these globals but NO file in _DEFERRED_FILES '
        f'defines them (the leaf module is missing from the manifest): {sorted(undefined)}. '
        'This is the 2026-07-13 outage: the built deferred bundle omits the file, '
        'the globals are undefined, and every paper sub-mode throws on open.'
    )


# ── Whitelist justification (self-documenting exemption) ──────────────────

def test_unbundled_whitelist_is_justified():
    """The one unbundled file (relay-admin.js) must be admin-only: a <script>
    tag in static/admin.html and NOT a <script> tag in index.html. This asserts
    the invariant that makes the exemption safe, so a genuinely-orphaned new
    file can't hide behind the whitelist."""
    index_html = _index_html()
    admin_html = _read(os.path.join(PROJECT_ROOT, 'static', 'admin.html'))
    for name in _UNBUNDLED_WHITELIST:
        # Present on the admin page…
        assert re.search(
            r'<script[^>]+src="static/js/' + re.escape(name) + r'[^"]*"', admin_html
        ), f'{name} is whitelisted as admin-only but has NO <script> tag in static/admin.html'
        # …and NOT a script tag in index.html (only a comment reference is OK).
        assert not re.search(
            r'<script[^>]+src="static/js/' + re.escape(name) + r'[^"]*"', index_html
        ), (
            f'{name} is whitelisted as admin-only but IS a <script> tag in '
            'index.html — it would be stripped-but-not-rebundled there.'
        )
