#!/usr/bin/env python3
"""Guards for the harness symbol resolver's SCAN SURFACE.

WHY THIS EXISTS
---------------
`tests/_conv_bundle_sources.py` is the single source of truth 13+ node/jsdom
harnesses use to answer "which shipped JS files must I eval to get symbol X".
It originally read ONLY `lib.js_bundler._BUNDLE_FILES` (131 files) and was
blind to `_DEFERRED_FILES` (21 files) — the whole `paper/*`, `project-brain*`,
`orchestration*`, `image-gen*`, `task-mode` tree.

That is not a cosmetic gap. A miss falls into the "not defined by any bundled
file" branch, whose message says **"the implementation was REMOVED. This is a
product regression"**. Measured 2026-07-28 on files that were on disk AND
shipping to users:

    _activeReviewLang (paper/report.js)   -> "the implementation was REMOVED"
    _loadPaperLibrary (paper/library.js)  -> "the implementation was REMOVED"
    _refreshAttention (project-brain.js)  -> "the implementation was REMOVED"

A precisely-worded, authoritative, WRONG diagnosis that sends the next reader
to restore code that never left — the charter's "false attribution" failure
mode, this time inside the tool meant to prevent guard rot.

WHAT THESE GUARDS ASSERT
------------------------
The RESULT (a symbol living in either bundle resolves to its real file), never
a constant or a file count. Re-shuffling a module between the two manifests,
renaming one, or adding a third bundle keeps them green as long as symbols
still resolve — while re-narrowing the scan surface goes red immediately.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest \
     tests/test_conv_bundle_sources_scan_surface.py
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _conv_bundle_sources as B  # noqa: E402

pytestmark = pytest.mark.unit


def _manifests():
    from lib.js_bundler import _BUNDLE_FILES, _DEFERRED_FILES
    return list(_BUNDLE_FILES), list(_DEFERRED_FILES)


def test_scan_surface_report():
    """Print what the resolver actually scans BEFORE asserting anything.

    Charter discipline for scanning guards: a scan-surface bug does not
    announce itself — the input set never errors, it just silently omits. So
    the sample is reported first and compared against the production
    manifests, rather than inferred from a green assertion.
    """
    core, deferred = _manifests()
    scanned = B.bundle_files()
    print(f'\ncore manifest     : {len(core)} files')
    print(f'deferred manifest : {len(deferred)} files')
    print(f'resolver scans    : {len(scanned)} files')
    missing = [f for f in core + deferred if f not in scanned]
    print(f'omitted           : {missing or "none"}')
    assert not missing, (
        f'the resolver does not scan {len(missing)} shipped file(s): {missing[:8]} '
        f'— every file the bundler ships MUST be searchable, or a symbol living '
        f'there is misreported as a deleted implementation.')


def test_a_deferred_bundle_symbol_resolves_to_its_real_file():
    """THE regression this file exists for.

    Each of these ships in the DEFERRED bundle and is reachable by users.
    Before the fix every one raised "the implementation was REMOVED".
    """
    cases = {
        '_activeReviewLang': 'paper/report.js',
        '_loadPaperLibrary': 'paper/library.js',
        '_refreshAttention': 'project-brain.js',
    }
    for sym, expected in cases.items():
        paths = B.sources_defining(sym)
        rels = [os.path.relpath(p, B.JS_DIR).replace(os.sep, '/') for p in paths]
        assert expected in rels, (
            f'{sym} should resolve to the deferred file {expected}, got {rels}')


def test_a_core_bundle_symbol_still_resolves():
    """Complement: widening the surface must not break core-bundle lookups.

    Without this, "scan nothing but deferred" would also satisfy the guard
    above — a scan surface can be wrong in either direction.
    """
    for sym, expected in {'_safeJsonParse': 'core.js',
                          '_trimMsgForPersist': 'core/conv_persist_helpers.js'}.items():
        rels = [os.path.relpath(p, B.JS_DIR).replace(os.sep, '/')
                for p in B.sources_defining(sym)]
        assert expected in rels, f'{sym} should resolve to {expected}, got {rels}'


def test_core_is_searched_before_deferred():
    """Execution order, not alphabetical order.

    `feature-loader.js` lives in the CORE bundle and injects the feature
    bundle on demand, so core always executes first. A harness that evals the
    returned files in order must therefore see core symbols defined before a
    deferred file references them.
    """
    core, deferred = _manifests()
    scanned = B.bundle_files()
    assert core and deferred, 'both manifests must be non-empty for this to mean anything'
    last_core = max(scanned.index(f) for f in core if f in scanned)
    first_deferred = min(scanned.index(f) for f in deferred if f in scanned)
    assert last_core < first_deferred, (
        'deferred files are ordered before core files — a harness evaluating '
        'them in this order would hit a ReferenceError on any core symbol a '
        'deferred module uses at load time.')


def test_unbundled_file_is_reported_as_a_manifest_bug_not_a_deletion(tmp_path):
    """The 4th state must NOT reuse the "implementation was REMOVED" wording.

    A file present on disk but absent from both manifests is a REAL problem —
    no user can reach that code — but it is a different problem with a
    different fix (edit the manifest, not the source). Conflating it with a
    deletion is exactly the false attribution this module was hardened against.
    """
    probe = os.path.join(B.JS_DIR, '__scan_surface_probe.js')
    with open(probe, 'w', encoding='utf-8') as fh:
        fh.write('function __scanSurfaceProbeSym__() { return 1; }\n')
    try:
        with pytest.raises(AssertionError) as ei:
            B.sources_defining('__scanSurfaceProbeSym__')
        msg = str(ei.value)
    finally:
        os.remove(probe)

    assert 'REMOVED' not in msg, (
        'an unbundled-but-present file was reported as a deleted implementation '
        f'— the two states share a message again:\n{msg}')
    assert 'never served' in msg and 'manifest' in msg, (
        f'the unbundled diagnosis must say the code ships to nobody and point at '
        f'the manifest; got:\n{msg}')


def test_a_genuinely_absent_symbol_is_still_reported_as_a_regression():
    """Complement to the test above: widening must not make MISSES silent.

    If every miss were re-labelled "manifest bug", a real deletion would stop
    being reported as a product regression.
    """
    with pytest.raises(AssertionError) as ei:
        B.sources_defining('__no_such_symbol_anywhere_xyz__')
    assert 'REMOVED' in str(ei.value)
