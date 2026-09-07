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

from tests._runtime_sections import native_module_path

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
I18N = os.path.join(REPO, 'frontend', 'src', 'i18n', 'index.ts')
I18N_BUNDLE = native_module_path('i18n-missing-tripwire.js', I18N)
LOCALES = os.path.join(REPO, 'frontend', 'src', 'i18n', 'locales')

try:
    import pytest
except ImportError:
    pytest = None


def _unit(fn):
    return fn if pytest is None else pytest.mark.unit(fn)


def _have_node():
    return shutil.which('node') is not None


def _drive(body, lang='en', neuter=False):
    """Load the bundled native i18n owner under a minimal DOM and run body."""
    src = open(I18N_BUNDLE, encoding='utf-8').read()
    if neuter:
        needle = 'console.warn(`[i18n] missing ${fingerprint}`);'
        assert needle in src, (
            'NEUTER anchor missing — the tripwire call was reworded; update '
            'this test so it keeps proving the report is load-bearing')
        src = src.replace(needle, '')

    harness = f"""
globalThis.window = globalThis;
globalThis.localStorage = {{ getItem: () => {json.dumps(lang)}, setItem: () => {{}} }};
globalThis.document = {{ documentElement: {{}}, querySelectorAll: () => [],
                        getElementById: () => null,
                        addEventListener: () => {{}}, readyState: 'complete',
                        get cookie() {{ return ''; }}, set cookie(value) {{}} }};
globalThis.CustomEvent = class CustomEvent {{ constructor(type, init) {{ this.type = type; this.detail = init?.detail; }} }};
globalThis.dispatchEvent = () => true;
const __warns = [];
console.warn = (...a) => __warns.push(a.join(' '));
{src}
(async () => {{
  await ready();
  const __out = await (async () => {{ {body} }})();
  console.log('@@' + JSON.stringify(__out));
}})().catch((error) => {{ console.error(error); process.exitCode = 1; }});
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
def test_missing_key_is_visible_and_reported():
    """A missing key must be both visible in the UI and observable in logs."""
    if not _have_node():
        print('SKIP (node unavailable)')
        return
    out = _drive("""
        return { text: t('probe.missing'), warns: __warns.length };
    """)
    assert out['text'] == 'probe.missing'
    assert out['warns'] == 1


@_unit
def test_healthy_keys_are_silent():
    """A signal that fires on healthy keys is noise and will be ignored."""
    if not _have_node():
        print('SKIP (node unavailable)')
        return
    out = _drive("""
        const a = t('sidebar.settings');
        return { text: a, warns: __warns.length };
    """)
    assert out['text'] == 'Settings', 'en resolution must be unaffected'
    assert out['warns'] == 0, 'a fully-translated key must never warn'


@_unit
def test_unknown_key_returns_the_key_and_warns():
    """Caller typos must not silently degrade into untranslated chrome."""
    if not _have_node():
        print('SKIP (node unavailable)')
        return
    out = _drive("""
        const a = t('totally.unknown.key');
        return { text: a, warns: __warns.length };
    """)
    assert out['text'] == 'totally.unknown.key'
    assert out['warns'] == 1


# ── Face 2: the report is usable ─────────────────────────────────────────

@_unit
def test_report_is_one_shot_per_key_so_a_render_loop_cannot_flood():
    if not _have_node():
        print('SKIP (node unavailable)')
        return
    out = _drive("""
        for (let i = 0; i < 50; i++) { t('probe.a'); t('probe.b'); }
        return { warns: __warns.length };
    """)
    assert out['warns'] == 2, (
        f"100 calls produced {out['warns']} warnings — the one-shot latch is "
        f'broken and a hot render loop would drown the console')


@_unit
def test_locale_key_sets_match_for_pack_acceptance_gate():
    """The split locale chunks must expose identical key sets."""
    with open(os.path.join(LOCALES, 'zh.json'), encoding='utf-8') as handle:
        zh = json.load(handle)
    with open(os.path.join(LOCALES, 'en.json'), encoding='utf-8') as handle:
        en = json.load(handle)
    assert set(en) == set(zh), (
        f'locale key drift: en-only={sorted(set(en) - set(zh))[:10]}, '
        f'zh-only={sorted(set(zh) - set(en))[:10]}')


@_unit
def test_zh_ui_reports_an_unknown_key_once_too():
    if not _have_node():
        print('SKIP (node unavailable)')
        return
    out = _drive("""
        t('probe.missing');
        t('probe.missing');
        return { warns: __warns.length };
    """, lang='zh')
    assert out['warns'] == 1


# ── NEUTER ───────────────────────────────────────────────────────────────

@_unit
def test_NEUTER_removing_the_report_restores_the_silent_degrade():
    if not _have_node():
        print('SKIP (node unavailable)')
        return
    body = """
        const text = t('probe.missing');
        return { text, warns: __warns.length };
    """
    shipped = _drive(body)
    neutered = _drive(body, neuter=True)

    assert neutered['warns'] == 0 and neutered['text'] == 'probe.missing', (
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
