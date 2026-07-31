#!/usr/bin/env python3
"""Guards for lib/i18n_packs.py — the atom slice 2 is built from.

WHAT IS BEING PROVEN
--------------------
Slice 2 will serve a SINGLE-LANGUAGE dictionary instead of the dual one. The
failure mode of getting that wrong is invisible: a dropped key does not throw,
because ``t()`` falls back to zh — so an English UI silently renders Chinese.
That is the same no-failure-signal defect class this epic already fixed once
at the ``t()`` level (4fbab4fa), and it must not be reintroduced underneath.

So before ANY pack is served, extraction must be proven EXACT, key by key.
These tests are that proof.

WHY THE IMPLEMENTATION EXECUTES JS INSTEAD OF PARSING IT
--------------------------------------------------------
A regex over ``{ zh: '…', en: '…' }`` looks sufficient and is not: the
dictionary holds apostrophes, escaped quotes, ``{n}`` placeholders and 378
comment lines. A regex that handles 99% of entries silently loses the rest.
``extract_dictionary`` runs the real file under node and reads the resulting
object, so the extractor cannot disagree with the runtime. The
``test_extraction_beats_a_naive_regex`` face makes that concrete rather than
rhetorical.

MEASURED (2026-07-26, brotli q9):
    dual i18n.js  348.1 KB raw -> 91.4 KB
    zh pack       178.7 KB raw -> 49.1 KB
    en pack       180.5 KB raw -> 47.7 KB
    saving                        42.3 KB compressed

That is MORE than the 30.6 KB the sizing test estimated, because generating a
pack also drops the comments and indentation that a naive ``en:``-strip keeps.

Run: python3 tests/test_i18n_packs.py
"""

import os
import re
import shutil
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

from lib.i18n_packs import (
    SUPPORTED_LANGS,
    build_pack_source,
    extract_dictionary,
    verify_pack_roundtrip,
)


def _unit(fn):
    return fn if pytest is None else pytest.mark.unit(fn)


def _have_node():
    return shutil.which('node') is not None


_CACHE = {}


def _dict():
    if 'd' not in _CACHE:
        _CACHE['d'] = extract_dictionary()
    return _CACHE['d']


# ── The load-bearing face ────────────────────────────────────────────────

@_unit
def test_every_key_survives_the_roundtrip_in_both_languages():
    """THE gate. A single dropped key = silent wrong-language rendering.

    Asserts exact equality per key, not a count — a pack could have the right
    total while holding a wrong string.
    """
    if not _have_node():
        print('SKIP (node unavailable)')
        return
    d = _dict()
    assert len(d) > 2000, f'only {len(d)} keys extracted — extraction is broken'

    for lang in SUPPORTED_LANGS:
        src = build_pack_source(d, lang)
        r = verify_pack_roundtrip(d, src, lang)
        assert not r['missing'], (
            f'{lang}: {len(r["missing"])} key(s) LOST, e.g. {r["missing"][:5]} — '
            f'each renders the wrong language with no error')
        assert not r['mismatched'], (
            f'{lang}: {len(r["mismatched"])} value(s) ALTERED, e.g. '
            f'{r["mismatched"][:5]}')
        assert not r['extra'], (
            f'{lang}: {len(r["extra"])} key(s) invented, e.g. {r["extra"][:5]}')
        assert r['keys'] > 2000


@_unit
def test_pack_preserves_the_entry_shape_t_depends_on():
    """``t()`` reads ``entry[_i18nLang]``. Flattening to {key: text} would
    silently make every lookup undefined -> fall back to zh -> the same
    invisible failure. Pin the shape."""
    if not _have_node():
        print('SKIP (node unavailable)')
        return
    src = build_pack_source(_dict(), 'en')
    assert re.search(r'\{en:"', src), (
        'entries are no longer {lang: text} objects — t()\'s entry[_i18nLang] '
        'lookup would return undefined for every key')


