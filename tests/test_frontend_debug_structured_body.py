"""jsdom test: the debug/state panel's STRUCTURED message body renderer.

Owner directives (2026-08-05, screenshot of the inline state panel):
  1. "the JSON is not formatted — arguments holds a complex JSON string that
     is not rendered, very hard to read" → tool_call arguments are PARSED:
     long/multi-line string values render as readable text blocks with REAL
     newlines, nested objects as syntax-coloured JSON, scalars inline. The
     raw envelope stays one click away in the 原始 JSON <details> (fidelity
     for the copy path).
  2. "don't show the big TOOLS list for every tool call result — meaningless"
     → the inline panel never mounts the tools-schema block (the drawer
     detail keeps it: there it is part of the request payload).
  3. "redesign the panel properly" → a round-scoped increment small enough
     to read auto-expands on mount, so the panel answers at a glance.

Renderer under test: _renderMsgBodyHtml / _debugRenderBody /
_debugOpenBlock (core/debug_panel.js) driven through the REAL inline panel
mount (request_inspector.js openStateInspector → _riRenderToolPanel).

NEUTERs (each patches a COPY; the shipped files stay byte-identical):
  1. Drop the arguments JSON.parse in _debugToolCallHtml → the per-key
     structured argument blocks disappear and the pin flips red.
  2. Disable the auto-expand gate in _riRenderToolPanel → blocks render
     collapsed and the auto-expand pin flips red.
"""

from __future__ import annotations

