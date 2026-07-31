#!/usr/bin/env python3
"""i18n missing-translation tripwire — the precondition for language packs.

WHY THIS EXISTS
---------------
``t()`` used to resolve as ``entry[_i18nLang] || entry.zh || key``. Today every
key ships both languages, so the ``|| entry.zh`` arm is unreachable and the
expression looks harmless. It is not: the moment a single-language pack ships
(Epic-E sub-part 1, measured worth 7.6% of the compressed first paint — see
tests/test_i18n_split_sizing.py) that arm becomes reachable for EVERY key the
pack omits, and an English UI quietly fills with Chinese.

That is a defect class with **no failure signal**: nothing throws, nothing
logs, no test can see it, and a user who does not read Chinese cannot report
what they cannot recognise as wrong. It is the same family as the ``_serverRev``
drift dimension diagnosed elsewhere in this project — a signal that mixes two
writers and therefore cannot distinguish "fine" from "broken".

THE FIX BEING PINNED
--------------------
The fallback still HAPPENS (never regress the UI to a raw key string), but it
is now REPORTED once per (key, lang). "Silently wrong" becomes "wrong and
traceable", which is what makes shipping language packs safe at all.

These probes drive the REAL shipped ``t()`` under node — not a reimplementation.

Run: python3 tests/test_i18n_missing_tripwire.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
I18N = os.path.join(REPO, 'static', 'js', 'i18n.js')

try:
    import pytest
except ImportError:
    pytest = None


def _unit(fn):
    return fn if pytest is None else pytest.mark.unit(fn)


def _have_node():
    return shutil.which('node') is not None


def _drive(body, lang='en', neuter=False):
    """Load the real i18n.js under a minimal DOM stub and run *body*."""
    src = open(I18N, encoding='utf-8').read()
    if neuter:
        # Restore the pre-fix silent expression.
        needle = '    _reportMissingTranslation(key, _i18nLang);\n'
        assert needle in src, (
            'NEUTER anchor missing — the tripwire call was reworded; update '
            'this test so it keeps proving the report is load-bearing')
        src = src.replace(needle, '')

    harness = f"""
globalThis.window = globalThis;
globalThis.localStorage = {{ getItem: () => {json.dumps(lang)}, setItem: () => {{}} }};
globalThis.document = {{ documentElement: {{}}, querySelectorAll: () => [],
                        addEventListener: () => {{}}, readyState: 'complete' }};
