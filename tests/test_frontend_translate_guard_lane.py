"""Regression: a server-committed translation reaches the OPEN conversation even
when one of the three frame guards in ``_onConvNotifyPush`` fires — driven from
the REAL entry point, not from the middle of the pipeline.

WHY THIS FILE EXISTS SEPARATELY FROM test_frontend_translate_notify_adopt.py
---------------------------------------------------------------------------
The first fix made ``_verifyActiveConvFromServer`` adopt translations, and its
tests called that function DIRECTLY. That passed while the feature was still
broken in practice, because a ``conv_changed`` frame must first survive three
guards in ``_onConvNotifyPush`` — and translations arrive exactly when those
guards are hot:

  * ``activeStreams.has(convId)``      → frame dropped
  * ``_editingMsgIdx !== null``        → frame dropped
  * ``_localWriteAt`` within 6000 ms   → frame dropped (self-echo)

(2026-08-05, conv msebjymx5b4a25: the live-stream guard now diverts to the
WIDER ``_streamActiveVerify`` lane — translations plus the server-injected
settled-prefix adoption for brain/peer/queue kickoffs; the editing and
self-echo guards still divert to ``_translationOnlyVerify``. All lanes run
the same byte-equality-guarded translation reducer, so the guard-integrity
property pinned here is unchanged.)

Auto-translate commits right after a turn settles, which is precisely when this
device just PUT the conv (``conversations.js`` sets ``_localWriteAt`` in
``syncConversationToServer``) and when the stream may not yet be removed from
``activeStreams``. The rev channel has NO replay, so a dropped frame is gone →
the user is back to pressing refresh.

THE GUARDS ARE CORRECT AND ARE NOT WEAKENED
-------------------------------------------
All three exist to stop a server snapshot from OVERWRITING content this device
holds more recently (a live streaming bubble, an in-progress edit, an unacked
PUT). They are written for *destructive* adoption. A translation merge is not
destructive: ``_mergeTranslationFields`` is strictly additive and refuses any
message whose content is not BYTE-EQUAL to the server's.

So instead of loosening a guard, the fix opens a NARROW lane
(``_translationOnlyVerify``) that the guards divert to: it fetches the server
body and runs ONLY the translation reducer — never content, thinking or
toolRounds — and repaints per-message via ``ConvView.applyMessage`` rather than
``replaceAll``, so a live streaming bubble is never rebuilt.

The safety property is carried by the reducer's byte-equality guard, which is
what makes this sound: while a turn is streaming, the local tail content differs
from the server's, so the identity guard refuses that message outright. Only
already-settled turns can gain a 译文 through this lane.

TESTS (all driven through ``_onConvNotifyPush``)
-----------------------------------------------
  A. the three guard scenarios each deliver the translation
  B. GUARD INTEGRITY — the lane must still refuse content/thinking adoption,
     must not rebuild the streaming bubble, and must not repaint a message that
     is being edited. This is the test that would catch "opened the lane by
     weakening the guard".
  C. background conv: the frame marks it stale AND the reopen path is wired to
     the shared reducer, so reopening needs no second action.
  D. NEUTER: remove the lane dispatch → the guard scenarios go dark again.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

from tests._jsdom import JS_DIR, ROOT

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
CTS = os.path.join(JS_DIR, 'core', 'cross_tab_sync.js')
REDUCERS = os.path.join(JS_DIR, 'core', 'conv_reducers.js')
STATE_REDUCER = os.path.join(JS_DIR, 'core', 'conv_state_reducer.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


# The harness loads the REAL cross_tab_sync.js + conv_reducers.js and drives
# _onConvNotifyPush. Every collaborator is stubbed; repaint calls are counted
# per-API so we can tell a surgical per-message repaint from a full rebuild.
_HARNESS = r"""
const fs = require('fs');
global.window = global;
global.addEventListener = () => {};
global.document = { addEventListener: () => {}, visibilityState: 'visible',
                    getElementById: () => null };