import os
import json
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_sections_dir

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = runtime_sections_dir()
_DEBUG_SRC = os.path.join(JS_DIR, 'core', 'debug_panel.js')
_RI_SRC = os.path.join(JS_DIR, 'core', 'request_inspector.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[4];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body>' +
  '<div id="riDrawer" style="display:none">' +
  '  <div id="riTaskList"></div><div id="riRoundList"></div>' +
  '  <div class="debug-panel" id="debugPanel">' +
  '    <div id="debugTitle"></div><div id="debugContent"></div>' +
  '  </div>' +
  '</div>' +
  '<div id="chatinner">' +
  '  <div data-prn="1"><div class="ri-tool-anchor-row" data-ri-state="task-T1:2"></div></div>' +
  '</div></body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;

global.escapeHtml = win.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
global.Icon = win.Icon = (n, s) => `<svg data-icon="${n}"></svg>`;
global.t = win.t = (k, a) => {
  let s = ({
    'ri.tabState': 'Result state', 'ri.tabRequest': 'Request',
    'ri.stateClose': 'Close', 'ri.loading': 'Loading…',
    'ri.stateEmpty': 'No state mirror', 'ri.stateKindTip': 'state',
    'ri.requestKindTip': 'request',
  })[k] || k;
  if (a) for (const kk of Object.keys(a)) s = s.replace('{' + kk + '}', String(a[kk]));
  return s;
};
global.activeConvId = win.activeConvId = 'conv-1';
global.debugVisible = win.debugVisible = false;
global._featureFlags = win._featureFlags = { debug_mode: true };
global.conversations = win.conversations = [{ id: 'conv-1' }];

/* The wire shape the owner screenshotted: an assistant turn whose tool_call
 * arguments are a JSON STRING holding a multi-line write_file content arg,
 * plus a nested object arg and a nested-JSON-string arg. */
const ASSIST = {
  role: 'assistant',
  reasoning_content: 'thinking about the file\nsecond reasoning line',
  content: '',
  tool_calls: [{
    id: 'write_file_273', type: 'function',
    function: { name: 'write_file', arguments: JSON.stringify({
      path: 'docs/RUNBOOK.md',
      content: '# PG Runbook\n\n> trigger background\n\n## 0. Facts\n\n| item | value |\n',
      mode: 'overwrite',
      nested: { timeout: 30, retries: 2 },
      filters: '{"include":["*.md"]}',
    }) },
  }],
};
const TOOLRES = {
  role: 'tool', tool_call_id: 'write_file_273', name: 'write_file',
  content: '{"ok":true,"path":"docs/RUNBOOK.md","bytes":1234}',
};
const R1 = [
  { role: 'system', content: 'SYS' },
  { role: 'user', content: 'seed question' },
];
const R2 = R1.concat([ASSIST, TOOLRES]);

win.Api = global.Api = {
  tasks: {
    byConv: async () => ({ convId: 'conv-1', tasks: [] }),
    getRequests: async () => ({ eventsAvailable: false }),
    getRequestPayload: async (taskId, roundNum, turn, kind) => {
      const msgs = String(roundNum) === '1' ? R1 : R2;
      return { taskId, roundNum, turn: '', kind: kind || 'state',
        model: 'm', params: {}, label: 'R' + roundNum, tools: [],
        messages: msgs.map((m) => ({ ...m })) };
    },
  },
  clientError: { report: () => {} },
};

const debugSrc = fs.readFileSync(process.argv[2], 'utf8');
const riSrc = fs.readFileSync(process.argv[3], 'utf8');
eval(debugSrc + '\n' + riSrc);

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  await openStateInspector('task-T1', 2);
  await sleep(40);
  const panel = document.querySelector('.ri-state-panel');
  check('panel_mounted', !!panel);
  if (!panel) { console.log(out.join('\n')); return; }

  /* ── Owner #2: no TOOLS schema dump in the per-tool panel ── */
  check('no_tools_schema_block', !panel.querySelector('.debug-tools-block'));

  /* ── Owner #3: the round-scoped increment auto-expands on mount ── */
  const blocks = panel.querySelectorAll('.debug-msg-block');
  check('increment_is_two_blocks', blocks.length === 2);
  check('auto_expanded_without_click',
    panel.querySelectorAll('.debug-msg-block.open').length === 2);

  /* ── Owner #1: arguments JSON string is PARSED and readable ── */
  const ab = blocks[0];
  check('tc_name_rendered', ab.textContent.indexOf('write_file') !== -1);
  const argKeys = [...ab.querySelectorAll('.debug-arg-key')]
    .map((e) => e.textContent);
  check('args_parsed_to_keys',
    argKeys.indexOf('path') !== -1 && argKeys.indexOf('content') !== -1);
  const argVal = ab.querySelector('.debug-arg-val');
  check('content_arg_is_text_block', !!argVal);
  check('content_has_real_newlines',
    !!argVal && argVal.textContent.indexOf('\n') !== -1);
  /* The escaped two-char sequence backslash-n must NOT appear in the
   * readable block (that was the owner's "unreadable" complaint). */
  check('content_not_escaped',
    !!argVal && argVal.textContent.indexOf('\\n') === -1);
  /* Short scalar args stay inline; nested objects and nested-JSON-string
   * args render as syntax-coloured JSON. */
  const jsonBlocks = ab.querySelectorAll('.debug-json');
  const jsonText = [...jsonBlocks].map((e) => e.textContent).join('\n');
  check('nested_object_arg_rendered', jsonText.indexOf('timeout') !== -1);
  check('nested_json_string_arg_parsed', jsonText.indexOf('include') !== -1);
  /* Reasoning renders as readable text, not a quoted JSON string. */
  const texts = [...ab.querySelectorAll('.debug-text')]
    .map((e) => e.textContent).join('\n');
  check('reasoning_readable', texts.indexOf('second reasoning line') !== -1);
  /* The raw envelope is preserved (fidelity for the copy path) and still
   * carries the escaped form — the structured view ADDED readability, it
   * did not replace the wire truth. */
  const rawPre = ab.querySelector('.debug-raw pre');
  check('raw_envelope_preserved', !!rawPre &&
    rawPre.innerHTML.indexOf('tool_calls') !== -1 &&
    rawPre.innerHTML.indexOf('reasoning_content') !== -1);

  /* ── Tool result: a JSON-string body renders parsed ── */
  const tb = blocks[1];
  const tJson = tb.querySelector('.debug-json');
  check('tool_result_json_parsed', !!tJson &&
    tJson.textContent.indexOf('"ok"') !== -1 &&
    tJson.textContent.indexOf('RUNBOOK') !== -1);

  console.log(out.join('\n'));
})().catch((e) => {
  console.log('FAIL harness_exception ' + (e && e.stack || e));
});
"""


def _run(debug_path=None, ri_path=None, expect_fail=None):
    harness = os.path.join(HERE, '_debug_structured_body_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, debug_path or _DEBUG_SRC, ri_path or _RI_SRC,
             ROOT],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    if expect_fail:
        assert f'FAIL {expect_fail}' in output, (
            f'neutered copy did NOT flip {expect_fail} red:\n{output}')
        return output
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'structured-body renderer failures:\n' + output
    assert output.count('PASS') >= 14, f'expected >=14 PASS, got:\n{output}'
    return output


def _neutered_copy(src_path, anchor, replacement, tmp_name):
    with open(src_path, encoding='utf-8') as f:
        src = f.read()
    assert anchor in src, f'neuter anchor drifted in {os.path.basename(src_path)}'
    neutered = src.replace(anchor, replacement, 1)
    assert neutered != src
    tmp = os.path.join(HERE, tmp_name)
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(neutered)
    return tmp, src


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed')
def test_structured_body_renderer():
    _run()


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed')
def test_neuter_arguments_parse_dropped_flips_red():
    """NC: make _debugToolCallHtml treat the arguments string as opaque → the
    per-key structured argument blocks MUST disappear (the owner's unreadable
    one-escaped-line shape) and the pin MUST fail. Proves the readable
    rendering comes from the JSON.parse, not from the raw dump."""
    tmp, src = _neutered_copy(
        _DEBUG_SRC,
        'const parsed = typeof raw === "string" ? _debugTryParseJson(raw)',
        'const parsed = false ? _debugTryParseJson(raw)',
        '_debug_panel_struct_neutered.js')
    try:
        _run(debug_path=tmp, expect_fail='args_parsed_to_keys')
    finally:
        os.remove(tmp)
    with open(_DEBUG_SRC, encoding='utf-8') as f:
        assert f.read() == src, 'shipped debug_panel.js must be byte-identical'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed')
def test_neuter_auto_expand_dropped_flips_red():
    """NC: disable the auto-expand gate → blocks render collapsed and the
    at-a-glance pin MUST fail. The panel exists to answer one round; making
    the user click every message open again is the old unreadable panel."""
    tmp, src = _neutered_copy(
        _RI_SRC,
        'if (shown.length <= 6 && total <= 300 * 1024 &&',
        'if (false &&',
        '_request_inspector_struct_neutered.js')
    try:
        _run(ri_path=tmp, expect_fail='auto_expanded_without_click')
    finally:
        os.remove(tmp)
    with open(_RI_SRC, encoding='utf-8') as f:
        assert f.read() == src, (
            'shipped request_inspector.js must be byte-identical')


def test_structured_body_static_pins():
    """Static pins on the pieces a future refactor could silently drop while
    keeping every jsdom probe green — including the owner directive that the
    inline panel NEVER re-grows the tools-schema dump."""
    ri = open(_RI_SRC, encoding='utf-8').read()
    assert 'updateDebugToolsBlock' not in ri, (
        'the TOOLS schema block crept back into the inline per-tool panel — '
        'owner 2026-08-05: meaningless noise identical on every round')
    assert '_debugOpenBlock' in ri, 'the auto-expand call is gone'
    dbg = open(_DEBUG_SRC, encoding='utf-8').read()
    for sym in ('_renderMsgBodyHtml', '_debugRenderBody', '_debugOpenBlock',
                '_debugToolCallHtml', '_debugTryParseJson'):
        assert sym in dbg, f'{sym} missing from debug_panel.js'
    locale_dir = os.path.join(ROOT, 'frontend', 'src', 'i18n', 'locales')
    locales = [
        json.loads(open(os.path.join(locale_dir, f'{lang}.json'),
                        encoding='utf-8').read())
        for lang in ('zh', 'en')
    ]
    for key in ('debug.structReasoning', 'debug.structContent',
                'debug.structToolCalls', 'debug.structToolResult',
                'debug.structFields', 'debug.structRawJson',
                'debug.structImage'):
        assert all(key in locale for locale in locales), \
            f'{key} missing from native locale JSON'
    css = open(os.path.join(ROOT, 'static', 'styles.css'),
               encoding='utf-8').read()
    for cls in ('.debug-struct', '.debug-tc-card', '.debug-arg-val',
                '.debug-raw', '.debug-json'):
        assert cls in css, f'{cls} missing from styles.css'
    # 2026-08-13 owner screenshot: the structured view ALSO mounts inside
    # .ri-state-panel in the chat flow, where the inherited face is the 14.5px
    # sans chat font — arg rows rendered ~45% larger than the 10–11px panel
    # chrome around them. The baseline must be pinned on .debug-struct itself,
    # not inherited from .debug-panel (which only wraps the drawer view).
    base = next((ln for ln in css.splitlines()
                 if ln.startswith('.debug-struct{')), '')
    assert 'font-size:' in base and 'font-family:' in base, (
        '.debug-struct lost its pinned font baseline — arg rows fall back to '
        'the mount container face (the 2026-08-13 mixed-font bug)')


if __name__ == '__main__':
    print(_run())
