#!/usr/bin/env python3
"""Slice 1 of the i18n split: the language becomes SERVER-VISIBLE.

WHAT THIS SLICE IS
------------------
Owner picked option A (lang cookie) to resolve the contradiction pinned by
tests/test_i18n_split_blocked_on_lang_signal.py: an eagerly-shipped
single-language pack must be chosen at serve time, by a server that could not
read localStorage['tofu_ui_lang'].

This slice ONLY establishes the signal:
  * i18n.js mirrors its language into a `tofu_ui_lang` cookie — on every boot
    AND in setLanguage(). localStorage stays authoritative for the client.
  * routes/common.py::request_ui_lang() reads that cookie through a WHITELIST.
  * index_page()'s served-HTML cache gains a `lang` dimension.

It deliberately does NOT split the bundle yet. Byte-for-byte, every client
still receives the same dual-language bundle, so this slice cannot regress
first paint. Slice 2 emits the per-language artifacts and is the one that
banks the measured 7.6%.

WHY THE MIRROR RUNS ON EVERY BOOT, NOT ONLY ON CHANGE
-----------------------------------------------------
A user who chose their language before this shipped has localStorage but no
cookie. If the mirror only ran inside setLanguage(), that user would be served
the DEFAULT language bundle forever and would never know why. Writing on boot
back-fills them. That also means slice 1 must land before slice 2, so cookies
are already populated when per-language bundles start being served.

WHY THE WHITELIST IS THE SECURITY BOUNDARY
------------------------------------------
The resolved value will select a bundle FILENAME in slice 2. A cookie is
attacker-controlled, so `request_ui_lang()` must return a member of a fixed
tuple and nothing else — never the raw string. Path-traversal and unknown
locales collapse to the default.

Run: python3 tests/test_i18n_lang_cookie_signal.py
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._runtime_sections import native_module_path

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMMON = os.path.join(REPO, 'routes', 'common.py')
I18N = os.path.join(REPO, 'frontend', 'src', 'i18n', 'index.ts')
I18N_BUNDLE = native_module_path('i18n-cookie-signal.js', I18N)

try:
    import pytest
except ImportError:
    pytest = None


def _unit(fn):
    return fn if pytest is None else pytest.mark.unit(fn)


def _read(p):
    with open(p, encoding='utf-8') as f:
        return f.read()


def _load_lang_resolver():
    """Exec just the ui-lang slice of routes/common.py.

    Importing routes.common boots the wider route graph. Slicing keeps this
    unit focused while still executing the shipped source text, not a copy.
    """
    src = _read(COMMON)
    start = src.index('_UI_LANG_COOKIE = ')
    end = src.index('\n_APP_ASSET_MARKER =')
    ns = {'logger': types.SimpleNamespace(debug=lambda *a, **k: None)}
    exec(compile(src[start:end], 'common_slice', 'exec'), ns)
    return ns


def _resolve(ns, cookie_value):
    ns['request'] = types.SimpleNamespace(
        cookies={} if cookie_value is None else {'tofu_ui_lang': cookie_value})
    return ns['request_ui_lang']()


# ── The whitelist ────────────────────────────────────────────────────────

@_unit
def test_known_languages_pass_through():
    ns = _load_lang_resolver()
    assert _resolve(ns, 'zh') == 'zh'
    assert _resolve(ns, 'en') == 'en'


@_unit
def test_case_and_whitespace_are_normalised():
    """Cookies get mangled by proxies and hand-editing; be liberal on input."""
    ns = _load_lang_resolver()
    assert _resolve(ns, 'EN') == 'en'
    assert _resolve(ns, '  en ') == 'en'


@_unit
def test_hostile_and_unknown_values_collapse_to_the_default():
    """THE security face — this value will pick a bundle filename in slice 2.

    A cookie is attacker-controlled. If the raw string could reach a filename,
    a crafted cookie becomes a path-traversal read. The resolver must only ever
    emit a member of the fixed tuple.
    """
    ns = _load_lang_resolver()
    langs = ns['_UI_LANGS']
    default = ns['_UI_LANG_DEFAULT']
    for hostile in ('../../etc/passwd', 'zh/../../secret', 'fr', 'zh-CN',
                    '', 'zh\x00en', 'ZH;en', 'a' * 500):
        got = _resolve(ns, hostile)
        assert got in langs, (
            f'{hostile!r} resolved to {got!r}, which is NOT in the whitelist '
            f'{langs} — in slice 2 that value selects a bundle filename')
        assert got == default


@_unit
def test_missing_cookie_and_missing_request_context_both_default():
    ns = _load_lang_resolver()
    assert _resolve(ns, None) == ns['_UI_LANG_DEFAULT']
    ns['request'] = None          # no request context at all (worker/test)
    assert ns['request_ui_lang']() == ns['_UI_LANG_DEFAULT'], (
        'must not raise outside a request context — background callers exist')


# ── The served-HTML cache must vary by language ──────────────────────────

@_unit
def test_index_html_cache_has_a_language_dimension():
    """Without this, the first visitor's language would be served to everyone.

    This is the exact fact test_i18n_split_blocked_on_lang_signal asserted was
    ABSENT; it is now present, which is what unblocks slice 2.
    """
    src = _read(COMMON)
    m = re.search(r'_bundled_index_cache = \{([^}]*)\}', src)
    assert m, 'could not locate the served-HTML cache'
    assert "'lang'" in m.group(1), (
        'the served-HTML cache lost its language key — a per-language bundle '
        'would then be cross-served between languages')

    # And the cache-hit test must actually compare it.
    assert "_bundled_index_cache['lang'] == request_ui_lang()" in src, (
        'the cache-hit condition ignores language — a zh visitor could be '
        'served the en HTML from cache')


# ── The client mirror ────────────────────────────────────────────────────

@_unit
def test_client_mirrors_language_on_boot_and_on_change():
    src = _read(I18N)
    assert 'function syncLanguageCookie' in src, 'the cookie mirror is gone'

    # Boot: called by ready() after the selected locale has loaded.
    ready = re.search(r'export async function ready\(\).*?\n\}', src, re.S)
    assert ready and 'syncLanguageCookie(_i18nLang)' in ready.group(0), (
        'the mirror is not invoked at boot — users who set their language '
        'BEFORE this shipped would never get a cookie, and would be served '
        'the default bundle forever')

    # Change: called inside setLanguage.
    m = re.search(r'export async function setLanguage\(.*?\n\}', src, re.S)
    assert m, 'setLanguage not found'
    body = m.group(0)
    assert 'syncLanguageCookie(language)' in body, (
        'setLanguage does not update the cookie — the switch would not '
        'survive a reload')
    assert "localStorage.setItem('tofu_ui_lang', language)" in body, (
        'localStorage must stay authoritative for the client')


@_unit
def test_cookie_attributes_are_sane_for_a_display_preference():
    src = _read(I18N)
    m = re.search(r'function syncLanguageCookie\(.*?\n\}', src, re.S)
    assert m, 'syncLanguageCookie body not found'
    body = m.group(0)
    assert 'path=/' in body, 'must apply to the whole app'
    assert 'SameSite=Lax' in body, (
        'a display preference should not ride cross-site requests')
    assert 'language: UiLanguage' in m.group(0), (
        'the cookie writer must only accept the whitelisted language union')
    assert 'try' in body, (
        'cookie writes throw when cookies are disabled; that must not break boot')


@_unit
def test_cookie_write_survives_cookies_being_disabled():
    """Behavioural: a throwing document.cookie must not break i18n boot."""
    if not shutil.which('node'):
        print('SKIP (node unavailable)')
        return
    src = _read(I18N_BUNDLE)
    harness = f"""
