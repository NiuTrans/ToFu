"""Behavioural proof for the finish_info lazy-popover split (Epic-E sub-8).

Drives the REAL shipped ui/finish_info.js (+ ui/finish_info_rich.js in
the rich phase) under bare node in TWO configurations:

  A. DEGRADED — core file alone (the rich module absent): renderFinishInfo
     on a cost-bearing assistant message must NOT throw (no
     _buildCostPopover ReferenceError), must emit the EMPTY placeholder
     span (no pre-built HTML), and must STASH the build ctx in
     _costCtxByMsg keyed by the msg object.
  B. RICH — core + rich: the deferred _toggleCostPopover, driven with a
     fake tag element, must BUILD the popover from the stash into the
     placeholder (the per-round breakdown markup appears) and open the
     popover element.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_sections_dir

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = runtime_sections_dir()


def _node_available() -> bool:
    return bool(shutil.which('node'))


_HARNESS = r"""
const fs = require('fs');

// ── window-scope stubs ──
global.window = global;
let _i18nLang = 'zh';
global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
global.t = (k, o) => {
  o = o || {};
  if (k === 'finishInfo.cbState.upstream') return 'UPSTREAM-BADGE';
  if (k === 'finishInfo.cacheBreakLabel') return 'MISS: ' + (o.reason || '');
  if (k && k.indexOf('{') === -1 && o && Object.keys(o).length) {
    let s = k; for (const kk in o) s = s.replace('{'+kk+'}', o[kk]); return s;
  }
  return typeof k === 'string' ? k : '';
};
global.formatCny = (n) => '¥' + Number(n||0).toFixed(4);
global.calcCostCny = () => ({ costCny: 0.0123, cacheSavingsCny: 0.001 });
global.calcCost = () => 0.01;
global.fmt = (n) => String(n);
global.Icon = () => '<i></i>';
global.IconDot = () => '<i></i>';
global._brandSvg = () => '<b></b>';
global._detectBrand = () => '';
global._providerDisplayName = () => 'm';
global._isThinkingCapable = () => false;
global._recoverOfflineConversations = () => {};
global._safeClipboardWrite = () => {};
global._isTurnInFlight = () => false;
global.debugLog = () => {};
global.apiUrl = (p) => p;

// DOM stubs sufficient for _toggleCostPopover.
const _els = [];
global.document = {
  createElement: (tag) => {
    const el = {
      tag, children: [], style: {}, className: '',
      set innerHTML(v) { this._html = v; }, get innerHTML() { return this._html || ''; },
      appendChild(c) { this.children.push(c); return c; },
      remove() {}, contains() { return false; },
      offsetWidth: 320, offsetHeight: 200,
    };
    _els.push(el); return el;
  },
  body: { appendChild: (c) => c },
  addEventListener: () => {}, removeEventListener: () => {},
};
global.innerHeight = 800;
global.addEventListener = () => {};
global.removeEventListener = () => {};
global.setTimeout = (fn) => fn();

// getActiveConv + _msgElIndex for the lazy-build lookup.
const MSG = {
  role: 'assistant', content: 'answer', timestamp: 1000,
  finishReason: 'stop', model: 'm',
  usage: { prompt_tokens: 100, completion_tokens: 10,
           cache_creation_input_tokens: 50000, cache_read_input_tokens: 40000 },
  cost: { inputTokens: 100, totalInputTokens: 100, outputTokens: 10,
          costCny: 0.0123, cacheSavingsCny: 0.001,
          cacheReadTokens: 40000, cacheWriteTokens: 50000 },
  _taskId: 't1',
};
global.getActiveConv = () => ({ id: 'c1', messages: [MSG] });
global._msgElIndex = () => 0;

eval(fs.readFileSync(process.argv[2], 'utf8'));   // REAL ui/finish_info.js
// const/let in eval stay in the eval's lexical scope (the real bundle is one
// concatenated script, so the WeakMap IS shared there) — capture an accessor.
eval('global.__getCtx = (m) => (typeof _costCtxByMsg !== \'undefined\') ? _costCtxByMsg.get(m) : null;');

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// ── Phase A: DEGRADED (rich module NOT loaded) ──
let threwA = null, bar = '';
try { bar = renderFinishInfo(MSG, false); } catch (e) { threwA = e; }
check('A_never_throws', !threwA);
if (threwA) out.push('  A error: ' + (threwA && threwA.stack || threwA));
check('A_bar_has_cost_tag', bar.indexOf('cost-tag-detail') !== -1);
check('A_placeholder_empty',
  /<span class="cost-popover-data" hidden><\/span>/.test(bar));
