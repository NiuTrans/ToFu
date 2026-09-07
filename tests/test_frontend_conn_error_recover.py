#!/usr/bin/env python3
"""Connection-class error presentation keeps recovery truthful and scoped.

The typed presentation owner receives translation and icon ports explicitly.
These tests bundle and call that public owner under both shipped locales; they
do not extract or rewrite implementation-private functions.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest

from tests._runtime_sections import native_module_path


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
OWNER = ROOT / 'frontend/src/error-presentation.ts'
LOCALE_DIR = ROOT / 'frontend/src/i18n/locales'


def _render(*, kind: str, lang: str = 'en') -> str:
    """Render one envelope through the public typed presentation factory."""
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available for typed-owner evaluation')
    owner_bundle = native_module_path(
        '.native/error-presentation-for-recovery.js', OWNER)
    envelope = {
        'kind': kind,
        'severity': 'warning',
        'retryable': True,
        'message': 'something happened',
        'hint': 'preexisting hint',
        'detail': 'raw detail text',
        'model': '',
        'context': '',
        'source': 'test',
        'raw': '',
    }
    harness = r'''
const fs = require('fs');
eval(fs.readFileSync(process.argv[2], 'utf8'));
const locales = {
  zh: JSON.parse(fs.readFileSync(process.argv[3], 'utf8')),
  en: JSON.parse(fs.readFileSync(process.argv[4], 'utf8')),
};
const language = process.argv[5];
const envelope = JSON.parse(process.argv[6]);
const translate = (key, params) => {
  let value = Object.hasOwn(locales[language], key)
    ? locales[language][key]
    : (Object.hasOwn(locales.zh, key) ? locales.zh[key] : key);
  for (const [name, replacement] of Object.entries(params || {})) {
    value = value.replaceAll('{' + name + '}', String(replacement ?? ''));
  }
  return value;
};
const presentation = createErrorEnvelopePresentation({
  translate,
  iconHtml: (name) => '<svg data-ico="' + name + '"></svg>',
});
process.stdout.write(presentation.renderErrorEnvelope(envelope));
'''
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False) as handle:
        handle.write(harness)
        harness_path = handle.name
    try:
        result = subprocess.run(
            [
                node,
                harness_path,
                owner_bundle,
                str(LOCALE_DIR / 'zh.json'),
                str(LOCALE_DIR / 'en.json'),
                lang,
                json.dumps(envelope),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout
    finally:
        os.unlink(harness_path)


def _evaluate_owner(program: str) -> dict[str, object]:
    """Evaluate public pure helpers from the same bundled TypeScript owner."""
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available for typed-owner evaluation')
    owner_bundle = native_module_path(
        '.native/error-presentation-for-recovery.js', OWNER)
    harness = (
        "const fs = require('fs');\n"
        "eval(fs.readFileSync(process.argv[1], 'utf8'));\n"
        + program
    )
    result = subprocess.run(
        [node, '-e', harness, owner_bundle],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


_TITLE = {
    'zh': '连接中断（结果可能已保存）',
    'en': 'Connection lost (your result may be saved)',
}
_RECOVER = {'zh': '恢复', 'en': 'Recover'}
_HINT_FRAGMENT = {'zh': '请不要重新生成', 'en': 'Do NOT regenerate'}
_JARGON = 'Server offline'


@pytest.mark.parametrize('lang', ['zh', 'en'])
def test_server_offline_gets_recover_button_and_localized_copy(lang):
    html = _render(kind='server_offline', lang=lang)
    assert "_recoverOfflineConversations('manual_button')" in html
    assert 'error-block-recover-btn' in html
    assert _RECOVER[lang] in html
    assert _TITLE[lang] in html
    assert _JARGON not in html
    assert _HINT_FRAGMENT[lang] in html
    assert 'data-ico="refresh"' in html


@pytest.mark.parametrize('kind', ['network', 'premature_close', 'abnormal_stop'])
def test_non_recoverable_kinds_have_no_recover_button(kind):
    html = _render(kind=kind, lang='en')
    assert '_recoverOfflineConversations' not in html
    assert 'error-block-recover-btn' not in html
    assert _TITLE['en'] not in html


def test_network_is_not_dressed_as_recoverable():
    html = _render(kind='network', lang='zh')
    assert '_recoverOfflineConversations' not in html
    assert _TITLE['zh'] not in html


def test_error_presentation_owner_has_no_browser_or_runtime_authority():
    source = OWNER.read_text(encoding='utf-8')
    assert 'export function createErrorEnvelopePresentation' in source
    assert 'translate: Translator' in source
    assert 'iconHtml?:' in source
    for ambient_authority in ('runtimeScope', 'globalThis', 'window.', 'document.'):
        assert ambient_authority not in source


def test_public_fallback_and_mojibake_helpers_preserve_display_policy():
    result = _evaluate_owner(r'''
const presentation = createErrorEnvelopePresentation({translate: (key) => key});
const fallback = presentation.fallbackCauseParts({
  fallbackKind: 'network',
  fallbackReason: 'network: API HTTP 502: <html><head><title>502 Bad Gateway</title></head><body><h1>502 Bad Gateway</h1><script>secret()</script><p>openresty</p></body></html>',
});
const long = presentation.fallbackCauseParts({
  fallbackKind: 'timeout',
  fallbackReason: 'timeout: ' + 'x'.repeat(200),
});
process.stdout.write(JSON.stringify({
  fallback,
  long,
  repaired: repairErrorMojibake('è¯·æ±‚å¤±è´¥'),
}));
''')
    fallback = result['fallback']
    assert isinstance(fallback, dict)
    assert fallback['kindLabel'] == 'Network error'
    assert fallback['shown'] == 'API HTTP 502: 502 Bad Gateway · openresty'
    assert '<html>' in fallback['detail']
    assert 'secret()' not in fallback['shown']
    assert fallback['hasCause'] is True
    long = result['long']
    assert isinstance(long, dict)
    assert len(long['shown']) == 161
    assert long['shown'].endswith('…')
    assert result['repaired'] == '请求失败'
