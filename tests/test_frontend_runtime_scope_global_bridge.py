"""tests/test_frontend_runtime_scope_global_bridge.py — the runtimeScope →
globalThis publish seam, guarded at three honest levels.

WHY
---
The 2026-08-14 "turn-ctx not showing" incident: during the classic→Vite ESM
migration, cross-module functions moved from window globals into the
module-private ``runtimeScope`` object (``Object.create(null)``), while ~400
retained vanilla call sites still reach them by BARE identifier
(``typeof X === "function"`` guards + direct calls). An unpublished name
resolves to nothing in the ESM bundle: the guard silently skips — zero
console output, the feature just disappears. 19 symbols / ~40 call sites
were dead at once (turn-ctx rail+fold+capture+reconcile, updateContextBar,
isChatModel, modelGroup*, openProjectBrain, presenceRefresh, ...).

The fix is a single publish seam at the app-runtime module tail.

Why three tests, and why the section harness alone is NOT enough:
  * Section-based jsdom tests are structurally BLIND to this seam:
    ``tests/_runtime_sections.py`` prepends ``var runtimeScope = window`` to
    every extracted section, so in the test view every runtimeScope member
    IS a global — the opposite of the production ESM module. That blindness
    is why the whole incident passed the existing suite.
  * Test A (static)      — the seam text exists and the scanner still sees
                           the load-bearing turn-ctx symbols.
  * Test B (real bundle) — node ESM-imports the SHIPPED vite artifact and
                           asserts every scanner-found seam symbol resolves
                           as a function on globalThis (plus a collision
                           audit: a name already occupied pre-import stays
                           dead under the builtin-wins skip). This is the
                           only check with production module semantics —
                           run it after ``npm run build:frontend``; a stale
                           pre-fix bundle goes red here.
  * Test C (render chain)— real chat_render + info-rail sections, WITHOUT
                           the renderTurnCtxNote _noop stub the autopilot
                           suite installs: renderMessage(msg._ctx) must
                           produce the rail as a DIRECT child of .message
                           (out of flow, the three-track grid) and the fold
                           INSIDE .message-content.
  * NC control           — neutering the bare guard in a copy of the source
                           must flip C's rail/fold assertions red, proving
                           they are load-bearing.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

import pytest

from tests._jsdom import node_deps_available, parse_harness_result
from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
RUNTIME = os.path.join(ROOT, 'frontend', 'src', 'runtime', 'app-runtime.js')
MANIFEST = os.path.join(ROOT, 'static', 'vite', 'manifest.json')


def _read_runtime() -> str:
    with open(RUNTIME, encoding='utf-8') as f:
        return f.read()


def _scan_seam_symbols(src: str) -> list[str]:
    """Symbols exported ONLY via ``runtimeScope.X =`` that the retained
    vanilla code still references by bare ``typeof X === 'function'``.

    These are exactly the names that die silently in the real ESM bundle
    unless the tail publish seam copies them onto globalThis.
    """
    rs_exports = set(re.findall(
        r'runtimeScope\.([A-Za-z_$][A-Za-z0-9_$]*)\s*=', src))
    bare_guards = set(re.findall(
        r"typeof\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*===?\s*['\"]function['\"]",
        src))
    # Bindings visible to bare identifiers at module scope: top-level
    # declarations (any indentation-0 form, incl. `export ...`) and the
    # named-import block at the top of the file.
    toplevel = set(re.findall(
        r'^(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)',
        src, re.M))
    toplevel |= set(re.findall(
        r'^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)',
        src, re.M))
    for m in re.finditer(r'import\s*\{([^}]*)\}', src[:8000]):
        for part in m.group(1).split(','):
            name = part.strip().split(' as ')[-1].strip()
            if re.fullmatch(r'[A-Za-z_$][A-Za-z0-9_$]*', name or ''):
                toplevel.add(name)
    return sorted((bare_guards & rs_exports) - toplevel)


# ═══════════════════════════ Test A — static seam ratchet ═════════════════


def test_publish_seam_present_and_scanner_sees_turn_ctx_family():
    src = _read_runtime()

    # (1) the tail publish loop (single seam, builtin-wins skip)
    assert 'const _globalPublishTarget' in src, (
        'runtimeScope → globalThis publish loop missing from the app-runtime '
        'tail — bare-identifier guards go silently dead again (2026-08-14 '
        'turn-ctx incident)')
    assert 'if (_name in _globalPublishTarget) continue;' in src, (
        'publish loop lost its builtin collision skip')
    assert 'console.warn(\'[runtimeScope] publish skipped for \'' in src, (
        'publish loop lost its poisoned-key resilience (per-key try/catch '
        '+ loud warning — one bad accessor must not kill app boot)')
    # (2) late registrations keep the invariant
    assert 'globalThis)[name] = value;' in src, (
        'setRuntimeService no longer re-publishes — feature-owner '
        'registrations after boot would be invisible to bare guards')

    # (3) the scanner still sees a real seam surface. The 2026-08-14
    # incident set was 19 symbols; a parallel per-callsite migration of the
    # turn-ctx family + 11 more shrank it — the bridge covers whatever
    # remains. If this floor fails because ALL guards were consciously
    # migrated to runtimeScope reads, lower it — never delete the test.
    seam = set(_scan_seam_symbols(src))
    assert len(seam) >= 1, (
        f'seam surface shrank to {len(seam)} symbols ({sorted(seam)}); '
        f'known remaining victims include updateContextBar / modelGroup* / '
        f'applyCapabilityTaxonomy — scanner or source regressed')


# ═══════════════════ Test B — the SHIPPED bundle, real ESM semantics ═══════

_HARNESS_B = r"""
'use strict';
const path = require('path');
const { pathToFileURL } = require('url');
const ROOT = process.argv[3];
const SYMBOLS = JSON.parse(process.env.SEAM_SYMBOLS || '[]');

