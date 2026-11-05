"""Visible diagnostic evidence follows complete wire content, including in-place edits."""
from pathlib import Path
import subprocess

import pytest

from tests._jsdom import require_node_modules
from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('kind', ['text', 'blocks', 'tool', 'in-place'])
def test_debug_message_updates_equal_size_evidence(kind):
    require_node_modules('jsdom', 'vite')
    source = runtime_section_path('core/debug_panel.js')
    result = subprocess.run(['node', '-e', r'''
const assert = require('node:assert/strict');
const { JSDOM } = require(process.argv[2] + '/node_modules/jsdom');
const dom = new JSDOM('<div id="debugTitle"></div><div id="debugContent"></div>',
  { url: 'http://localhost/' });
Object.assign(globalThis, { window: dom.window, document: dom.window.document,
  activeConvId: 'conv-1', conversations: [{id: 'conv-1'}], debugVisible: true,
  t: key => key, Icon: () => '', escapeHtml: s => String(s ?? '')
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;') });
require('node:vm').runInThisContext(require('node:fs').readFileSync(process.argv[1], 'utf8'));
const kind = process.argv[3];
const before = { _msgId: 'stable-id', role: 'assistant', content: 'old' };
if (kind === 'blocks') before.content = [{type: 'text', text: 'old'}];
if (kind === 'tool') before.tool_calls = [{id: 'call-1', type: 'function',
  function: { name: 'alpha', arguments: '{"value":"old"}' }}];
showMessagesInDebug([before], '', false, 'conv-1');
document.querySelector('.debug-msg-header').click();
const after = kind === 'in-place' ? before : structuredClone(before);
if (kind === 'blocks') after.content[0].text = 'new';
else if (kind === 'tool') after.tool_calls[0].function = { name: 'bravo', arguments: '{"value":"new"}' };
else after.content = 'new';
showMessagesInDebug([after], '', true, 'conv-1');
const block = document.querySelector('.debug-msg-block');
assert.ok(block.classList.contains('open'), 'updated message remains expanded');
assert.match(block.querySelector('.debug-struct').textContent, /new/,
  'equal-length evidence must replace stale content');
assert.match(block.querySelector('.debug-raw pre').textContent, /new/,
  'the raw envelope also shows the updated value');
assert.deepEqual(document.getElementById('debugContent')._rawMessages, [after],
  'copy/export retains the complete new message');
assert.strictEqual(block._msgRef, after, 'lazy body reads the latest message');
if (kind === 'tool') assert.match(block.querySelector('.debug-tool-calls').textContent, /bravo/);
dom.window.close();
''', source, str(ROOT), kind], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
