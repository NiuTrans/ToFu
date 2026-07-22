"""jsdom regression for the SHADOW-KEY cache-invalidation bug in the live
seg-prose / narration painters.

WHY
---
Four live-streaming painters skip a repaint by comparing a SHADOW JS PROPERTY
(`thinkEl._lastThink`, `narrEl._lastNarr`, `narr._lastZh`, `*._lastPartial`)
instead of the DOM (the actual source of truth for the display layer). The
top-level thinking painter, by contrast, keys its skip on the DOM value
(`textEl.textContent !== msg.thinking`) and therefore SELF-HEALS a clobbered
node on the next frame.

The asymmetry is the "持续到刷新才好" root cause: if ANY transient writer (an
rAF-vs-poll race, a competing second painter, a node reused across rounds)
dirties one of these nodes WITHOUT moving its shadow key, the equality skip
pins the dirty content until a full rebuild — which only happens on page reload
(→ renderSegmentTimelineHTML). The model value is clean the whole time; only the
live projection is stale. That is a real, independent cache-invalidation defect.

THE FIX (root cause, not a band-aid)
------------------------------------
Mirror the top-level painter's self-heal: on the skip path, ALSO verify the DOM
still equals what we last wrote. Cheap — a single string compare of
textContent / innerHTML, NO re-render on the hot path — the expensive
renderMarkdown only re-runs when the DOM actually drifted.

This harness drives the REAL shipped painters under jsdom. It dirties a node the
way a real competing writer would (an external innerHTML/textContent clobber
that leaves the shadow key stale), then feeds a subsequent frame whose MODEL
VALUE IS EQUAL to the last one, and asserts the painter RE-SYNCS the DOM back to
the model value (before the fix: stays dirty; after: healed).

NEUTER: strip the `|| node.<domprop> !== <shadowHtml>` self-heal clause → the
skip reverts to shadow-key-only → the dirty node is NOT corrected → the heal
assertion fails, proving the clause is load-bearing.

Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')
STREAMING_UI = os.path.join(JS_DIR, 'ui', 'streaming_ui.js')
TRANSLATION_RENDER = os.path.join(JS_DIR, 'ui', 'translation_render.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2];
const NEUTER = process.argv[5] || 'none';
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="chatInner"></div></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;
global.setInterval = win.setInterval = () => 0;
global.setTimeout = win.setTimeout = (fn) => 0;
global.requestAnimationFrame = win.requestAnimationFrame = (fn) => 0;
global.getSelection = win.getSelection = () => ({ isCollapsed: true, rangeCount: 0 });
globalThis.escapeHtml = win.escapeHtml = (s)=>String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
globalThis.renderMarkdown = win.renderMarkdown = (s)=>'<p>'+globalThis.escapeHtml(s)+'</p>';
globalThis.stripNoTranslateTags = win.stripNoTranslateTags = (s)=>s;
globalThis.t = win.t = (k)=>k;
win.isNearBottom=global.isNearBottom=()=>false; win.scrollToBottom=global.scrollToBottom=()=>{};
win._fcFingerprint=global._fcFingerprint=()=>0;
win._extractFileChangesFromRoundsAsync=global._extractFileChangesFromRoundsAsync=()=>({then:()=>{}});
win._renderFileChangesHtml=global._renderFileChangesHtml=()=>'';
win.renderMcpLoginHintHtml=global.renderMcpLoginHintHtml=()=>'';
win.renderPreferenceLearnedHtml=global.renderPreferenceLearnedHtml=()=>'';
win.renderTurnProvenanceHtml=global.renderTurnProvenanceHtml=()=>'';
win._isRoundSwarm=global._isRoundSwarm=(r)=>!!(r&&r._swarm);
win._renderUnifiedToolLine=global._renderUnifiedToolLine=(r)=>'<div class="ptool-line" data-prn="'+(r&&r.roundNum)+'"></div>';
win._renderToolSlot=global._renderToolSlot=(r)=>'<div class="ptool-line" data-prn="'+(r&&r.roundNum)+'"></div>';
win._buildSwarmPanelHTML=global._buildSwarmPanelHTML=()=>'<div class="sw-panel"></div>';
win._buildSwarmInboxChipsHTML=global._buildSwarmInboxChipsHTML=()=>'';
win._renderTurnHead=global._renderTurnHead=()=>''; win._renderSoloRoundTag=global._renderSoloRoundTag=()=>'';
win._turnLabelText=global._turnLabelText=()=>''; win.Icon=global.Icon=()=>'';
win.CSS=global.CSS=undefined;
win.activeStreams=global.activeStreams=new Map(); win.streamBufs=global.streamBufs=new Map();
win.conversations=global.conversations=[]; global.activeConvId=win.activeConvId='c1';

let SUI = fs.readFileSync(process.argv[3], 'utf8');
let TR = fs.readFileSync(process.argv[4], 'utf8');
if (NEUTER === 'think') {
  SUI = SUI.replace('thinkEl._lastThink !== _think || _txtEl.textContent !== _think',
                    'thinkEl._lastThink !== _think /* NEUTER */');
  if (SUI.indexOf('/* NEUTER */') < 0) { console.log('FAIL neuter_applied_think'); process.exit(0); }
}
if (NEUTER === 'narr') {
  SUI = SUI.replace('narrEl._lastNarr !== _narr || narrEl.innerHTML !== narrEl._lastNarrHtml',
                    'narrEl._lastNarr !== _narr /* NEUTER */');
  if (SUI.indexOf('/* NEUTER */') < 0) { console.log('FAIL neuter_applied_narr'); process.exit(0); }
}
if (NEUTER === 'zh') {
  TR = TR.replace('narr._lastZh !== zh || narr.innerHTML !== narr._lastZhHtml',
                  'narr._lastZh !== zh /* NEUTER */');
  if (TR.indexOf('/* NEUTER */') < 0) { console.log('FAIL neuter_applied_zh'); process.exit(0); }
}
(0, eval)(SUI);
(0, eval)(TR);

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

function freshBody(){
  document.getElementById('chatInner').innerHTML =
    '<div class="message" id="streaming-msg" data-msg-id="m1"><div class="message-body" id="streaming-body"></div></div>';
}
function q(sel){ const b=document.getElementById('streaming-body'); const e=b&&b.querySelector(sel); return e?e.textContent:'<none>'; }

if (typeof updateStreamingUI !== 'function') { console.log('FAIL updateStreamingUI_exposed'); process.exit(0); }
if (typeof _renderStreamingTranslatePreview !== 'function') { console.log('FAIL translate_preview_exposed'); process.exit(0); }

// ── CASE 1: seg-thinking self-heal ──
// Paint round1 thinking → dirty the .thinking-text node externally (a competing
// writer leaves _lastThink stale) → re-render with the SAME model value → the
// self-heal must re-sync the DOM.
freshBody();
const r1 = {roundNum:1,llmRound:1,toolName:'read_files',toolCallId:'tc1',status:'done',
  thinking:'The guardrail is the right ask.', assistantContent:'Right — protects this.'};
updateStreamingUI({content:'',thinking:'',toolRounds:[r1],phase:null});
check('c1_painted_clean', q('.seg-thinking .thinking-text') === 'The guardrail is the right ask.');
// External clobber (simulates any writer that dirtied the node w/o moving _lastThink):
(function(){ const e=document.getElementById('streaming-body').querySelector('.seg-thinking .thinking-text'); e.textContent = 'Right' + e.textContent; })();
check('c1_dirtied', q('.seg-thinking .thinking-text').indexOf('Right') === 0);
// A LATER frame: round1's prose is byte-identical, but a 2nd round arrives so
// _syncToolRoundsDOM's fingerprint changes and the per-round painter re-runs
// for round1 (the production reality — later frames always move the fp). The
// _lastThink guard then decides skip-vs-heal for round1's unchanged prose.
const r2 = {roundNum:2,llmRound:2,toolName:'grep_search',toolCallId:'tc2',status:'searching'};
updateStreamingUI({content:'',thinking:'',toolRounds:[r1,r2],phase:null});
check('c1_self_healed', q('.seg-thinking .thinking-text') === 'The guardrail is the right ask.');

// ── CASE 2: english narration self-heal ──
freshBody();
updateStreamingUI({content:'',thinking:'',toolRounds:[r1],phase:null});
check('c2_narr_clean', q('.stream-seg-en-narration') === 'Right — protects this.');
(function(){ const e=document.getElementById('streaming-body').querySelector('.stream-seg-en-narration'); e.innerHTML = '<p>DIRTY</p>'; })();
check('c2_narr_dirtied', q('.stream-seg-en-narration') === 'DIRTY');
const r2b = {roundNum:2,llmRound:2,toolName:'grep_search',toolCallId:'tc2',status:'searching'};
updateStreamingUI({content:'',thinking:'',toolRounds:[r1,r2b],phase:null});
check('c2_narr_self_healed', q('.stream-seg-en-narration') === 'Right — protects this.');

// ── CASE 3: chinese translation narration self-heal ──
freshBody();
updateStreamingUI({content:'',thinking:'',toolRounds:[r1],phase:null});
_renderStreamingTranslatePreview('c1','m1','中文', {'1':'护栏是对的要求。'});
check('c3_zh_clean', q('.stream-seg-narration') === '护栏是对的要求。');
(function(){ const e=document.getElementById('streaming-body').querySelector('.stream-seg-narration'); e.innerHTML = '<p>Right脏了</p>'; })();
check('c3_zh_dirtied', q('.stream-seg-narration').indexOf('Right') === 0);
_renderStreamingTranslatePreview('c1','m1','中文', {'1':'护栏是对的要求。'});
check('c3_zh_self_healed', q('.stream-seg-narration') === '护栏是对的要求。');

console.log(out.join('\n'));
"""