// App boot fires background promises (feature-flag fetch, chunk preloads);
// in jsdom they legitimately reject. Swallow so node doesn't die before
// the assertions run.
process.on('unhandledRejection', (e) => {
  console.log('NOTE unhandledRejection swallowed: '
    + (e && e.message ? e.message : e));
});
// App init in a bare jsdom env can throw in event dispatch paths (jsdom
// events reaching node EventTargets). The publish seam runs at MODULE
// evaluation — before any init — so assertions on globals stay valid even
// when the app's own boot crashes (it is caught + logged by main.js).
process.on('uncaughtException', (e) => {
  console.log('NOTE uncaughtException swallowed: '
    + (e && e.message ? e.message : e));
});

const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><html><body><div id="chatInner"></div></body></html>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.self = win;

// Browser APIs the bundle touches during eval/boot that node/jsdom lack.
const _mm = () => ({ matches: false, media: '', addEventListener() {},
  removeEventListener() {}, addListener() {}, removeListener() {} });
try { win.matchMedia = win.matchMedia || _mm; } catch (e) { /* readonly */ }
global.matchMedia = win.matchMedia || _mm;
global.IntersectionObserver = win.IntersectionObserver =
  class { observe() {} unobserve() {} disconnect() {} };
global.ResizeObserver = win.ResizeObserver =
  class { observe() {} unobserve() {} disconnect() {} };
global.requestIdleCallback = win.requestIdleCallback = () => 0;
global.requestAnimationFrame = win.requestAnimationFrame = () => 0;
global.localStorage = win.localStorage;
global.sessionStorage = win.sessionStorage;
global.CustomEvent = win.CustomEvent;
global.Event = win.Event;
global.MutationObserver = win.MutationObserver;
global.getComputedStyle = (el, ps) => win.getComputedStyle(el, ps);
global.alert = () => {};
global.confirm = () => false;
global.prompt = () => null;
global.HTMLElement = win.HTMLElement;
global.CSS = win.CSS || (win.CSS = { escape: (s) => String(s) });
for (const p of ['navigator', 'location']) {
  try {
    Object.defineProperty(globalThis, p,
      { value: win[p], configurable: true, writable: true });
  } catch (e) { /* node getter — bundle reads fall through to node's own */ }
}
win.fetch = win.fetch || global.fetch;

