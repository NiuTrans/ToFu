"""The Settings mobile-client card follows the public health projection."""

import json
import os
from pathlib import Path
import tempfile
import shutil
import subprocess

import pytest

from tests._jsdom import require_node_modules
from tests._paper_vite import compiled_typescript

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
CLIENT_DOWNLOADS = ROOT / 'frontend/src/features/settings/client-downloads.ts'


def _node() -> str:
    require_node_modules('jsdom')
    return shutil.which('node')


def _run(mutate: bool = False) -> dict:
    entry = (
        f"import {{connectFeatureRuntime}} from {str(ROOT / 'frontend/src/feature-registry.ts')!r};\n"
        f"import {{renderSettingsClientInfo}} from {str(CLIENT_DOWNLOADS)!r};\n"
        "connectFeatureRuntime(name => window[name], (name, value) => { window[name] = value; });\n"
        "globalThis.renderSettingsClientInfo = renderSettingsClientInfo;"
    )
    with compiled_typescript(
        CLIENT_DOWNLOADS, contents=entry,
        expose_feature_registry_to_window=True,
    ) as built:
        source = Path(built).read_text(encoding='utf-8')
        if mutate:
            original = source
            source = source.replace(
                'data?.mobile_client_url ? "" : "none"', '""')
            assert source != original, 'mobile visibility gate anchor not found'
        script = r'''
const {JSDOM} = require('jsdom');
const results = [];
const payloads = [
  {mobile_client_url: 'https://x/y.apk', mobile_client_version: '0.1.16'},
  {mobile_client_url: 'https://x/y.apk'},
  {mobile_client_url: 'https://x/y.apk', ios_client_url: 'https://testflight.apple.com/join/abc'},
  {},
];
(async () => {
  for (const payload of payloads) {
    const dom = new JSDOM(`<!DOCTYPE html><body>
      <div id="settingsMobileCard" style="display:none">
        <a id="settingsMobileAndroid"></a>
        <span id="settingsMobileAndroidVersion"></span>
        <a id="settingsMobileIos" class="stg-mobile-row-soon"></a>
        <span id="settingsMobileIosBadge"></span>
      </div></body>`);
    global.window = dom.window;
    global.document = dom.window.document;
    global.HTMLAnchorElement = window.HTMLAnchorElement;
    global.HTMLElement = window.HTMLElement;
    window.Api = {health: {info: async () => payload}};
    eval(SOURCE);
    await globalThis.renderSettingsClientInfo({
      isCurrent: () => true, scope: {signal: undefined},
    });
    await new Promise(resolve => setImmediate(resolve));
    results.push({
      display: document.getElementById('settingsMobileCard').style.display,
      android: document.getElementById('settingsMobileAndroid').getAttribute('href'),
      version: document.getElementById('settingsMobileAndroidVersion').textContent,
      versionDisplay: document.getElementById('settingsMobileAndroidVersion').style.display,
      ios: document.getElementById('settingsMobileIos').getAttribute('href'),
      iosTarget: document.getElementById('settingsMobileIos').getAttribute('target'),
      iosSoon: document.getElementById('settingsMobileIos').classList.contains('stg-mobile-row-soon'),
      iosBadge: document.getElementById('settingsMobileIosBadge').style.display,
    });
    window.close();
  }
  console.log(JSON.stringify(results));
})().catch(error => { console.error(error); process.exit(1); });
'''.replace('SOURCE', json.dumps(source))
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.cjs', encoding='utf-8', delete=False,
            dir=ROOT / 'tests' / 'frontend',
        ) as handle:
            handle.write(script)
            harness = handle.name
        try:
            completed = subprocess.run(
                [_node(), harness], cwd=ROOT,
                capture_output=True, text=True,
            )
        finally:
            os.unlink(harness)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_mobile_client_card_renders_only_when_url_present():
    ready, without_version, ios_ready, absent = _run()
    assert ready['display'] != 'none'
    assert ready['android'] == 'https://x/y.apk'
    assert ready['version'] == 'v0.1.16'
    assert ready['versionDisplay'] != 'none'
    assert ready['iosSoon'] is True
    assert ready['ios'] is None
    assert without_version['version'] == ''
    assert without_version['versionDisplay'] == 'none'
    assert ios_ready['ios'] == 'https://testflight.apple.com/join/abc'
    assert ios_ready['iosTarget'] == '_blank'
    assert ios_ready['iosSoon'] is False
    assert ios_ready['iosBadge'] == 'none'
    assert absent['display'] == 'none'


def test_NC_gate_removed_leaks_dead_card():
    *_, absent = _run(mutate=True)
    assert absent['display'] != 'none'
