#!/usr/bin/env python3
"""i18n single-language split — MEASURED sizing, so the number stops drifting.

WHY THIS FILE EXISTS
--------------------
Across four passes this one proposal was sized at 19% → 10% → 5% → "noise",
and a 160 KB "key-name overhead" line item was asserted that does not exist.
Every wrong figure came from the same move: **deriving a component by
subtracting from an aggregate, on UNCOMPRESSED source bytes.**

This test replaces opinion with a reproducible measurement. It builds a real
zh-only variant of ``static/js/i18n.js`` by dropping the ``en:`` member from
every ``{zh:…, en:…}`` entry, compresses both with the SAME codec+quality the
server actually ships (``server.py`` → brotli q9 for content-addressed
immutable assets), and asserts the saving is materially larger than noise.

MEASURED (2026-07-26, brotli q9):
    i18n.js dual   344.0 KB raw ->  89.8 KB compressed
    i18n.js zh-only 232.8 KB raw ->  59.2 KB compressed
    saving                            30.6 KB compressed
    first-paint bundle             402.3 KB compressed
    => 7.6% of the shipped first-paint payload

7.6% is NOT noise. The earlier "not worth doing" verdict was wrong, and this
test exists so the next session inherits the measurement rather than the
opinion.

WHAT THIS TEST DOES *NOT* CLAIM
-------------------------------
It sizes the prize only. It does not assert the split is safe to ship — that
needs the ``t()`` fallback redesign (``entry[_i18nLang] || entry.zh``, i18n.js:
3398) so a missing key surfaces instead of silently rendering Chinese in an
English UI. That is a real design cost and is why this stays a proposal with a
number attached, not a green light.

Run: python3 tests/test_i18n_split_sizing.py
"""

import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
I18N = os.path.join(REPO, 'static', 'js', 'i18n.js')

try:
    import pytest
except ImportError:
    pytest = None

try:
    import brotli
except ImportError:
    brotli = None


def _unit(fn):
    return fn if pytest is None else pytest.mark.unit(fn)


# Matches a full {zh:'…', en:'…'} entry and keeps only the zh member.
_ENTRY = re.compile(
    r"(\{\s*zh\s*:\s*(['\"])(?:\\.|(?!\2).)*\2)\s*,\s*en\s*:\s*(['\"])(?:\\.|(?!\3).)*\3(\s*\})",
    re.S)


def _dual_and_zh_only():
    src = open(I18N, encoding='utf-8').read()
    zh_only, n = _ENTRY.subn(r"\1\4", src)
    return src, zh_only, n


def _br(data, quality=9):
    return len(brotli.compress(data, quality=quality))


@_unit
def test_zh_only_variant_covers_essentially_every_entry():
    """The rewrite must actually strip ~all entries, else the sizing is bogus."""
    src, zh_only, n = _dual_and_zh_only()
    total_keys = len(re.findall(r"^\s*'[^']+'\s*:\s*\{", src, re.M))
    assert total_keys > 2000, f'unexpected dictionary shape ({total_keys} keys)'
    coverage = n / total_keys
    assert coverage > 0.97, (
        f'only {n}/{total_keys} ({coverage:.1%}) entries stripped — the sizing '
        f'below would understate the saving; check for entries whose text '
        f'contains quotes the regex cannot span')


@_unit
def test_saving_is_measured_on_COMPRESSED_bytes_not_source_bytes():
    """The guardrail against the mistake that produced 19%/10%/5%.

    Raw source bytes systematically overstate: comments, indentation and the
    repeated {zh:..,en:..} structure are exactly what brotli eats. The only
    figure that means anything is the compressed delta.
    """
    if brotli is None:
        print('SKIP (brotli unavailable)')
        return
    src, zh_only, _ = _dual_and_zh_only()
    raw_saving = (len(src.encode()) - len(zh_only.encode())) / 1024
    comp_saving = (_br(src.encode()) - _br(zh_only.encode())) / 1024

    assert raw_saving > comp_saving, (
        'sanity: compression must shrink the apparent saving')
    # Raw overstates by ~3.6x. Pin the ratio loosely so the lesson survives
    # dictionary growth but a regression in reasoning is still visible.
    assert raw_saving / comp_saving > 2.0, (
        f'raw {raw_saving:.1f} KB vs compressed {comp_saving:.1f} KB — if these '
        f'converge, re-derive; the whole point is that raw bytes mislead')


@_unit
def test_compressed_saving_is_material_not_noise():
    """The verdict this file overturns: 'benefit is noise-level'. It is not."""
    if brotli is None:
        print('SKIP (brotli unavailable)')
        return
    src, zh_only, _ = _dual_and_zh_only()
    comp_saving = (_br(src.encode()) - _br(zh_only.encode())) / 1024
    assert comp_saving > 20.0, (
        f'compressed saving {comp_saving:.1f} KB — measured 30.6 KB on '
        f'2026-07-26; a large drop means the dictionary shrank or the split '
        f'stopped paying, so re-run the sizing before acting on it')


@_unit
def test_saving_as_share_of_the_shipped_first_paint_payload():
    """Express the prize against what the browser ACTUALLY downloads."""
    if brotli is None:
        print('SKIP (brotli unavailable)')
        return
    bundles = sorted(glob.glob(os.path.join(REPO, 'static', 'js', 'bundle-*.js')),
                     key=os.path.getmtime)
    if not bundles:
        print('SKIP (no built bundle present)')
        return
    src, zh_only, _ = _dual_and_zh_only()
    comp_saving = _br(src.encode()) - _br(zh_only.encode())
    bundle_comp = _br(open(bundles[-1], 'rb').read())
    share = comp_saving / bundle_comp
    assert share > 0.04, (
        f'i18n split is only {share:.1%} of the compressed first-paint payload; '
        f'measured 7.6% on 2026-07-26')
    print(f'i18n zh-only saves {comp_saving/1024:.1f} KB of '
          f'{bundle_comp/1024:.1f} KB compressed first paint = {share:.1%}')


@_unit
def test_the_fallback_that_makes_the_split_unsafe_is_still_there():
    """Pins the REAL blocker so it is not mistaken for a sizing problem.

    ``t()`` returns ``entry[_i18nLang] || entry.zh || key``. Ship English-only
    and every missing key renders Chinese with no error — a defect class with
    no failure signal. Any split MUST redesign this first.
    """
    src = open(I18N, encoding='utf-8').read()
    assert 'entry[_i18nLang] || entry.zh' in src, (
        'the silent-zh-fallback changed; re-assess whether a single-language '
        'pack can now fail loudly (that would remove the main objection)')


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