global.activeStreams = new Map();
global._editingMsgIdx = null;
global.debugLog = () => {};
global.config = {};
global._applySettingsToConv = () => {};
global.saveConversations = () => {};
global.ConvCache = { put: (c) => { cachePuts.push(c && c.id); } };
global.AbortSignal = { timeout: () => undefined };
global.renderConversationList = () => {};
global.updateSendButton = () => {};
global.applyRunningTaskIdsFrame = () => {};
global._dispatchableQueueCount = () => 0;
global._restoreConvToolState = () => {};
global.reconnectToTask = () => {};

let cachePuts = [];
let replaceAllCalls = [];
let applyMsgCalls = [];
global.ConvView = {
  replaceAll: (id) => { replaceAllCalls.push(id); },
  applyMessage: (id, msg, opts) => {
    applyMsgCalls.push({ id, idx: opts && opts.idx });
    return true;
  },
};

let getCalls = 0;
let SERVER = null;
global.Api = { conversations: { get: async () => { getCalls++;
  return JSON.parse(JSON.stringify(SERVER)); } } };

global.conversations = [];
eval(fs.readFileSync(process.argv[2], 'utf8'));   // REAL conv_reducers.js
eval(fs.readFileSync(process.argv[4], 'utf8'));   // REAL conv_state_reducer.js
eval(fs.readFileSync(process.argv[3], 'utf8'));   // REAL cross_tab_sync.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* A settled two-turn conv. msg[1] is an OLD settled assistant turn that the
 * server has just translated. msg[2] is the RECENT turn. */
function seed(extra) {
  cachePuts = []; replaceAllCalls = []; applyMsgCalls = []; getCalls = 0;
  global.conversations = [Object.assign({
    id: 'c1', _serverRev: 8, updatedAt: 1000,
    messages: [
      { role: 'user', content: 'q', _msgId: 'm0' },
      { role: 'assistant', content: 'First settled answer.', thinking: '',
        toolRounds: [], finishReason: 'stop', _msgId: 'm1' },
      { role: 'assistant', content: 'Second settled answer.', thinking: 'th',
        toolRounds: [], finishReason: 'stop', _msgId: 'm2',
        segments: [{ type: 'text', llmRound: 0 }] },
    ],
  }, extra || {})];
  /* Server truth: SAME content everywhere (a translate commit grows nothing),
   * both assistant turns now carry 译文. */
  SERVER = { id: 'c1', rev: 9, updatedAt: 2000, settings: {},
    messages: [
      { role: 'user', content: 'q', _msgId: 'm0' },
      { role: 'assistant', content: 'First settled answer.', thinking: '',
        toolRounds: [], finishReason: 'stop', _msgId: 'm1',
        translatedContent: '第一个已完成回答。' },
      { role: 'assistant', content: 'Second settled answer.', thinking: 'th',
        toolRounds: [], finishReason: 'stop', _msgId: 'm2',
        translatedContent: '第二个已完成回答。',
        segments: [{ type: 'text', llmRound: 0, translatedText: '旁白中文' }] },
    ] };
}
const zh1 = () => conversations[0].messages[1].translatedContent;
const zh2 = () => conversations[0].messages[2].translatedContent;

