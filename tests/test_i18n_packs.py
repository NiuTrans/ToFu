"""Exactness and bounded-loading contracts for Vite-owned locale data."""

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
LOCALES = ROOT / 'frontend/src/i18n/locales'
DELIVERY = ROOT / 'frontend/src/i18n/generated'
I18N_OWNER = ROOT / 'frontend/src/i18n/index.ts'
I18N_BUNDLE = Path(native_module_path('i18n-catalog-loading.js', I18N_OWNER))


def _load(language: str) -> dict[str, str]:
    value = json.loads((LOCALES / f'{language}.json').read_text(encoding='utf-8'))
    assert isinstance(value, dict)
    return value


def test_both_locales_have_the_exact_same_complete_key_set():
    zh = _load('zh')
    en = _load('en')
    assert zh
    assert zh.keys() == en.keys()
    assert all(isinstance(key, str) and key for key in zh)
    assert all(isinstance(value, str) for value in zh.values())
    assert all(isinstance(value, str) for value in en.values())


def test_locale_json_roundtrips_without_shape_or_unicode_loss():
    for language in ('zh', 'en'):
        path = LOCALES / f'{language}.json'
        raw = path.read_text(encoding='utf-8')
        decoded = json.loads(raw)
        encoded = json.dumps(decoded, ensure_ascii=False)
        assert json.loads(encoded) == decoded
    assert any('\u4e00' <= char <= '\u9fff' for char in ''.join(_load('zh').values()))


def _drive_i18n(body: str) -> dict[str, object]:
    node = shutil.which('node')
    if node is None:
        pytest.skip('node is required for the Vite/ESM i18n harness')
    harness = f"""
globalThis.window = globalThis;
globalThis.localStorage = {{ getItem: () => 'en', setItem: () => {{}} }};
globalThis.document = {{
  documentElement: {{}}, querySelectorAll: () => [], getElementById: () => null,
  get cookie() {{ return ''; }}, set cookie(value) {{}},
}};
globalThis.CustomEvent = class CustomEvent {{
  constructor(type, init) {{ this.type = type; this.detail = init?.detail; }}
}};
globalThis.dispatchEvent = () => true;
{body}
{I18N_BUNDLE.read_text(encoding='utf-8')}
"""
    with tempfile.NamedTemporaryFile(
        'w', suffix='.js', delete=False, encoding='utf-8',
    ) as handle:
        handle.write(harness)
        path = handle.name
    try:
        result = subprocess.run(
            [node, path], capture_output=True, text=True, timeout=90,
        )
        assert result.returncode == 0, result.stderr[-1000:]
        outputs = [
            line[2:] for line in result.stdout.splitlines()
            if line.startswith('@@')
        ]
        assert outputs, result.stdout[-1000:]
        decoded = json.loads(outputs[-1])
        assert isinstance(decoded, dict)
        return decoded
    finally:
        os.unlink(path)


def test_concurrent_locale_readiness_coalesces_one_resource_request():
    result = _drive_i18n("""
let fetchCalls = 0;
let releaseFetch;
const fetchGate = new Promise((resolve) => { releaseFetch = resolve; });
globalThis.fetch = async () => {
  fetchCalls += 1;
  await fetchGate;
  return {
    ok: true, status: 200,
    json: async () => ({ 'sidebar.settings': 'Settings' }),
  };
};
queueMicrotask(async () => {
  const first = ready();
  const second = ready();
  releaseFetch();
  await Promise.all([first, second]);
  console.log('@@' + JSON.stringify({ fetchCalls, text: t('sidebar.settings') }));
});
""")
    assert result == {'fetchCalls': 1, 'text': 'Settings'}


def test_failed_locale_decode_does_not_poison_a_later_retry():
    result = _drive_i18n("""
let fetchCalls = 0;
globalThis.fetch = async () => {
  fetchCalls += 1;
  return {
    ok: true, status: 200,
    json: async () => fetchCalls === 1
      ? []
      : ({ 'sidebar.settings': 'Recovered' }),
  };
};
queueMicrotask(async () => {
  let firstError = '';
  try { await ready(); } catch (error) { firstError = String(error); }
  await ready();
  console.log('@@' + JSON.stringify({
    fetchCalls, firstFailed: firstError.includes('Invalid en locale catalog root'),
    text: t('sidebar.settings'),
  }));
});
""")
    assert result == {
        'fetchCalls': 2,
        'firstFailed': True,
        'text': 'Recovered',
    }


def test_referenced_locale_files_are_only_the_two_supported_languages():
    assert sorted(path.name for path in LOCALES.glob('*.json')) == ['en.json', 'zh.json']


def test_generated_delivery_catalogs_are_compact_exact_copies():
    assert sorted(path.name for path in DELIVERY.glob('*.json')) == [
        'en.generated.json', 'zh.generated.json',
    ]
    for language in ('zh', 'en'):
        authored = _load(language)
        path = DELIVERY / f'{language}.generated.json'
        raw = path.read_text(encoding='utf-8')
        assert raw == json.dumps(authored, ensure_ascii=False, separators=(',', ':')) + '\n'
