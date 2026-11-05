"""Native form controls use the same DOM constructors as the harness window."""
import json
import subprocess
from pathlib import Path

import pytest

from tests._jsdom import require_node_modules

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_native_select_uses_the_harness_dom_realm():
    require_node_modules('jsdom')
    script = r'''
const assert = require('node:assert/strict');
const {setup} = require('./tests/_jsdom_harness.js');
const {window,document} = setup({root:ROOT,html:'<select><option value="saved">Saved</option></select>',targets:[]});
assert.equal(typeof HTMLSelectElement,'function','typed select readers require the native DOM constructor');
assert.equal(HTMLSelectElement,window.HTMLSelectElement);
assert.ok(document.querySelector('select') instanceof HTMLSelectElement);
window.close();
'''.replace('ROOT', json.dumps(str(ROOT)))
    result = subprocess.run(['node', '-e', script], cwd=ROOT,
        capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