@_unit
def test_missing_language_is_omitted_not_filled_from_the_other():
    """Filling a gap from the other language is the tempting wrong fix.

    It makes the pack look complete while showing the wrong language, and it
    defeats the runtime tripwire that exists to report exactly this.
    """
    fake = {
        'both.ok': {'zh': '中', 'en': 'EN'},
        'only.zh': {'zh': '只有中文'},
    }
    en_src = build_pack_source(fake, 'en')
    assert 'only.zh' not in en_src, (
        'a key lacking `en` was emitted into the en pack — it would render '
        'Chinese in an English UI, which is what the tripwire must catch')
    assert 'both.ok' in en_src


# ── Why not a regex ──────────────────────────────────────────────────────

@_unit
def test_extraction_beats_a_naive_regex():
    """Concrete evidence that executing JS is not over-engineering.

    Counts entries a simple `'key': { zh: '…', en: '…' }` regex can see, and
    asserts the executed extraction sees MORE. If they ever match, the
    dictionary got simple enough that this justification should be revisited —
    the test says so rather than silently passing.
    """
    if not _have_node():
        print('SKIP (node unavailable)')
        return
    src = open(I18N, encoding='utf-8').read()
    naive = re.findall(
        r"^\s*'([^']+)'\s*:\s*\{\s*zh\s*:\s*'(?:[^'\\]|\\.)*'\s*,\s*en\s*:\s*'(?:[^'\\]|\\.)*'\s*\}",
        src, re.M)
    real = _dict()
    assert len(real) >= len(naive), 'executed extraction should never see fewer'
    assert len(real) > len(naive), (
        f'the naive regex now sees all {len(naive)} entries — the dictionary '
        f'may have become regex-tractable; re-justify the node dependency '
        f'before simplifying, and check for quote/brace edge cases first')


# ── The prize, measured on shipped bytes ─────────────────────────────────

@_unit
def test_single_language_pack_is_materially_smaller_compressed():
    if not _have_node():
        print('SKIP (node unavailable)')
        return
    if brotli is None:
        print('SKIP (brotli unavailable)')
        return
    dual = open(I18N, 'rb').read()
    dual_c = len(brotli.compress(dual, quality=9))
    zh_c = len(brotli.compress(build_pack_source(_dict(), 'zh').encode(), quality=9))
    saved = (dual_c - zh_c) / 1024
    assert saved > 25.0, (
        f'zh pack saves only {saved:.1f} KB compressed — measured 42.3 KB on '
        f'2026-07-26; a large drop means the dictionary or the generator '
        f'changed, so re-measure before relying on the number')
    print(f'zh pack saves {saved:.1f} KB compressed '
          f'({dual_c/1024:.1f} -> {zh_c/1024:.1f} KB)')


@_unit
def test_generated_pack_is_syntactically_valid_js():
    """A pack that does not parse would blank the app. node --check it."""
    if not _have_node():
        print('SKIP (node unavailable)')
        return
    import subprocess
    import tempfile
    for lang in SUPPORTED_LANGS:
        src = build_pack_source(_dict(), lang)
        with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False,
                                         encoding='utf-8') as fh:
            fh.write(src)
            path = fh.name
        try:
            r = subprocess.run([shutil.which('node'), '--check', path],
                               capture_output=True, text=True, timeout=60)
            assert r.returncode == 0, f'{lang} pack is invalid JS: {r.stderr[:400]}'
        finally:
            os.unlink(path)


@_unit
def test_unsupported_language_is_refused():
    """The lang reaches a filename in slice 2 — never emit for an unknown one."""
    from lib.i18n_packs import PackExtractionError
    try:
        build_pack_source({'k': {'zh': 'x'}}, '../../etc/passwd')
    except PackExtractionError:
        return
    raise AssertionError('an unsupported language was accepted')


if __name__ == '__main__':
    if not _have_node():
        print('SKIP: node not available')
        sys.exit(0)
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
