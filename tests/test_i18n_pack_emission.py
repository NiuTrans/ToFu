#!/usr/bin/env python3
"""Guards for lib/i18n_packs.emit_pack_files — the pack EMISSION layer.

SCOPE
-----
tests/test_i18n_packs.py proves extraction + rendering are exact. This file
proves the PUBLISH step is safe:

  * artifacts are content-hashed and deterministic (same source -> same name);
  * a pack on disk ALWAYS passed the roundtrip gate first — nothing is
    published unverified (a silently-shrinking pack renders the wrong language
    with no error; the gate is the only place that can catch it);
  * publish is atomic (temp + os.replace) and failure-safe (a failed emission
    publishes NOTHING and leaves the last-good set servable);
  * stale artifacts are cleaned, but only after the current set is on disk.

These run against a TEMP directory with a small synthetic i18n.js, so they
are fast and never touch the real static/js.

Run: python3 tests/test_i18n_pack_emission.py
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import pytest
except ImportError:
    pytest = None

from lib.i18n_packs import (
    PACK_BASENAME_RE,
    PackExtractionError,
    emit_pack_files,
    verify_pack_roundtrip,
    extract_dictionary,
    _ARTIFACT_GRACE_S,
)


def _unit(fn):
    return fn if pytest is None else pytest.mark.unit(fn)


_FAKE_I18N = """
var _i18nLang = 'zh';
var _i18n = {
  'a.first': { zh: '第一', en: 'First' },
  'a.second': { zh: '带\\'引号\\'的 {n} 占位', en: 'with \\'quotes\\' and {n}' },
  'a.third': { zh: '第三', en: 'Third' },
};
function t(key) { var e = _i18n[key]; return e ? (e[_i18nLang] || e.zh || key) : key; }
function setLanguage(lang) { _i18nLang = lang; _applyI18n(); }
function _applyI18n() { /* repaint */ }
function _reportMissingTranslation(key, lang) { /* tripwire */ }
"""


def _mkwork():
    d = tempfile.mkdtemp(prefix='i18n-emit-')
    src = os.path.join(d, 'i18n.js')
    with open(src, 'w', encoding='utf-8') as f:
        f.write(_FAKE_I18N)
    return d, src


@_unit
def test_emits_both_languages_with_content_hashed_names():
    d, src = _mkwork()
    try:
        out = emit_pack_files(d, source_path=src)
        assert set(out) == {'zh', 'en'}
        for lang, name in out.items():
            assert PACK_BASENAME_RE.match(name), (
                f'{name} does not match the artifact pattern — the parity '
                f'test\'s disk-orphan edge would treat it as a source file')
            assert f'-{lang}-' in name
            assert os.path.exists(os.path.join(d, name))
    finally:
        shutil.rmtree(d, ignore_errors=True)


@_unit
def test_deterministic_same_source_same_filename():
    d, src = _mkwork()
    try:
        a = emit_pack_files(d, source_path=src)
        b = emit_pack_files(d, source_path=src)
        assert a == b, (
            'same source produced different artifact names — the content hash '
            'is not stable, so caches keyed on the filename would churn')
    finally:
        shutil.rmtree(d, ignore_errors=True)


@_unit
def test_every_emitted_file_passes_the_roundtrip_gate():
    """The whole point: a pack on disk is PROVEN lossless, not hoped so."""
    d, src = _mkwork()
    try:
        dictionary = extract_dictionary(src)
        out = emit_pack_files(d, source_path=src)
        for lang, name in out.items():
            text = open(os.path.join(d, name), encoding='utf-8').read()
            r = verify_pack_roundtrip(dictionary, text, lang)
            assert not r['missing'] and not r['mismatched'] and not r['extra'], (
                f'{lang} emitted pack is not an exact reproduction: {r}')
    finally:
        shutil.rmtree(d, ignore_errors=True)


@_unit
def test_stale_artifacts_are_removed_but_current_kept():
    d, src = _mkwork()
    try:
        stale = os.path.join(d, 'i18n-zh-deadbeef.js')
        with open(stale, 'w') as f:
            f.write('var _i18n = {};')
        # AGE it past the grace window. Cleanup is deliberately two-condition
        # — "not current" AND "not young" — because a pack is referenced by an
        # already-served index.html and deleting a JUST-published one 404s the
        # page currently loading (which has no error path: the core bundle
        # excludes i18n.js, so t() simply doesn't exist). Writing the file and
        # immediately expecting it gone would assert the young case, i.e. the
        # exact behaviour tests/test_stale_i18n_pack_self_heal.py forbids.
        old = time.time() - (_ARTIFACT_GRACE_S + 600)
        os.utime(stale, (old, old))
        out = emit_pack_files(d, source_path=src)
        assert not os.path.exists(stale), 'aged stale pack was not cleaned'
        for name in out.values():
            assert os.path.exists(os.path.join(d, name)), (
                'cleanup deleted a CURRENT pack — that is the dangerous '
                'direction (a servable artifact vanishing)')
    finally:
        shutil.rmtree(d, ignore_errors=True)


@_unit
def test_failed_emission_publishes_nothing_and_keeps_last_good():
    """A broken source must NOT leave a partial pack set.

    Sequence: emit a GOOD set (simulating last-good in production), then break
    the source and emit again. The second call must raise AND the good files
    must still be there — the server can keep serving them.
    """
    d, src = _mkwork()
    try:
        good = emit_pack_files(d, source_path=src)
        with open(src, 'w', encoding='utf-8') as f:
            f.write('var _i18n = { broken syntax !!!')   # unparseable
        try:
            emit_pack_files(d, source_path=src)
        except PackExtractionError:
            pass
        else:
            raise AssertionError('a broken source did not raise')
        for name in good.values():
            assert os.path.exists(os.path.join(d, name)), (
                'a failed emission deleted the last-good packs — production '
                'would lose its servable set exactly when the source broke')
    finally:
        shutil.rmtree(d, ignore_errors=True)


@_unit
def test_emitted_files_are_syntactically_valid_js():
    if not shutil.which('node'):
        print('SKIP (node unavailable)')
        return
    d, src = _mkwork()
    try:
        out = emit_pack_files(d, source_path=src)
        for lang, name in out.items():
            r = subprocess.run([shutil.which('node'), '--check',
                                os.path.join(d, name)],
                               capture_output=True, text=True, timeout=60)
            assert r.returncode == 0, f'{lang} pack invalid JS: {r.stderr[:300]}'
    finally:
        shutil.rmtree(d, ignore_errors=True)


@_unit
def test_emitted_pack_is_a_full_replacement_with_functions():
    """The pack is a drop-in i18n.js replacement — functions must survive."""
    d, src = _mkwork()
    try:
        out = emit_pack_files(d, source_path=src)
        zh = open(os.path.join(d, out['zh']), encoding='utf-8').read()
        for fn in ('function t(', 'function setLanguage(',
                   'function _applyI18n(', '_reportMissingTranslation'):
            assert fn in zh, f'emitted pack lost {fn!r}'
        # And the dictionary is single-language: the en TEXT must be entirely
        # absent from the zh pack (not just the en member shape).
        assert 'First' not in zh, 'en text leaked into the zh pack'
    finally:
        shutil.rmtree(d, ignore_errors=True)


@_unit
def test_quotes_and_placeholders_survive_intact():
    """The synthetic source has escaped quotes + an {n} placeholder — the exact
    content that breaks a regex-based extractor. Pin that they survive."""
    d, src = _mkwork()
    try:
        out = emit_pack_files(d, source_path=src)
        en = open(os.path.join(d, out['en']), encoding='utf-8').read()
        assert "with 'quotes' and {n}" in en, (
            'escaped quotes or the {n} placeholder were mangled — this is the '
            'content class a regex extractor silently corrupts')
    finally:
        shutil.rmtree(d, ignore_errors=True)


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