// Browser reality: globalThis === window — bare identifiers (Image, Api,
// XMLHttpRequest, …) resolve through it. Node's globalThis lacks every
// jsdom constructor; mirror them across so module-scope bare references in
// the bundle see the same object graph a browser gives them.
for (const _k of Object.getOwnPropertyNames(win)) {
  if (_k in globalThis) continue;
  try { globalThis[_k] = win[_k]; } catch (e) { /* jsdom accessor guard */ }
}
// Dual-homed browser standards (node ships its own AbortController/Signal):
// the bundle's DOM code passes these into jsdom EventTargets, which type-
// check against the WINDOW's classes — jsdom's versions must win.
for (const _k of ['AbortController', 'AbortSignal']) {
  if (win[_k]) { globalThis[_k] = win[_k]; }
}

const before = new Set(Object.getOwnPropertyNames(globalThis));

const out = [];
function check(name, cond, extra) {
  out.push((cond ? 'PASS ' : 'FAIL ') + name + (extra ? ' :: ' + extra : ''));
}

(async () => {
  let importError = '';
  await import(pathToFileURL(process.argv[2]).href).catch((e) => {
    importError = (e && e.name ? e.name + ': ' : '')
      + String((e && e.message) || e) + '\n'
      + String((e && e.stack) || '').slice(0, 600);
  });
  // Let boot microtasks drain; do not wait on app timers (they are neutered
  // or one-shot) — 100ms is plenty for promise chains.
  await new Promise((r) => setTimeout(r, 100));

  check('bundle_imported', !importError,
        importError ? String(importError).slice(0, 700) : '');

  if (!importError) {
    const unresolved = [];
    const collided = [];
    for (const sym of SYMBOLS) {
      const t = typeof globalThis[sym];
      if (t !== 'function') {
        (before.has(sym) ? collided : unresolved).push(sym + ':' + t);
      }
    }
    check('seam_symbols_resolve', unresolved.length === 0,
          'unresolved in the REAL bundle: ' + unresolved.join(', '));
    check('seam_symbols_no_global_collision', collided.length === 0,
          'occupied on globalThis BEFORE import (builtin-wins skip leaves '
          + 'them dead): ' + collided.join(', '));

    // Behavioural smoke against the SHIPPED minified renderTurnCtxNote.
    const r = globalThis.renderTurnCtxNote;
    check('renderTurnCtxNote_is_function', typeof r === 'function');
    if (typeof r === 'function') {
      const cur = r({ model: 'claude-opus-4.8', depth: 'high',
                      modes: [{ label: 'Autopilot', tone: 'mode' }],
                      tools: [{ label: 'Search', tone: 'search' }],
                      roots: [] });
      check('current_shape_rail',
            !!(cur && typeof cur === 'object'
               && String(cur.rail).indexOf('<div class="turn-ctx"') !== -1));
      check('current_shape_fold',
            !!(cur && typeof cur === 'object'
               && String(cur.fold).indexOf('tctx-fold') !== -1));
      const leg = r({ model: 'gpt-5.6',
                      tools: [{ label: 'Search', tone: 'search' },
                              { label: 'Swarm', tone: 'mode' }],
                      roots: [] });
      check('legacy_shape_mode_badge',
            !!(leg && String(leg.rail).indexOf('tctx-mode-badge') !== -1
               && String(leg.rail).indexOf('Swarm') !== -1));
      check('legacy_shape_mode_not_chip',
            !!(leg && String(leg.rail).indexOf('tctx-chip tctx-tone-mode') === -1));
    }
  }

  console.log(out.join('\n'));
  console.log('__JSDOM_RESULT__ '
    + JSON.stringify({ pass: out.filter((l) => l.startsWith('PASS')).length,
                       fail: out.filter((l) => l.startsWith('FAIL')).length }));
  process.exit(0);
})().catch((e) => {
  console.log('FAIL harness_crashed :: ' + ((e && e.stack) || e));
  process.exit(0);
});
"""


# The shipped chunks are ESM with a `.js` extension; the repo root
# package.json has no `"type": "module"`, so node classifies them as CJS
# and the bundle's `import` statement is a syntax error. Force module
# format for files under static/vite/assets/ via a registerHooks loader —
# /tmp-only files, nothing written into the shared build output.
_ESM_FORCE_LOADER = r"""
import { readFileSync } from 'node:fs';
import { registerHooks } from 'node:module';
registerHooks({
  load(url, context, nextLoad) {
    if (url.includes('/static/vite/assets/') && url.endsWith('.js')) {
      return {
        format: 'module',
        source: readFileSync(new URL(url), 'utf8'),
        shortCircuit: true,
      };
    }
    return nextLoad(url, context);
  },
});
"""


def _manifest_main_asset() -> str:
    with open(MANIFEST, encoding='utf-8') as f:
        manifest = json.load(f)
    entry = manifest.get('frontend/src/main.ts') or {}
    rel = entry.get('file')
    assert rel, 'static/vite/manifest.json has no entry for frontend/src/main.ts'
    return os.path.join(ROOT, 'static', 'vite', rel)


@pytest.mark.skipif(not node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_real_bundle_publishes_seam_symbols_to_globalthis():
    symbols = _scan_seam_symbols(_read_runtime())
    assert symbols, 'scanner found no seam symbols — scanner broken?'

    harness = os.path.join(HERE, '_runtime_scope_bridge_harness_b.js')
    loader = os.path.join(HERE, '_runtime_scope_bridge_esm_loader.mjs')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS_B)
    with open(loader, 'w', encoding='utf-8') as f:
        f.write(_ESM_FORCE_LOADER)
    try:
        proc = subprocess.run(
            ['node', '--import', loader, harness,
             _manifest_main_asset(), ROOT],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, 'SEAM_SYMBOLS': json.dumps(symbols)},
        )
    finally:
        for p in (harness, loader):
            try:
                os.remove(p)
            except OSError:
                pass

    output = (proc.stdout or '').strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    npass, nfail, _structured = parse_harness_result(output)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails and nfail == 0, (
        f'real-bundle seam failures (bundle={_manifest_main_asset()}):\n'
        f'{output}\nIf symbols are unresolved, rebuild first: '
        f'npm run build:frontend')
    assert npass >= 8, f'expected >=8 PASS checks, got {npass}:\n{output}'


# ═══════════ Test C — real render chain: rail + fold from msg._ctx ═════════

_CHAT_RENDER = runtime_section_path('ui/chat_render.js')
_ESCAPE_HTML = runtime_section_path('core/escape_html.js')
_SAFE_HTML = runtime_section_path('core/safe_html.js')
_TRANSLATION_MODEL = runtime_section_path('core/translation_model.js')
_TRANSLATION_INDICATOR = runtime_section_path('ui/translation_indicator.js')
_INFO_RAIL = runtime_section_path('info-rail.js')

_HARNESS_C = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[5];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body><div id="chatInner"></div></body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
global.setTimeout = win.setTimeout = () => 0;
global.requestAnimationFrame = win.requestAnimationFrame = () => 0;
win.CSS = global.CSS = { escape: (s) => String(s) };

const out = [];
function check(name, cond, extra) {
  out.push((cond ? 'PASS ' : 'FAIL ') + name + (extra ? ' :: ' + extra : ''));
}

// ── chat_render harness state (mirrors test_frontend_autopilot_flat_render) ──
const _conv = { id: 'c-ctx', messages: [], activeTaskId: null };
win.activeStreams = global.activeStreams = new Map();
win.conversations = global.conversations = [_conv];
win.activeConvId = global.activeConvId = 'c-ctx';
win.getActiveConv = global.getActiveConv = () => _conv;
win.t = global.t = (k) => k;
win._fmtAbsoluteDateTime = global._fmtAbsoluteDateTime = () => '';
win.stripNoTranslateTags = global.stripNoTranslateTags =
  (s) => (s == null ? '' : String(s));
win.renderMarkdown = global.renderMarkdown =
  (s) => '<md>' + String(s == null ? '' : s) + '</md>';
win.getToolRoundsFromMsg = global.getToolRoundsFromMsg =
  (m) => (m && m.toolRounds) || [];
win.renderToolRoundsHTML = global.renderToolRoundsHTML = () => '';
win.renderSegmentTimelineHTML = global.renderSegmentTimelineHTML = () => '';

// NOTE: renderTurnCtxNote is deliberately NOT stubbed here — this suite is
// the one that exercises the REAL info-rail renderer through renderMessage.
const _noop = () => '';
for (const name of [
  'renderMcpLoginHintHtml','renderTurnProvenanceHtml','renderFileChangesBar',
  'renderErrorEnvelope','renderBranchZone','renderPreferenceLearnedHtml',
  'renderFinishInfo','_buildSwarmInboxChipsHTML','_injectAnchoredBranches',
  '_prefetchConvCosts','_prefetchConvFileChanges','_stampFreshness',
  'buildTurnNav','calcCostCny',
]) {
  if (typeof win[name] === 'undefined') { win[name] = global[name] = _noop; }
}
win._USER_AVATAR_SVG = global._USER_AVATAR_SVG = '<img data-avatar="onigiri">';
win._TOFU_WORKER_SVG = global._TOFU_WORKER_SVG = '<img data-avatar="worker">';
win._TOFU_PLANNER_SVG = global._TOFU_PLANNER_SVG = '<img data-avatar="planner">';
win._TOFU_CRITIC_SVG = global._TOFU_CRITIC_SVG = '<img data-avatar="critic">';
win.BASE_PATH = global.BASE_PATH = '';
win._INITIAL_RENDER = global._INITIAL_RENDER = 20;

// ── info-rail harness state (mirrors test_frontend_turn_ctx_fact_card) ──
win.projectState = global.projectState = { active: false, path: '' };
win.searchMode = global.searchMode = 'off';
win.fetchEnabled = global.fetchEnabled = false;
win.browserEnabled = global.browserEnabled = false;
win.desktopEnabled = global.desktopEnabled = false;
win.codeExecEnabled = global.codeExecEnabled = false;
win.memoryEnabled = global.memoryEnabled = false;
win.imageGenEnabled = global.imageGenEnabled = false;
win.humanGuidanceEnabled = global.humanGuidanceEnabled = false;
win.autoTranslate = global.autoTranslate = false;
win.endpointEnabled = global.endpointEnabled = false;
win.autopilotEnabled = global.autopilotEnabled = false;
win.swarmEnabled = global.swarmEnabled = false;
win.activeFlow = global.activeFlow = '';
win.config = global.config = { model: '', thinkingDepth: '' };
win.serverModel = global.serverModel = '';
win._isThinkingCapable = global._isThinkingCapable = () => false;
win._modelShortName = global._modelShortName = (m) => m;
win._detectBrand = global._detectBrand = () => 'generic';
win._brandSvg = global._brandSvg = () => '';
win.Api = global.Api = { mcp: { toolsList: async () => ({ ok: false }) } };

// ── load the real sections ──
(0, eval)(fs.readFileSync(process.argv[3], 'utf8'));   // core/escape_html.js
(0, eval)(fs.readFileSync(process.argv[4], 'utf8'));   // core/safe_html.js
(0, eval)(fs.readFileSync(process.argv[7], 'utf8'));   // core/translation_model.js
(0, eval)(fs.readFileSync(process.argv[8], 'utf8'));   // ui/translation_indicator.js
(0, eval)(fs.readFileSync(process.argv[9], 'utf8'));   // info-rail.js (REAL)
const CHAT = fs.readFileSync(process.argv[2], 'utf8');
const NC = process.argv[6] || '';
let chatSrc = CHAT;
if (NC === 'nc_guard') {
  // Neuter the render seam the way the 2026-08-14 incident broke it: the
  // looked-up name resolves to nothing, guard silently skips. Regex form:
  // the call site migrated from bare `renderTurnCtxNote` to
  // `runtimeScope.renderTurnCtxNote` mid-2026-08; match either shape.
  chatSrc = CHAT.replace(
    /typeof\s+(?:runtimeScope\.)?renderTurnCtxNote\s*===\s*"function"/,
    'typeof renderTurnCtxNote_missing === "function"');
}
check('nc_pattern_applied', NC === '' || chatSrc !== CHAT);
(0, eval)(chatSrc);                                    // ui/chat_render.js

// The section prelude binds runtimeScope to `window` — here the jsdom win,
// not node's globalThis — so runtimeScope exports land on win while the bare
// guards inside chat_render resolve on globalThis. Replay the production
// publish seam for the section view: copy the exported functions across.
for (const _k of ['renderTurnCtxNote', 'buildTurnCtxSnapshot',
                  'reconcileTurnCtxCapsule']) {
  if (win[_k] && typeof global[_k] === 'undefined') global[_k] = win[_k];
}

if (typeof renderMessage !== 'function') {
  console.log('FAIL fn_exposed renderMessage missing'); process.exit(0);
}
if (typeof renderTurnCtxNote !== 'function') {
  console.log('FAIL fn_exposed renderTurnCtxNote missing'); process.exit(0);
}
check('fn_exposed', true);

function mkMsg(ctx) {
  return { role: 'user', _msgId: 'u1', content: 'hello there', _ctx: ctx };
}
function fragOf(html) {
  const frag = win.document.createElement('div');
  frag.innerHTML = html;
  return frag;
}

// ══ current snapshot shape: rail + fold, correctly parented ══
{
  const html = renderMessage(mkMsg({
    model: 'claude-opus-4.8', depth: 'high',
    modes: [{ label: 'Autopilot', tone: 'mode' }],
    tools: [{ label: 'Search', tone: 'search' }, { label: 'Code', tone: 'code' }],
    roots: [{ path: '/mnt/x/brain/b/c/workspace', short: 'b/c/workspace', ro: false }],
  }), 0);
  const frag = fragOf(html);
  check('c_msg_rendered', !!frag.querySelector('.message'));

  // Rail: DIRECT child of .message (out of flow — the third track).
  check('c_rail_direct_child_of_message', !!frag.querySelector('.message > .turn-ctx'));
  check('c_rail_not_inside_content', !frag.querySelector('.message-content .turn-ctx'));
  // Fold: INSIDE .message-content (never a .message child — the zero-width
  // track swallowed it there in the 2026-08-03 report).
  check('c_fold_inside_content', !!frag.querySelector('.message-content > .tctx-fold'));
  check('c_fold_not_direct_child_of_message', !frag.querySelector('.message > .tctx-fold'));

  const rail = frag.querySelector('.message > .turn-ctx');
  check('c_rail_model_chip', !!(rail && rail.querySelector('.tctx-model')));
  check('c_rail_mode_badge',
        !!(rail && rail.textContent.indexOf('Autopilot') !== -1));
  check('c_rail_tool_chip_search',
        !!(rail && rail.querySelector('.tctx-chip.tctx-tone-search')));
  const p = rail && rail.querySelector('.tctx-path');
  check('c_rail_path_title_full',
        !!(p && p.getAttribute('title') === '/mnt/x/brain/b/c/workspace'));
  const fold = frag.querySelector('.message-content > .tctx-fold');
  const ftxt = fold ? fold.textContent : '';
  check('c_fold_summary_counts',
        ftxt.indexOf('2 tools') !== -1 && ftxt.indexOf('1 ws') !== -1);
}

// ══ legacy snapshot shape: modes embedded as tone:'mode' tools ══
{
  const frag = fragOf(renderMessage(mkMsg({
    model: 'gpt-5.6',
    tools: [{ label: 'Search', tone: 'search' }, { label: 'Swarm', tone: 'mode' }],
    roots: [],
  }), 1));
  const rail = frag.querySelector('.message > .turn-ctx');
  check('c_legacy_rail', !!rail);
  check('c_legacy_mode_badge_recovered',
        !!(rail && rail.querySelector('.tctx-mode-badge')
           && rail.textContent.indexOf('Swarm') !== -1));
  check('c_legacy_mode_not_a_chip',
        !!(rail && !rail.querySelector('.tctx-chip.tctx-tone-mode')));
}

// ══ empty snapshot / no snapshot: no zero-width ghosts ══
{
  const frag = fragOf(renderMessage(
    mkMsg({ model: '', tools: [], modes: [], roots: [] }), 2));
  check('c_empty_no_rail', !frag.querySelector('.turn-ctx'));
  check('c_empty_no_fold', !frag.querySelector('.tctx-fold'));
}
{
  const frag = fragOf(renderMessage(
    { role: 'user', _msgId: 'u2', content: 'plain' }, 3));
  check('c_noctx_no_rail', !frag.querySelector('.turn-ctx'));
  check('c_noctx_no_fold', !frag.querySelector('.tctx-fold'));
}

console.log(out.join('\n'));
console.log('__JSDOM_RESULT__ '
  + JSON.stringify({ pass: out.filter((l) => l.startsWith('PASS')).length,
                     fail: out.filter((l) => l.startsWith('FAIL')).length }));
process.exit(0);
"""


