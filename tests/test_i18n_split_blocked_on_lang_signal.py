#!/usr/bin/env python3
"""The i18n split's gate: RESOLVED by owner decision (option A, lang cookie).

HISTORY — why this file exists and what changed
-----------------------------------------------
This started as the blocking analysis for Epic-E sub-part 1 step ③. It pinned a
contradiction between two facts that were both true at the time:

(A) The boot language MUST ship eagerly, present before ``_applyI18n()`` runs.
    index.html has 309 data-i18n bindings whose static fallback is not
    uniformly one language (~69% / ~88% CJK across the two channels), so a late
    dictionary flashes the wrong language at the DEFAULT user on every cold
    load. Still pinned by tests/test_i18n_pack_boot_sync.py.

(B) The server could not know which language to ship — the language lived only
    in ``localStorage['tofu_ui_lang']``.

An eagerly-shipped pack must be chosen at serve time (A) by a server with no
way to choose (B). Every escape route required a NEW server-visible language
signal, which is a contract change, so the choice was surfaced to the owner
rather than smuggled in.

**The owner chose option A: mirror the language into a cookie.** Half (B) is
therefore now deliberately FALSE, and the faces that asserted it have been
deleted (see the note below — they are not weakened, they are retired). The
positive replacement is tests/test_i18n_lang_cookie_signal.py.

WHAT THIS FILE STILL GUARDS
---------------------------
The three remaining faces are the ones that did NOT become obsolete:
  * the core bundle is still ONE artifact — i.e. slice 2 has not landed yet, so
    slice 1 provably cannot regress first paint;
  * half (A) is still pinned somewhere (a dependency check on the companion
    guard, so deleting it cannot silently remove the constraint);
  * the non-boot-language seam is still viable (setLanguage repaints in place;
    _i18n is a mutable `var`).

Run: python3 tests/test_i18n_split_blocked_on_lang_signal.py
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
I18N = os.path.join(REPO, 'static', 'js', 'i18n.js')
COMMON = os.path.join(REPO, 'routes', 'common.py')
BUNDLER = os.path.join(REPO, 'lib', 'js_bundler.py')

try:
    import pytest
except ImportError:
    pytest = None


def _unit(fn):
    return fn if pytest is None else pytest.mark.unit(fn)


def _read(p):
    with open(p, encoding='utf-8') as f:
        return f.read()


# ── RETIRED FACES (the gate they guarded is now deliberately OPEN) ───────
#
# Two faces used to live here:
#   test_language_lives_only_in_localStorage
#   test_server_never_reads_the_language_key
#   test_served_html_is_not_cached_per_language
# They asserted half (B): that the language was NOT server-visible. The owner
# chose option A (lang cookie), so that is now deliberately FALSE — i18n.js
# mirrors the language into a cookie and routes/common.py::request_ui_lang()
# reads it through a whitelist.
#
# They are DELETED rather than weakened. A guard whose premise has been
# consciously overturned is not "failing" — it has finished its job, and
# keeping a softened version would leave a test that asserts nothing. Their
# replacement is tests/test_i18n_lang_cookie_signal.py, which asserts the
# POSITIVE facts (whitelist holds, cache varies by language, client mirrors on
# boot and on change) plus the security boundary that matters now the value
# selects a filename.
#
# The three faces below are still load-bearing and stay.


@_unit
def test_bundle_excludes_i18n_only_when_packs_exist():
    """The atomic-fact rule, now that slice 2 HAS landed.

    The core bundle may exclude i18n.js ONLY when the pack set exists in the
    same build — otherwise t() is undefined for every module. Assert the
    bundler's source keeps that coupling: the conditional file list, and the
    state that is set from the SAME pack_map in the SAME place.
    """
    src = _read(BUNDLER)
    assert "if f != 'i18n.js'" in src and 'if pack_map else' in src, (
        'the conditional core-file list is gone — the bundle can now exclude '
        'i18n.js WITHOUT packs existing, which blanks t() app-wide')
    assert '_bundle_includes_i18n = not pack_map' in src, (
        '_bundle_includes_i18n no longer derives from the same pack_map — the '
        'bundle and the pack set can drift apart (the split-without-pack '
        'failure this epic exists to prevent)')


@_unit
def test_boot_sync_requirement_is_still_pinned_elsewhere():
    """Half (A): the companion guard must still exist and still assert it.

    If someone deletes that file, this analysis silently loses half its
    premise — so depend on it explicitly rather than restating its evidence.
    """
    companion = os.path.join(REPO, 'tests', 'test_i18n_pack_boot_sync.py')
    assert os.path.exists(companion), (
        'test_i18n_pack_boot_sync.py is gone — the boot-must-be-synchronous '
        'half of this contradiction is no longer pinned anywhere')
    body = _read(companion)
    assert 'i18n.js' in body and '_BUNDLE_FILES' in body, (
        'the companion guard no longer asserts eager-load ordering')


@_unit
def test_the_other_language_seam_remains_viable():
    """The half that IS unblocked, so a future implementer starts there.

    setLanguage() is user-initiated and repaints in place, so fetching the
    non-boot pack there costs nothing perceptible. That part of step ③ needs
    no new server input at all.
    """
    src = _read(I18N)
    m = re.search(r'function setLanguage\(lang\)\s*\{(.*?)\n\}', src, re.S)
    assert m, 'setLanguage not found'
    body = m.group(1)
    assert '_applyI18n()' in body and 'location.reload' not in body, (
        'setLanguage is no longer an in-place repaint — the on-demand pack '
        'fetch for the NON-boot language would need rework')
    assert re.search(r'^var _i18n = \{', src, re.M), (
        'the dictionary is no longer a mutable `var` — a fetched pack could '
        'not be merged in')


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