def _run_node(neuter: str = 'none') -> str:
    with tempfile.NamedTemporaryFile('w', suffix='.js', dir=HERE,
                                     delete=False, encoding='utf-8') as fh:
        hp = fh.name
        fh.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', hp, ROOT, STREAMING_UI, TRANSLATION_RENDER, neuter],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(hp)
        except OSError:
            pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return (proc.stdout or '').strip()


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_seg_prose_guards_self_heal():
    output = _run_node('none')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'seg-prose self-heal failures:\n' + output
    for name in ('c1_self_healed', 'c2_narr_self_healed', 'c3_zh_self_healed'):
        assert f'PASS {name}' in output, output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_think_self_heal_is_load_bearing():
    """Neuter the seg-thinking DOM-drift clause → the dirtied node is not
    corrected on the same-value frame → self-heal assertion fails."""
    output = _run_node('think')
    assert 'FAIL c1_self_healed' in output, (
        'neutering the seg-thinking self-heal did NOT leave the node dirty:\n' + output)


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_narr_self_heal_is_load_bearing():
    output = _run_node('narr')
    assert 'FAIL c2_narr_self_healed' in output, (
        'neutering the english-narration self-heal did NOT leave the node dirty:\n' + output)


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_zh_self_heal_is_load_bearing():
    output = _run_node('zh')
    assert 'FAIL c3_zh_self_healed' in output, (
        'neutering the chinese-narration self-heal did NOT leave the node dirty:\n' + output)


def test_source_carries_self_heal_clauses():
    """The shipped source must contain the DOM-drift self-heal clauses so this
    regression rots with the code, not just the harness."""
    with open(STREAMING_UI, encoding='utf-8') as f:
        sui = f.read()
    with open(TRANSLATION_RENDER, encoding='utf-8') as f:
        tr = f.read()
    assert '_txtEl.textContent !== _think' in sui, 'seg-thinking self-heal clause missing'
    assert 'narrEl.innerHTML !== narrEl._lastNarrHtml' in sui, 'english-narration self-heal clause missing'
    assert 'narr.innerHTML !== narr._lastZhHtml' in tr, 'chinese-narration self-heal clause missing'
