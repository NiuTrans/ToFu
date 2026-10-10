"""Retained settings markup owns generated server-injected delivery."""
from pathlib import Path
import json
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_database_template_delivery_matches_retained_source():
    source = ROOT / 'frontend/src/settings-panels/database.html'
    output = ROOT / 'static/settings_panels/database.html'
    assert output.read_bytes() == source.read_bytes()


def test_panel_generator_checks_drift_and_preserves_legacy_fragments(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node unavailable')
    source = tmp_path / 'frontend/src/settings-panels'
    output = tmp_path / 'static/settings_panels'
    source.mkdir(parents=True)
    output.mkdir(parents=True)
    (source / 'database.html').write_text('<div id="settingsTab_database">loading</div>\n')
    legacy = output / 'general.html'
    legacy.write_text('legacy fragment must survive\n')
    module = (ROOT / 'scripts/compose_settings_panels.mjs').as_uri()
    code = f'''import {{ composeSettingsPanels }} from {json.dumps(module)};
      await composeSettingsPanels({{rootDirectory: {json.dumps(str(tmp_path))}}});
      await composeSettingsPanels({{rootDirectory: {json.dumps(str(tmp_path))}, check: true}});'''
    result = subprocess.run([node, '--input-type=module', '-e', code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert legacy.read_text() == 'legacy fragment must survive\n'
    assert (output / 'database.html').read_bytes() == (source / 'database.html').read_bytes()
    (output / 'database.html').write_text('stale')
    check = f'''import {{ composeSettingsPanels }} from {json.dumps(module)};
      await composeSettingsPanels({{rootDirectory: {json.dumps(str(tmp_path))}, check: true}});'''
    result = subprocess.run([node, '--input-type=module', '-e', check], capture_output=True, text=True)
    assert result.returncode != 0
    assert 'Settings panel output is stale' in result.stderr
    assert (output / 'database.html').read_text() == 'stale'
