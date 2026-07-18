#!/usr/bin/env python3
"""Connection-class error envelope: recoverable copy + Recover button scoping.

WHY
---
A dropped browser↔server link during a long turn (the VS Code port-forwarding
idle-timeout case) surfaced as a red "Network error / Failed to fetch" bubble
with no guidance and no recovery affordance — jargon a real user can't act on.
The fix gives ONLY the recoverable ``server_offline`` kind a friendly, truthful
headline + hint + an inline Recover button wired to the EXISTING offline
recovery path (``_recoverOfflineConversations('manual_button')``), which adopts
the server's completed result and clears the stale error.

The scoping is the load-bearing subtlety: ``_recoverOfflineConversations`` only
recovers conversations whose trailing message finished ``server_offline`` /
``interrupted``. The ``network`` kind is stamped at ``context:'chat-start'``
when the POST that STARTS a turn fails — no task ever ran, so a Recover button
there would scan, find nothing, and no-op while claiming "your result may be
saved". ``premature_close`` / ``abnormal_stop`` are upstream (server↔gateway)
failures emitted after retries are exhausted — no saved result either. So the
Recover button + "result may be saved" copy MUST be scoped to ``server_offline``
ALONE; every other kind renders byte-identical to before (Retry-oriented).

This test EXTRACTS the real shipped ``renderErrorEnvelope`` (+ its helpers +
``ERROR_KIND_LABELS``) from ``static/js/core/error_envelope.js`` and evals it in
node with the REAL i18n runtime (``_i18n`` table + ``t()`` from i18n.js) under
BOTH ``zh`` and ``en``. It asserts:

  • ``server_offline`` → localized title (``err.conn.title``) + localized hint
    (``err.conn.hint``) + a Recover button whose onclick calls
    ``_recoverOfflineConversations('manual_button')`` — in both languages.
  • ``network`` / ``premature_close`` / ``abnormal_stop`` → NO Recover button,
    NO ``_recoverOfflineConversations`` wiring (the scoping guard), and keep
    their plain kind label.

Poisoned-fixture NC: neuter ``_envIsRecoverable`` to always return false → the
friendly title reverts to the "Server offline" jargon label AND the button
disappears, proving the title/hint/button are all genuinely gated on the
recoverable branch (not tautologies).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
EE_JS = os.path.join(ROOT, 'static', 'js', 'core', 'error_envelope.js')
I18N_JS = os.path.join(ROOT, 'static', 'js', 'i18n.js')


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as f:
        return f.read()


def _brace_match(src: str, open_pos: int) -> int:
    depth = 0
    j = open_pos
    while j < len(src):
        if src[j] == '{':
            depth += 1
        elif src[j] == '}':
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    raise AssertionError('unbalanced braces')


def _extract_fn(src: str, fn_name: str) -> str:
    m = re.search(r'(?:async\s+)?function\s+' + re.escape(fn_name) + r'\s*\(', src)
    assert m, f'{fn_name} not found'
    i = src.find('{', m.end())
    return src[m.start():_brace_match(src, i)]


def _extract_const_obj(src: str, name: str) -> str:
    """Grab `const <name> = { ... };` by brace-matching."""
    m = re.search(r'const\s+' + re.escape(name) + r'\s*=\s*', src)
    assert m, f'{name} not found'
    brace = src.find('{', m.end())
    return src[m.start():_brace_match(src, brace)] + ';'


def _extract_i18n_runtime() -> str:
    src = _read(I18N_JS)
    m = re.search(r'var\s+_i18n\s*=\s*', src)
    assert m, '_i18n table not found in i18n.js'
    brace = src.find('{', m.end())
    table = src[m.start():_brace_match(src, brace)]
    t_fn = _extract_fn(src, 't')
    return 'var _i18nLang = "zh";\n' + table + ';\n' + t_fn


def _render(*, kind: str, lang: str = 'en', poison: str = '') -> str:
    """Render the real renderErrorEnvelope for an envelope of ``kind``."""
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available for extraction-and-eval')

    src = _read(EE_JS)
    labels = _extract_const_obj(src, 'ERROR_KIND_LABELS')
    fns = [
        '_envT', '_envIsRecoverable', 'isErrorEnvelope',
        'normalizeErrorEnvelope', 'renderErrorEnvelope',
    ]
    extracted = labels + '\n' + '\n'.join(_extract_fn(src, f) for f in fns)
    i18n_runtime = _extract_i18n_runtime()

    if poison == 'recoverable':
        # Neuter the scoping predicate → nothing is ever "recoverable". The
        # friendly title/hint override + Recover button must then vanish.
        extracted = extracted.replace(
            'return !!env && env.kind === \'server_offline\';',
            'return false;')
        assert 'return false;' in extracted, 'poison did not apply'

    env = {
        'kind': kind,
        'severity': 'warning',
        'retryable': True,
        'message': 'something happened',
        'hint': 'preexisting hint',
        'detail': 'raw detail text',
        'model': '', 'context': '', 'source': 'test', 'raw': '',
    }

    harness = f'''
{i18n_runtime}
_i18nLang = {json.dumps(lang)};
function escapeHtml(s) {{ return String(s == null ? '' : s).replace(/[&<>"]/g, function(c){{
  return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]; }}); }}
function Icon(name) {{ return '<svg data-ico="' + name + '"></svg>'; }}

{extracted}

process.stdout.write(renderErrorEnvelope({json.dumps(env)}));
'''
    with tempfile.NamedTemporaryFile('w', suffix='.mjs', delete=False) as f:
        f.write(harness)
        tmp = f.name
    try:
        out = subprocess.run([node, tmp], capture_output=True, text=True, timeout=20)
        assert out.returncode == 0, f'node eval failed: {out.stderr}'
        return out.stdout
    finally:
        os.unlink(tmp)


# Localized strings we assert on (must match i18n.js err.conn.* entries).
_TITLE = {'zh': '连接中断（结果可能已保存）', 'en': 'Connection lost (your result may be saved)'}
_RECOVER = {'zh': '恢复', 'en': 'Recover'}
# A distinctive fragment of err.conn.hint per language.
_HINT_FRAG = {'zh': '请不要重新生成', 'en': 'Do NOT regenerate'}
_JARGON = 'Server offline'  # ERROR_KIND_LABELS.server_offline


# ─────────────────── server_offline: friendly + recoverable ───────────────────

@pytest.mark.parametrize('lang', ['zh', 'en'])
def test_server_offline_gets_recover_button_and_localized_copy(lang):
    html = _render(kind='server_offline', lang=lang)
    # Recover button wired to the EXISTING offline-recovery path (not a regen).
    assert "_recoverOfflineConversations('manual_button')" in html
    assert 'error-block-recover-btn' in html
    assert _RECOVER[lang] in html
    # Friendly, truthful title replaces the "Server offline" jargon label.
    assert _TITLE[lang] in html
    assert _JARGON not in html
    # Truthful hint present (in this language).
    assert _HINT_FRAG[lang] in html
    # SVG glyph, not emoji (§3.4).
    assert 'data-ico="refresh"' in html


# ─────────────────── scoping guard: other kinds get NO button ───────────────────

@pytest.mark.parametrize('kind', ['network', 'premature_close', 'abnormal_stop'])
def test_non_recoverable_kinds_have_no_recover_button(kind):
    html = _render(kind=kind, lang='en')
    assert '_recoverOfflineConversations' not in html, \
        f'{kind} must NOT render a Recover button (nothing to recover → false hope)'
    assert 'error-block-recover-btn' not in html
    # It keeps its own plain kind label, not the "result may be saved" copy.
    assert _TITLE['en'] not in html


def test_network_is_not_dressed_as_recoverable():
    """The exact false-hope case: network (chat-start POST failed) must never
    claim the result may be saved."""
    html = _render(kind='network', lang='zh')
    assert '_recoverOfflineConversations' not in html
    assert _TITLE['zh'] not in html


# ─────────────────────────── poisoned-NC (load-bearing) ───────────────────────────

def test_nc_neutered_recoverable_reverts_to_jargon_and_drops_button():
    """Neuter _envIsRecoverable → server_offline loses BOTH the friendly title
    and the Recover button, proving the branch is load-bearing (the assertions
    above aren't tautologies)."""
    html = _render(kind='server_offline', lang='en', poison='recoverable')
    assert '_recoverOfflineConversations' not in html
    assert 'error-block-recover-btn' not in html
    assert _TITLE['en'] not in html
    # Falls back to the raw jargon kind label.
    assert _JARGON in html
