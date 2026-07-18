"""Regression test: a FINISHED, untranslated assistant message gets
auto-translated when the conversation is next opened IF the live global
auto-translate toggle is ON — even when that conversation was frozen
``autoTranslate=OFF`` at send-time.

WHY
---
The per-conversation ``autoTranslate`` flag is FROZEN at send-time so a mid-task
toggle can't change an in-flight run (the cross-talk fix — see
``.tofu/skills/finishstream-global-autotranslate-bug.md``). But that freeze used
to ALSO veto the on-open retro-translate forever: a conversation started while
the toggle was OFF could never be auto-translated even after the user turned the
global toggle ON, so an already-generated reply sat there demanding a manual
"Translate" click (the reported bug).

The fix introduces ``convAutoTranslateEffective(conv)`` — the LIVE global toggle
wins when ON; otherwise the frozen per-conv value (an explicit per-conv ON still
honored). ``_resumePendingTranslations`` Phase-0 (the on-open / on-activate
trigger, already wired into ``loadConversation``) now reads the effective
resolver instead of the frozen ``convAutoTranslate``. The streaming early-return
+ the unified pipeline's claim guard still prevent double-firing against an
in-flight run / the server safety-net.

This drives the REAL shipped ``_resumePendingTranslations`` under node, stubbing
only the network/render seams, and observes whether it kicked
``_runTranslationPipeline(mode='auto')`` for the untranslated tail message.

Endpoint/autopilot routing is NOT exercised here (those modes never reach this
frontend resume path — they translate server-side); the backend incremental
gate's endpoint/autopilot exclusion is covered by
``tests/test_incremental_translate.py::test_gate_excludes_endpoint_and_autopilot``.

Runs the REAL shipped JS under node; skips cleanly when node isn't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


_HARNESS = r"""
const fs = require('fs');
global.window = global;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// ── Stub every global the resume path + pipeline touch BEFORE eval'ing the
//    real modules (they declare top-level fns; the IIFE at translation.js's
//    bottom wires a push subscriber against pushSubscribe). ──
global.pushSubscribe = () => {};
global.activeConvId = 'c1';
global.activeStreams = new Map();            // empty → not streaming
global.saveConversations = () => {};
global.renderChat = () => {};
global._renderMsgInPlace = () => {};
global._renderStreamingTranslatePreview = () => false;
global._applyTranslationStatus = () => {};
global._armAutoTranslateWatchdog = () => {};
global.errorEnvelopeMessage = () => '';
global._isStalePartialTranslation = () => false;   // not stale in these cases
global._patchTranslateLoadingDom = () => {};
global._tryRecoverFromServer = async () => false;
global._pollTranslateTaskBatch = async () => [];

// core/conversations.js — defines convAutoTranslate + convAutoTranslateEffective
eval(fs.readFileSync(process.argv[2], 'utf8'));
// core/translation_model.js — the REAL needsTranslation/readTranslation the
// resume sweep decision now depends on (must load before translation.js).
eval(fs.readFileSync(process.argv[4], 'utf8'));
// translation.js — defines _resumePendingTranslations (+ registers push sub)
eval(fs.readFileSync(process.argv[3], 'utf8'));

// Record every _runTranslationPipeline kick so we can assert WHICH message
// (and mode) the resume path decided to translate. MUST be assigned AFTER the
// evals: translation.js declares `_runTranslationPipeline` as a top-level
// `function`, which would otherwise clobber a pre-eval stub (and then run the
// real pipeline → unstubbed Api). Reassigning the global binding here makes the
// resume path call OUR counter instead.
let _pipelineCalls = [];
_runTranslationPipeline = (conv, idx, msg, opts) => {
  _pipelineCalls.push({ convId: conv && conv.id, idx, mode: opts && opts.mode,
                        field: opts && opts.field });
};

if (typeof convAutoTranslateEffective !== 'function') {
  console.log('FAIL effective_resolver_exposed convAutoTranslateEffective missing');
  console.log(out.join('\n')); process.exit(0);
}
check('effective_resolver_exposed', true);
if (typeof _resumePendingTranslations !== 'function') {
  console.log('FAIL resume_exposed _resumePendingTranslations missing');
  console.log(out.join('\n')); process.exit(0);
}
check('resume_exposed', true);