globalThis.window = globalThis;
globalThis.localStorage = {{ getItem: () => 'en', setItem: () => {{}} }};
globalThis.document = {{
  documentElement: {{}}, querySelectorAll: () => [], getElementById: () => null,
  addEventListener: () => {{}}, readyState: 'complete',
  get cookie() {{ return ''; }},
  set cookie(v) {{ throw new Error('cookies disabled'); }},
}};
globalThis.CustomEvent = class CustomEvent {{ constructor(type, init) {{ this.type = type; this.detail = init?.detail; }} }};
globalThis.dispatchEvent = () => true;
{src}
(async () => {{
  await ready();
  console.log('@@' + JSON.stringify({{ lang: _i18nLang, t: t('sidebar.settings') }}));
}})().catch((error) => {{ console.error(error); process.exitCode = 1; }});
"""
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False,
                                     encoding='utf-8') as fh:
        fh.write(harness)
        path = fh.name
    try:
        r = subprocess.run([shutil.which('node'), path],
                           capture_output=True, text=True, timeout=90)
        assert r.returncode == 0, (
            f'i18n.js failed to load when cookies throw: {r.stderr[:500]}')
        out = [l for l in r.stdout.splitlines() if l.startswith('@@')]
        assert out, f'no probe output: {r.stdout[-300:]}'
        assert '"lang":"en"' in out[-1], 'language resolution broke'
        assert '"t":"Settings"' in out[-1], 'translation broke'
    finally:
        os.unlink(path)


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