check('A_no_prebuilt_popover', bar.indexOf('cp-rounds') === -1
  && bar.indexOf('cost-popover-inner') === -1);
const _ctx = global.__getCtx(MSG);
check('A_ctx_stashed', !!(_ctx && _ctx.costInfo && _ctx.rounds));

// ── Phase B: RICH ──
// Production: two <script> tags share the global LEXICAL environment, so the
// core's top-level `const _costCtxByMsg` IS visible to the feature bundle.
// node eval() gives each call its OWN lexical scope — mirror production by
// evaluating core+rich in ONE call (fresh scope), then re-render to restash.
const _coreSrc = fs.readFileSync(process.argv[2], 'utf8');
eval(_coreSrc + '\n;\n' + fs.readFileSync(process.argv[3], 'utf8'));
check('B_rich_symbols_present',
  typeof _buildCostPopover === 'function' &&
  typeof _toggleCostPopover === 'function');
bar = renderFinishInfo(MSG, false);   // restash into this scope's WeakMap

// Fake tag element wrapping the (empty) placeholder span.
const placeholder = { _html: '',
  set innerHTML(v) { this._html = v; }, get innerHTML() { return this._html; } };
const tagEl = {
  querySelector: (sel) => sel === '.cost-popover-data' ? placeholder : null,
  closest: () => null,
  getBoundingClientRect: () => ({ left: 10, top: 100, bottom: 120 }),
};
let threwB = null;
try { _toggleCostPopover({ stopPropagation: () => {} }, tagEl); }
catch (e) { threwB = e; }
check('B_toggle_never_throws', !threwB);
if (threwB) out.push('  B error: ' + (threwB && threwB.stack || threwB));
check('B_popover_built_into_placeholder',
  placeholder.innerHTML.length > 100
  && placeholder.innerHTML.indexOf('cp-') !== -1);
const pop = _els.find(e => e.className === 'cost-popover');
check('B_popover_opened', !!pop && pop.innerHTML === placeholder.innerHTML);

console.log(out.join('\n'));
process.exit(0);
"""


def _run(fi_path: str, rich_path: str) -> subprocess.CompletedProcess:
    harness = os.path.join(HERE, '_finish_info_rich_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        return subprocess.run(
            ['node', harness, fi_path, rich_path],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_degraded_and_rich_modes():
    fi = os.path.join(JS_DIR, 'ui', 'finish_info.js')
    rich = os.path.join(JS_DIR, 'ui', 'finish_info_rich.js')
    proc = _run(fi, rich)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    for want in (
        'PASS A_never_throws',
        'PASS A_bar_has_cost_tag',
        'PASS A_placeholder_empty',
        'PASS A_no_prebuilt_popover',
        'PASS A_ctx_stashed',
        'PASS B_rich_symbols_present',
        'PASS B_toggle_never_throws',
        'PASS B_popover_built_into_placeholder',
        'PASS B_popover_opened',
    ):
        assert want in output, f'{want} missing:\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_NC_stash_is_load_bearing(tmp_path):
    """NEUTER: drop the ctx stash on a COPY of the core file → the rich
    toggle finds no ctx and builds nothing (placeholder stays empty).
    Proves the stash (not some incidental state) feeds the lazy build."""
    fi = os.path.join(JS_DIR, 'ui', 'finish_info.js')
    rich = os.path.join(JS_DIR, 'ui', 'finish_info_rich.js')
    with open(fi, encoding='utf-8') as f:
        src = f.read()
    anchor = '_costCtxByMsg.set(msg, {'
    assert anchor in src, 'ctx stash missing — update the neuter target'
    neutered = src.replace(anchor, '/* NEUTERED */ void 0 && _costCtxByMsg.set(msg, {', 1)
    assert neutered != src
    copy = tmp_path / 'finish_info_neutered.js'
    copy.write_text(neutered, encoding='utf-8')
    proc = _run(str(copy), rich)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    assert 'FAIL A_ctx_stashed' in output, (
        'NEUTER did not bite — without the stash the ctx should be absent:\n'
        + output)
    assert 'FAIL B_popover_built_into_placeholder' in output, (
        'NEUTER did not bite — without the stash the popover cannot build:\n'
        + output)
    with open(fi, encoding='utf-8') as f:
        assert f.read() == src, 'shipped finish_info.js mutated by the NC'