// ── Pure resolver semantics (the keystone of the fix) ──
global.autoTranslate = true;
check('eff_global_on_overrides_frozen_off',
  convAutoTranslateEffective({ autoTranslate: false }) === true);
global.autoTranslate = false;
check('eff_global_off_falls_back_to_frozen_on',
  convAutoTranslateEffective({ autoTranslate: true }) === true);
check('eff_global_off_frozen_off_stays_off',
  convAutoTranslateEffective({ autoTranslate: false }) === false);
delete global.autoTranslate;
check('eff_all_absent_off',
  convAutoTranslateEffective({}) === false);

// A finished, untranslated assistant tail message.
function freshConv(convAuto) {
  const conv = {
    id: 'c1',
    messages: [
      { role: 'user', content: '问题', _msgId: 'mU' },
      { role: 'assistant', content: 'A finished English reply.', _msgId: 'mA' },
    ],
  };
  if (convAuto !== undefined) conv.autoTranslate = convAuto;   // frozen value
  return conv;
}
function load(conv) { global.conversations = [conv]; }

// ── CASE 1 (the bug): conv frozen autoTranslate=OFF, global toggle ON now.
//    Opening it MUST kick an auto-translate for the untranslated tail. ──
async function case1() {
  _pipelineCalls = [];
  global.autoTranslate = true;            // user turned the global toggle ON
  const conv = freshConv(false);          // …but this conv was frozen OFF
  load(conv);
  await _resumePendingTranslations('c1');
  const hit = _pipelineCalls.find(c => c.idx === 1 && c.mode === 'auto'
                                        && c.field === 'translatedContent');
  check('case1_frozen_off_global_on_translates', !!hit);
}

// ── CASE 2: global toggle OFF and conv frozen OFF → NO auto-translate
//    (the user hasn't asked for it anywhere). ──
async function case2() {
  _pipelineCalls = [];
  global.autoTranslate = false;
  const conv = freshConv(false);
  load(conv);
  await _resumePendingTranslations('c1');
  check('case2_all_off_no_translate', _pipelineCalls.length === 0);
}

// ── CASE 3: already-translated tail → never re-translate, regardless of
//    toggle (the Phase-0 break on translatedContent present). ──
async function case3() {
  _pipelineCalls = [];
  global.autoTranslate = true;
  const conv = freshConv(false);
  conv.messages[1].translatedContent = '已翻译';   // already done
  load(conv);
  await _resumePendingTranslations('c1');
  check('case3_already_translated_skipped', _pipelineCalls.length === 0);
}

// ── CASE 4: a task is STILL streaming for this conv → resume must NOT fire
//    (the in-flight freeze is respected; only finished messages retro-xlate). ──
async function case4() {
  _pipelineCalls = [];
  global.autoTranslate = true;
  const conv = freshConv(false);
  load(conv);
  global.activeStreams = new Map([['c1', { taskId: 't-live' }]]);
  await _resumePendingTranslations('c1');
  global.activeStreams = new Map();       // reset
  check('case4_streaming_blocks_retro_translate', _pipelineCalls.length === 0);
}

// ── CASE 5: conv frozen ON but global OFF → still translates (explicit
//    per-conv opt-in is honored by the effective resolver). ──
async function case5() {
  _pipelineCalls = [];
  global.autoTranslate = false;
  const conv = freshConv(true);           // frozen ON
  load(conv);
  await _resumePendingTranslations('c1');
  const hit = _pipelineCalls.find(c => c.idx === 1 && c.mode === 'auto');
  check('case5_frozen_on_global_off_translates', !!hit);
}

(async () => {
  await case1();
  await case2();
  await case3();
  await case4();
  await case5();
  console.log(out.join('\n'));
})();
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_frontend_retro_autotranslate_honors_live_toggle():
    harness = os.path.join(HERE, '_retro_autotranslate_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'core', 'conversations.js'),  # argv[2]
             os.path.join(JS_DIR, 'translation.js'),            # argv[3]
             os.path.join(JS_DIR, 'core', 'translation_model.js'),  # argv[4]
             ],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'retro auto-translate failures:\n' + output
    assert output.count('PASS') >= 11, f'expected >=11 PASS lines, got:\n{output}'