def _run_c(nc: str = '') -> str:
    harness = os.path.join(HERE, '_runtime_scope_bridge_harness_c.js')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS_C)
    try:
        proc = subprocess.run(
            ['node', harness,
             _CHAT_RENDER,       # argv[2]
             _ESCAPE_HTML,       # argv[3]
             _SAFE_HTML,         # argv[4]
             ROOT,               # argv[5]
             nc,                 # argv[6]
             _TRANSLATION_MODEL,   # argv[7]
             _TRANSLATION_INDICATOR,  # argv[8]
             _INFO_RAIL,         # argv[9]
             ],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = (proc.stdout or '').strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


@pytest.mark.skipif(not node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_render_message_produces_turn_ctx_rail_and_fold():
    output = _run_c('')
    npass, nfail, _structured = parse_harness_result(output)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails and nfail == 0, f'turn-ctx render-chain failures:\n{output}'
    assert npass >= 17, f'expected >=17 PASS checks, got {npass}:\n{output}'


@pytest.mark.skipif(not node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_nc_neutered_guard_flips_rail_and_fold_red():
    """NC: breaking the bare-identifier guard (the exact 2026-08-14 failure
    mode) must flip the rail AND fold assertions red — proves they are
    load-bearing, not vacuous."""
    output = _run_c('nc_guard')
    assert 'PASS nc_pattern_applied' in output, f'NC mutation did not apply:\n{output}'
    assert 'FAIL c_rail_direct_child_of_message' in output, (
        f'neutered guard did NOT kill the rail — assertion not '
        f'load-bearing:\n{output}')
    assert 'FAIL c_fold_inside_content' in output, (
        f'neutered guard did NOT kill the fold — assertion not '
        f'load-bearing:\n{output}')


if __name__ == '__main__':
    if not node_deps_available():
        print('SKIP — node + jsdom not available')
    else:
        test_publish_seam_present_and_scanner_sees_turn_ctx_family()
        test_real_bundle_publishes_seam_symbols_to_globalthis()
        test_render_message_produces_turn_ctx_rail_and_fold()
        test_nc_neutered_guard_flips_rail_and_fold_red()
        print('PASS test_frontend_runtime_scope_global_bridge')