(async () => {
  global.activeConvId = 'c1';
  global._frameIsOurs = () => true;

  check('entry_fn_exposed', typeof _onConvNotifyPush === 'function');

  // ══ A1. SELF-ECHO window: our own finishStream PUT landed 1s ago ══
  {
    seed({ _localWriteAt: Date.now() - 1000 });
    _onConvNotifyPush({ type: 'conv_changed', convId: 'c1', rev: 9, userId: 1 });
    await sleep(250);
    check('A1_selfecho_translation_delivered', zh1() === '第一个已完成回答。' && zh2() === '第二个已完成回答。');
    check('A1_selfecho_narration_delivered',
      conversations[0].messages[2].segments[0].translatedText === '旁白中文');
    check('A1_selfecho_fetched_once', getCalls === 1);
    check('A1_selfecho_content_untouched',
      conversations[0].messages[1].content === 'First settled answer.');
    check('A1_selfecho_repainted', applyMsgCalls.length >= 1);
  }

  // ══ A2. LIVE STREAM on this conv ══
  //    The tail is mid-stream: local content DIFFERS from the server copy, so
  //    the identity guard must refuse the tail while still translating the
  //    settled earlier turn.
  {
    seed();
    activeStreams.set('c1', { controller: {} });
    conversations[0].messages[2].content = 'Second settled answer, still streaming MORE';
    _onConvNotifyPush({ type: 'conv_changed', convId: 'c1', rev: 9, userId: 1 });
    await sleep(250);
    activeStreams.delete('c1');
    check('A2_livestream_settled_turn_translated', zh1() === '第一个已完成回答。');
    check('A2_livestream_streaming_tail_refused', zh2() === undefined);
    check('A2_livestream_tail_content_untouched',
      conversations[0].messages[2].content === 'Second settled answer, still streaming MORE');
    check('A2_livestream_no_full_rebuild', replaceAllCalls.length === 0);
  }

  // ══ A3. EDITING a message ══
  {
    seed();
    global._editingMsgIdx = 1;             // user is editing msg[1]
    _onConvNotifyPush({ type: 'conv_changed', convId: 'c1', rev: 9, userId: 1 });
    await sleep(250);
    global._editingMsgIdx = null;
    check('A3_editing_other_turn_translated', zh2() === '第二个已完成回答。');
    check('A3_editing_no_full_rebuild', replaceAllCalls.length === 0);
    const touchedEdited = applyMsgCalls.some((c) => c.idx === 1);
    check('A3_editing_target_not_repainted', !touchedEdited);
  }

  // ══ B. GUARD INTEGRITY — the lane must stay translation-ONLY ══
  {
    seed({ _localWriteAt: Date.now() - 1000 });
    // Server ALSO claims different content/thinking/toolRounds. The lane must
    // adopt the 译文 and NOTHING else — this is what keeps the guards' purpose
    // intact (never overwrite what this device holds more recently).
    SERVER.messages[1].content = 'SERVER STALE CONTENT that must not land';
    SERVER.messages[2].thinking = 'SERVER STALE THINKING';
    SERVER.messages[2].toolRounds = [{ roundNum: 99 }];
    _onConvNotifyPush({ type: 'conv_changed', convId: 'c1', rev: 9, userId: 1 });
    await sleep(250);
    check('B_content_never_overwritten',
      conversations[0].messages[1].content === 'First settled answer.');
    check('B_mismatched_content_gets_no_translation', zh1() === undefined);
    check('B_thinking_never_overwritten',
      conversations[0].messages[2].thinking === 'th');
    check('B_toolrounds_never_overwritten',
      conversations[0].messages[2].toolRounds.length === 0);
    // msg[2]'s content DID match, so its 译文 is legitimately adopted.
    check('B_matched_turn_still_translated', zh2() === '第二个已完成回答。');
  }

  // ══ B2. rev-gate still authoritative: an old/equal rev is a no-op ══
  {
    seed({ _localWriteAt: Date.now() - 1000 });
    _onConvNotifyPush({ type: 'conv_changed', convId: 'c1', rev: 8, userId: 1 });
    await sleep(250);
    check('B2_revgate_no_fetch', getCalls === 0);
    check('B2_revgate_no_translation', zh1() === undefined);
  }

  // ══ B3. other user's frame still dropped ══
  {
    seed({ _localWriteAt: Date.now() - 1000 });
    global._frameIsOurs = () => false;
    _onConvNotifyPush({ type: 'conv_changed', convId: 'c1', rev: 9, userId: 2 });
    await sleep(250);
    global._frameIsOurs = () => true;
    check('B3_other_user_no_fetch', getCalls === 0);
  }

  // ══ C. BACKGROUND conv: marked stale so the reopen path merges ══
  {
    seed();
    global.activeConvId = 'cOther';
    _onConvNotifyPush({ type: 'conv_changed', convId: 'c1', rev: 9, userId: 1 });
    await sleep(250);
    global.activeConvId = 'c1';
    check('C_background_marked_stale', conversations[0]._needsLoad === true);
    check('C_background_no_viewport_repaint', replaceAllCalls.length === 0);
  }

  console.log(out.join('\n'));
  process.exit(0);
})();
"""


def _run(name: str, cts_path: str) -> str:
    harness = os.path.join(HERE, f'_xlate_guards_{name}.js')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, REDUCERS, cts_path, STATE_REDUCER],
            capture_output=True, text=True, timeout=90, cwd=ROOT,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_translation_survives_all_three_frame_guards():
    out = _run('main', CTS)
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'guard-lane failures:\n' + out
    assert out.count('PASS') >= 20, f'expected >=20 PASS, got:\n{out}'
    print(out)


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_NEUTER_lane_dispatch_is_load_bearing(tmp_path):
    """Remove the translation-only lane dispatch on a COPY → every guarded
    scenario goes dark again, while the guard-integrity assertions stay green
    (they assert *refusals*, which survive trivially when nothing is adopted).
    Proves the lane dispatch is what carries the fix."""
    with open(CTS, encoding='utf-8') as f:
        src = f.read()

    # Every guard diverts through one of the two lane calls — remove ALL
    # dispatches (translation-only ×2 guards + the stream-active lane).
    dispatches = re.findall(
        r'^\s*(?:_scheduleTranslationOnlyVerify|_scheduleStreamActiveVerify)'
        r'\(convId\);\s*$', src, flags=re.M)
    assert len(dispatches) >= 3, (
        f'expected >=3 lane dispatches (one per guard), found {len(dispatches)} '
        '— update the neuter target')
    neutered = re.sub(
        r'^\s*(?:_scheduleTranslationOnlyVerify|_scheduleStreamActiveVerify)'
        r'\(convId\);\s*$', '', src, flags=re.M)
    assert neutered != src

    copy = tmp_path / 'cross_tab_sync_neutered.js'
    copy.write_text(neutered, encoding='utf-8')

    out = _run('neuter', str(copy))
    for name in ('A1_selfecho_translation_delivered',
                 'A1_selfecho_narration_delivered',
                 'A2_livestream_settled_turn_translated',
                 'A3_editing_other_turn_translated'):
        assert f'FAIL {name}' in out, (
            f'NEUTER did not bite on {name} — the translation landed without '
            f'the lane dispatch.\n{out}')
    # Refusal-shaped guards still hold with nothing adopted.
    assert 'PASS B_content_never_overwritten' in out, out
    assert 'PASS C_background_marked_stale' in out, out

    with open(CTS, encoding='utf-8') as f:
        assert f.read() == src, 'harness mutated the shipped cross_tab_sync.js'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_reopen_path_merges_translations_through_shared_reducer():
    """A background conv only gets ``_needsLoad``, so reopening MUST merge the
    server 译文 with no further user action. Assert every reconcile branch of
    ``loadConversationMessages`` either delegates to the shared reducer or
    replaces messages wholesale from the server body."""
    with open(os.path.join(JS_DIR, 'core', 'conversations.js'), encoding='utf-8') as f:
        src = f.read()
    merges = src.count('_mergeServerTranslations(serverMsgs, conv.messages)')
    assert merges >= 3, (
        f'expected >=3 reopen-branch translation merges, found {merges} — a '
        'reconcile branch may have lost its merge, which would resurrect '
        '"switch conversations to see the 译文"')
    # The per-index literal call moved inside the array-level reducer
    # (conversations.js:1060) — assert the reopen path routes through IT.
    assert '_mergeServerTranslations(' in src, (
        'reopen merge no longer routes through the shared array-level reducer '
        '(_mergeServerTranslations internally calls _mergeTranslationFields)')
    # And the wholesale-overwrite branches take the server array (translations
    # included by construction). Two adoption forms count (pt_cfdfd30c8699407b):
    # the direct assignment AND the _withPendingQueuedTail wrapper — the latter
    # still adopts the server body wholesale (translations ride along), it only
    # re-appends local queued rows the server legitimately lacks.
    wholesale = (src.count('conv.messages = serverMsgs')
                 + src.count('_withPendingQueuedTail(conv.messages, serverMsgs)'))
    assert wholesale >= 2, src[:0] or (
        'wholesale reconcile branches no longer adopt the server messages')
