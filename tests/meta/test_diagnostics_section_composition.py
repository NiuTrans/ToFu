"""The raw Diagnostics fixture composes its real eager dependencies before use."""
from pathlib import Path
import subprocess

import pytest

from tests._jsdom import require_node_modules
from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_diagnostics_fixture_boots_without_ambient_dependency_stubs():
    require_node_modules('jsdom', 'vite')
    section = runtime_section_path('core/debug_panel.js')
    result = subprocess.run(['node', '-e', r'''
const assert = require('node:assert/strict');
const { JSDOM } = require(process.argv[2] + '/node_modules/jsdom');
const dom = new JSDOM('<body></body>', { url: 'http://localhost/' });
global.window = dom.window;
global.document = dom.window.document;
require('node:vm').runInThisContext(require('node:fs').readFileSync(process.argv[1], 'utf8'));
assert.equal(typeof showMessagesInDebug, 'function');
assert.ok(window.__tofuDiagRing, 'the real collection owner starts');
dom.window.close();
console.log('PASS Diagnostics fixture composition');
''', section, str(ROOT)], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert 'PASS Diagnostics fixture composition' in result.stdout
