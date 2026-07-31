#!/usr/bin/env python3
"""Guards for the i18n-pack branch of ``resolve_stale_bundle`` + pack cleanup.

WHAT WENT WRONG (2026-07-29, observed in production)
----------------------------------------------------
``_BUILT_BUNDLE_RE`` admits THREE kinds of built artifact — ``bundle-``,
``feature-`` and ``i18n-<lang>-`` — but ``resolve_stale_bundle`` classified
them TWO ways::

    if filename.startswith('bundle-'):  current = get_bundle_filename()
    else:                # 'feature-'   current = get_feature_bundle_filename()

so every stale i18n pack fell into the ``feature-`` branch. The server logged
the consequence verbatim::

    [StaleBundle] Self-healing stale request:
        i18n-zh-9e07255b.js -> feature-92a75489.js

That single mis-redirect produces the whole observed failure, because when
packs are active the core bundle EXCLUDES ``i18n.js`` (js_bundler.py:1306) —
the pack is the ONLY copy of ``_i18n`` / ``_i18nLang`` / ``t()``:

  * every string renders as its raw key (``collab.needsYou``);
  * the language falls back to index.html's static text, which is mostly
    English, so a zh user sees an English UI;
  * the feature bundle executes TWICE (once in the pack's slot, once in its
    own) → ``Identifier '_igGenerating' has already been declared`` → boot
    dies mid-flight and the loading spinner never resolves.

WHY IT WAS INVISIBLE: tests/test_stale_bundle_self_heal.py's
``test_resolver_is_pure_and_precise`` enumerates ``bundle-``, ``feature-``,
``core.js``, ``bundle-loader.js``, ``''`` and ``None`` — every kind EXCEPT a
pack. The regex was extended to admit packs without the resolver's branch or
its guard following.

The second defect, which MANUFACTURED the stale request: ``emit_pack_files``
deleted non-current packs with no age grace, while the bundle cleaner protects
young artifacts for ``_BUILT_ARTIFACT_GRACE_S`` (2h). A page served seconds
before a rebuild had its pack deleted out from under it.

Run: python3 tests/test_stale_i18n_pack_self_heal.py
"""

import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import pytest
except ImportError:
    pytest = None

from lib import js_bundler
from lib.i18n_packs import PACK_BASENAME_RE, emit_pack_files

pytestmark = [] if pytest is None else [pytest.mark.unit]


def _unit(fn):
    return fn if pytest is None else pytest.mark.unit(fn)


_FAKE_I18N = """
var _i18nLang = 'zh';
var _i18n = {
  'a.first': { zh: '第一', en: 'First' },
};
function t(key) { var e = _i18n[key]; return e ? (e[_i18nLang] || e.zh || key) : key; }
function setLanguage(lang) { _i18nLang = lang; _applyI18n(); }
function _applyI18n() { /* repaint */ }
function _reportMissingTranslation(key, lang) { /* tripwire */ }
"""


def _mkwork():
    d = tempfile.mkdtemp(prefix='i18n-stale-')
    src = os.path.join(d, 'i18n.js')
    with open(src, 'w', encoding='utf-8') as f:
        f.write(_FAKE_I18N)
    return d, src


def _bump(src):
    """Change the dictionary so the next emission yields DIFFERENT hashes.

    The new entry must go INSIDE the ``var _i18n = {...}`` literal: appending
    an assignment after it changes the runtime dict but not the located block,
    which build_full_pack_source correctly rejects as boundary drift.
    """
    text = open(src, encoding='utf-8').read()
    assert "'a.first'" in text
    open(src, 'w', encoding='utf-8').write(text.replace(
        "  'a.first': { zh: '第一', en: 'First' },",
        "  'a.first': { zh: '第一', en: 'First' },\n"
        "  'a.second': { zh: '第二', en: 'Second' },"))


class _Pinned:
    """Pin the module manifest that resolve_stale_bundle reads.

    resolve_stale_bundle calls get_bundle_filename() to keep the pointer and
    the pack map coherent; here we pin BOTH so the assertions are about the
    classification branch, not about whether a real bundle can be built.

    ★ TEARDOWN INVALIDATES, IT DOES NOT REPLAY A SNAPSHOT. The manifest
    globals are PUBLISHED as a side effect of building, so restoring a
    snapshot taken before a build stamps stale values over the real ones and
    silently convinces the module that packs are inactive for the rest of the
    process — poisoning every later test that asks for a pack tag. (That is
    what an earlier version of the sibling boot-floor test did, and it broke
    7 tests in test_i18n_pack_serving.py that neither file revealed alone.)
    The monkeypatched FUNCTIONS are safe to restore verbatim; the DATA is not,
    so it goes through reset_manifest_for_tests() and the next reader
    rebuilds.
    """

    def __init__(self, packs, feature='feature-92a75489.js',
                 core='bundle-265d9d7f.js'):
        self._packs, self._feature, self._core = packs, feature, core

    def __enter__(self):
        # Only the FUNCTIONS are snapshot-restorable (they are not build
        # side effects). The data globals are handled by the reset seam.
        self._saved_fns = (js_bundler.get_bundle_filename,
                           js_bundler.get_feature_bundle_filename)
        js_bundler._pack_filenames = self._packs
        js_bundler._feature_filename = self._feature
        js_bundler._bundle_filename = self._core
        js_bundler.get_bundle_filename = lambda: self._core
        js_bundler.get_feature_bundle_filename = lambda: self._feature
        return self

    def __exit__(self, *exc):
        (js_bundler.get_bundle_filename,
         js_bundler.get_feature_bundle_filename) = self._saved_fns
        js_bundler.reset_manifest_for_tests()
        return False