const __warns = [];
console.warn = (...a) => __warns.push(a.join(' '));
{src}
const __out = (() => {{ {body} }})();
console.log('@@' + JSON.stringify(__out));
"""
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False,
                                     encoding='utf-8') as fh:
        fh.write(harness)
        path = fh.name
    try:
        r = subprocess.run([shutil.which('node'), path],
                           capture_output=True, text=True, timeout=90)
        if r.returncode != 0:
            raise AssertionError(f'node failed: {r.stderr[:700]}')
        line = [l for l in r.stdout.splitlines() if l.startswith('@@')][-1]
        return json.loads(line[2:])
    finally:
        os.unlink(path)


# ── Face 1: the UI must NOT change ───────────────────────────────────────

@_unit
def test_fallback_still_renders_chinese_not_a_raw_key():
    """Observability must not cost correctness.

    Making the miss loud is worthless if it also degrades the UI to a raw key
    string — that would be a visible regression traded for a log line.
    """
    if not _have_node():
        print('SKIP (node unavailable)')
        return
    out = _drive("""
        _i18n['probe.zhOnly'] = { zh: '中文文案' };
        return { text: t('probe.zhOnly'), warns: __warns.length };
    """)
    assert out['text'] == '中文文案', (
        'the zh fallback must still render — the tripwire reports, it does not '
        'change what the user sees')
    assert out['warns'] == 1


@_unit
def test_healthy_keys_are_silent():
    """A signal that fires on healthy keys is noise and will be ignored."""
    if not _have_node():
        print('SKIP (node unavailable)')
        return
    out = _drive("""
        const a = t('sidebar.settings');
        return { text: a, warns: __warns.length, missing: i18nMissingKeys() };
    """)
    assert out['text'] == 'Settings', 'en resolution must be unaffected'
    assert out['warns'] == 0, 'a fully-translated key must never warn'
    assert out['missing'] == []


@_unit
def test_unknown_key_returns_the_key_and_does_not_warn():
    """An unknown key is a caller bug, not a missing-translation event.

    Conflating the two would flood the new signal with typos and make the
    language-pack use case unreadable.
    """
    if not _have_node():
        print('SKIP (node unavailable)')
        return
    out = _drive("""
        const a = t('totally.unknown.key');
        return { text: a, warns: __warns.length, missing: i18nMissingKeys() };
    """)
    assert out['text'] == 'totally.unknown.key'
    assert out['warns'] == 0, (
        'an absent ENTRY is a different failure from an entry missing one '
        'language; only the latter is the language-pack hazard')
    assert out['missing'] == []


# ── Face 2: the report is usable ─────────────────────────────────────────

@_unit
def test_report_is_one_shot_per_key_so_a_render_loop_cannot_flood():
    if not _have_node():
        print('SKIP (node unavailable)')
        return
    out = _drive("""
        _i18n['probe.a'] = { zh: '甲' };
        _i18n['probe.b'] = { zh: '乙' };
        for (let i = 0; i < 50; i++) { t('probe.a'); t('probe.b'); }
        return { warns: __warns.length, missing: i18nMissingKeys().sort() };
    """)
    assert out['warns'] == 2, (
        f"100 calls produced {out['warns']} warnings — the one-shot latch is "
        f'broken and a hot render loop would drown the console')
    assert out['missing'] == ['en:probe.a', 'en:probe.b']


@_unit
def test_missing_set_is_machine_readable_for_a_pack_acceptance_gate():
    """This is how a future language-pack change proves itself.

    Exercise the UI in `en`, then assert i18nMissingKeys() is empty. That check
    is impossible without this seam, which is why the seam ships BEFORE the
    split.
    """
    if not _have_node():
        print('SKIP (node unavailable)')
        return
    out = _drive("""
        _i18n['probe.gap'] = { zh: '缺' };
        t('probe.gap');
        const before = i18nMissingKeys();
        resetI18nMissingKeysForTests();
        const after = i18nMissingKeys();
        return { before, after };
    """)
    assert out['before'] == ['en:probe.gap'], 'lang:key shape is the contract'
    assert out['after'] == [], 'reset seam must clear it for the next scenario'


@_unit
def test_zh_ui_never_reports_since_zh_is_the_fallback_language():
    """Running in zh cannot produce a miss — guards against a noisy default."""
    if not _have_node():
        print('SKIP (node unavailable)')
        return
    out = _drive("""
        _i18n['probe.zhOnly'] = { zh: '中文文案' };
        t('probe.zhOnly');
        return { warns: __warns.length, missing: i18nMissingKeys() };
    """, lang='zh')
    assert out['warns'] == 0
    assert out['missing'] == []


# ── NEUTER ───────────────────────────────────────────────────────────────

@_unit
def test_NEUTER_removing_the_report_restores_the_silent_degrade():
    if not _have_node():
        print('SKIP (node unavailable)')
        return
    body = """
        _i18n['probe.zhOnly'] = { zh: '中文文案' };
        const text = t('probe.zhOnly');
        return { text, warns: __warns.length };
    """
    shipped = _drive(body)
    neutered = _drive(body, neuter=True)

    assert neutered['warns'] == 0 and neutered['text'] == '中文文案', (
        'without the report the miss is invisible — this is the pre-fix '
        'defect being reproduced')
    assert shipped['warns'] == 1, (
        'the shipped code must report; if these two ever agree the tripwire '
        'has been removed and language packs become unsafe again')


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
