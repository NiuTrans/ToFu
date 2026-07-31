#!/usr/bin/env python3
"""Why the i18n pack fetcher must be SYNCHRONOUS for the boot language.

CONTEXT
-------
Epic-E sub-part 1 wants single-language packs, measured at 7.6% of the
compressed first paint (tests/test_i18n_split_sizing.py). Step ① — making the
missing-translation fallback observable — landed in 4fbab4fa. Step ② is the
pack fetcher itself, and the obvious shape ("lazily fetch the language pack")
is WRONG for the boot language. This test pins why, so nobody implements the
obvious-but-broken version.

THE CONSTRAINT
--------------
``index.html`` ships 309 elements carrying data-i18n / data-i18n-title, and
their static fallback text is MIXED-LANGUAGE by construction:

    ~69% of element-text fallbacks are CJK; ~88% of title= fallbacks are CJK
    -- i.e. each channel carries a real minority of the OTHER language

Neither language renders correctly from the static HTML alone. It looks right only because
``_applyI18n()`` runs synchronously at boot with the dictionary already in
memory (i18n.js is first in _BUNDLE_FILES, "MUST be first — t() is used by all
other modules").

Make the boot pack async and the DEFAULT zh user gets a visible flash of
English across those elements on every cold load — a first-paint correctness
regression traded for a transfer win. Same bad trade that was refused in step
① when the zh fallback was kept rather than degrading to raw keys.

THE SHAPE THAT WORKS
--------------------
    boot language  → inlined/ordered so it is present BEFORE _applyI18n()
                     (still one language, so the 7.6% still lands)
    OTHER language → fetched on demand inside setLanguage(), which is already
                     a user-initiated action where a short await is invisible

So the saving comes from shipping ONE language eagerly, not from making the
boot language late.

Run: python3 tests/test_i18n_pack_boot_sync.py
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(REPO, 'index.html')
I18N = os.path.join(REPO, 'static', 'js', 'i18n.js')
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


@_unit
def test_static_markup_depends_on_i18n_for_a_correct_first_paint():
    """Quantifies the blast radius of a late dictionary."""
    html = _read(INDEX)
    n_text = len(re.findall(r'data-i18n="', html))
    n_title = len(re.findall(r'data-i18n-title="', html))
    assert n_text + n_title > 200, (
        f'only {n_text + n_title} i18n-bound elements found — if the markup '
        f'stopped depending on the dictionary, the sync-boot constraint below '
        f'may no longer apply; re-derive before relaxing it')


@_unit
def test_static_fallback_is_not_uniformly_one_language():
    """THE load-bearing fact.

    If the static fallback were uniformly zh, a late dictionary would flash
    only for en users (a minority, and arguably acceptable). MEASURED it is
    ~69% CJK in element text and ~88% CJK in title= attributes — i.e. a
    substantial English minority in the markup zh users see, and a Chinese
    majority in the markup en users see. Neither language renders correctly
    from the static HTML alone, so _applyI18n() cannot become late for EITHER.
    """
    html = _read(INDEX)
    cjk = re.compile(r'[\u4e00-\u9fff]')

    texts = re.findall(r'data-i18n="[^"]+"\s*>([^<]{1,60})', html)
    titles = re.findall(r'title="([^"]{1,40})"\s+data-i18n-title=', html)
    assert texts and titles, 'could not sample both fallback shapes'

    text_cjk = sum(1 for t in texts if cjk.search(t)) / len(texts)
    title_cjk = sum(1 for t in titles if cjk.search(t)) / len(titles)

    # Neither channel is uniform: each carries a real minority of the other
    # language. A single uniform channel would be a different (weaker) case.
    for label, ratio in (('element text', text_cjk), ('title attrs', title_cjk)):
        assert 0.05 < ratio < 0.98, (
            f'{label} fallbacks are {ratio:.0%} CJK — if this has become '
            f'uniformly one language, the "wrong for everyone" argument no '
            f'longer holds and the async pack design can be revisited')


@_unit
def test_i18n_loads_first_in_the_core_bundle():
    """The current guarantee that makes _applyI18n() synchronous."""
    src = _read(BUNDLER)
    m = re.search(r'_BUNDLE_FILES\s*=\s*\[(.*?)\n\]', src, re.S)
    assert m, 'could not locate _BUNDLE_FILES'
    entries = re.findall(r"^\s*'([^']+\.js)'", m.group(1), re.M)
    assert entries, 'parsed no entries from _BUNDLE_FILES'
    assert entries[0] == 'i18n.js', (
        f'i18n.js is no longer first in _BUNDLE_FILES (now {entries[0]!r}). '
        f'It is first precisely so t() is defined before every consumer; a '
        f'pack design must preserve that for the BOOT language.')


@_unit
def test_setLanguage_is_the_correct_seam_for_the_NON_boot_language():
    """Switching language is user-initiated, so an await there is invisible."""
    src = _read(I18N)
    m = re.search(r'function setLanguage\(lang\)\s*\{(.*?)\n\}', src, re.S)
    assert m, 'setLanguage not found'
    body = m.group(1)
    assert '_applyI18n()' in body, (
        'setLanguage must still re-apply translations; that call is the exact '
        'point a pack fetch would await before repainting')
    assert 'location.reload' not in body, (
        'setLanguage does an in-place repaint (no reload) — the property that '
        'lets a fetched pack be merged into the live _i18n object')


@_unit
def test_dictionary_object_is_mutable_so_a_fetched_pack_can_merge():
    """`var _i18n` (not const) is what makes runtime merging possible."""
    src = _read(I18N)
    assert re.search(r'^var _i18n = \{', src, re.M), (
        'the dictionary is no longer a mutable `var` binding — a fetched pack '
        'could not be merged into it and the pack design needs rework')


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