_CUR = {'zh': 'i18n-zh-4fe3959f.js', 'en': 'i18n-en-db74e770.js'}


@_unit
def test_stale_zh_pack_heals_to_the_current_zh_pack():
    """The exact production request. NOT to the feature bundle."""
    with _Pinned(_CUR):
        got = js_bundler.resolve_stale_bundle('i18n-zh-9e07255b.js')
    assert got == _CUR['zh'], (
        f'stale zh pack resolved to {got!r}. Anything other than the current '
        f'zh pack means the browser runs the WRONG FILE in the slot that is '
        f'the only copy of t()/_i18n/_i18nLang — the UI then renders raw i18n '
        f'keys and boot dies. This is the shipped bug verbatim.')


@_unit
def test_stale_pack_never_resolves_to_a_bundle_of_ANOTHER_KIND():
    """Pin the CONSEQUENCE, not just the happy value.

    A pack healing to a bundle/feature artifact is the defect class; assert on
    the kind so any future re-classification is caught even if the pack map
    changes shape.
    """
    with _Pinned(_CUR):
        for stale in ('i18n-zh-9e07255b.js', 'i18n-en-deadbeef.js'):
            got = js_bundler.resolve_stale_bundle(stale)
            assert got and PACK_BASENAME_RE.match(got), (
                f'{stale} healed to {got!r}, which is not an i18n pack at all')


@_unit
def test_language_is_preserved_across_the_heal():
    """A stale EN pack must not heal to the ZH pack.

    Same-kind is not enough: serving zh strings to a user whose page asked for
    en is silently wrong (t() finds every key, so no tripwire fires).
    """
    with _Pinned(_CUR):
        assert js_bundler.resolve_stale_bundle('i18n-en-deadbeef.js') == _CUR['en']
        assert js_bundler.resolve_stale_bundle('i18n-zh-deadbeef.js') == _CUR['zh']


@_unit
def test_current_pack_is_not_redirected():
    """Already-current → None, so it serves normally (no redirect loop)."""
    with _Pinned(_CUR):
        assert js_bundler.resolve_stale_bundle(_CUR['zh']) is None
        assert js_bundler.resolve_stale_bundle(_CUR['en']) is None


@_unit
def test_pack_request_404s_honestly_when_no_packs_are_published():
    """Dual-bundle fallback: i18n.js is IN the core bundle and no pack exists.

    Redirecting to some other artifact here would be a lie; a real 404 is
    correct (and the core bundle already carries the dictionary).
    """
    with _Pinned({}):
        assert js_bundler.resolve_stale_bundle('i18n-zh-9e07255b.js') is None


@_unit
def test_other_kinds_still_resolve_as_before():
    """Guard against fixing packs by breaking the two original kinds."""
    with _Pinned(_CUR):
        assert js_bundler.resolve_stale_bundle('bundle-95e8203d.js') == 'bundle-265d9d7f.js'
        assert js_bundler.resolve_stale_bundle('feature-5f582d2e.js') == 'feature-92a75489.js'
        assert js_bundler.resolve_stale_bundle('core.js') is None
        assert js_bundler.resolve_stale_bundle('bundle-loader.js') is None
        assert js_bundler.resolve_stale_bundle('') is None
        assert js_bundler.resolve_stale_bundle(None) is None


@_unit
def test_young_stale_pack_survives_a_rebuild():
    """The defect that MANUFACTURED the stale request.

    A page served seconds ago references pack A. A rebuild renames it to B.
    Deleting A immediately 404s the in-flight page — and a missing pack has no
    error path, because the core bundle excludes i18n.js.
    """
    d, src = _mkwork()
    try:
        first = emit_pack_files(d, source_path=src)
        _bump(src)
        second = emit_pack_files(d, source_path=src)
        assert second != first, 'source change did not change the hashes'
        for lang, name in first.items():
            assert os.path.exists(os.path.join(d, name)), (
                f'a JUST-PUBLISHED {lang} pack ({name}) was deleted by the '
                f'next rebuild. Any page served in that window loses t() '
                f'entirely and renders raw i18n keys.')
    finally:
        shutil.rmtree(d, ignore_errors=True)


@_unit
def test_genuinely_old_pack_is_still_reclaimed():
    """The grace must not become a leak — an aged pack is still deleted."""
    d, src = _mkwork()
    try:
        first = emit_pack_files(d, source_path=src)
        old = time.time() - (js_bundler._BUILT_ARTIFACT_GRACE_S + 600)
        for name in first.values():
            os.utime(os.path.join(d, name), (old, old))
        _bump(src)
        emit_pack_files(d, source_path=src)
        for lang, name in first.items():
            assert not os.path.exists(os.path.join(d, name)), (
                f'aged {lang} pack {name} was never reclaimed — the grace '
                f'window turned cleanup off instead of delaying it')
    finally:
        shutil.rmtree(d, ignore_errors=True)


@_unit
def test_pack_grace_matches_the_bundle_cleaner():
    """Both cleaners must honour ONE window, or an artifact protected by one
    is deleted by the other and the pairing breaks anyway."""
    from lib.i18n_packs import _ARTIFACT_GRACE_S
    assert _ARTIFACT_GRACE_S == js_bundler._BUILT_ARTIFACT_GRACE_S, (
        'pack cleanup and bundle cleanup disagree on the grace window')


if __name__ == '__main__':
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print('ok  ', name)
            except AssertionError as e:
                failures += 1
                print('FAIL', name)
                print('     ', e)
    print('ALL PASSED' if not failures else f'{failures} FAILED')
    sys.exit(1 if failures else 0)
