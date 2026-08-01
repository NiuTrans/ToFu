"""tests/test_frontend_board_answer_lane.py — the Project Brain board's
AWAITING-ANSWER lane after the 2026-07-31 consolidation, plus the per-card
"Create conversation" action.

The operator problem this round closes (owner complaint, with a panel
screenshot): an epic halted on a structured question rendered its FULL
answering UI in TWO places — the Board tab's awaiting lane AND the Needs-you
tab's card — so the operator met the identical question twice. The answering
surface is now singular (redesign §D6: deep-link, don't duplicate):

  • the BOARD keeps a COMPACT awaiting card — title+reason in one clamp, an
    "awaiting your answer" badge, and a deep-link button that switches the
    panel to the Needs-you tab (where the question, option chips and the
    free-text input live; covered by test_frontend_project_brain_attention);
  • EVERY board card (open / claimed / blocked / awaiting / done) leads its
    action row with a "New chat" button: close the panel, open a fresh
    conversation and pre-fill the composer with a kickoff naming the epic id
    + title — never auto-sent.

Kept from the pre-consolidation suite (they pin behaviours that did not
change): one clamp per card, md-lite rendering with XSS inert, the inline
block-note editor (zero window.prompt), the answered chip, and the board
badge counting the awaiting epic.

NC-FE ×3: (a) byte-revert the awaiting partition in a COPY → the pending epic
leaks back into the open/blocked lane; (b) strip the gotoAttention handler →
the deep-link no longer switches tabs; (c) strip the createConv handler →
clicking "New chat" opens nothing. Shipped file untouched.

Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_BRAIN_SRC = os.path.join(ROOT, 'static', 'js', 'project-brain.js')
_I18N_SRC = os.path.join(ROOT, 'static', 'js', 'i18n.js')
_CSS_SRC = os.path.join(ROOT, 'static', 'styles.css')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


def _extract_panel_fragment():
    with open(os.path.join(ROOT, 'index.html'), encoding='utf-8') as f:
        html = f.read()
    start = html.find('<div class="project-brain-overlay"')
    assert start != -1, 'project-brain-overlay not found in index.html'
    end = html.find('<div class="chat-container"', start)
    assert end != -1, 'could not bound the overlay fragment'
    return html[start:end].strip()


_LONG_TITLE = 'EPIC ' + 'T' * 300
_LONG_REASON = '[human-gated] ' + 'R' * 300

_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const SRC = process.argv[2];
const ROOT = process.argv[3];
const FRAG = process.argv[4];
const LONG_TITLE = process.argv[5];
const LONG_REASON = process.argv[6];
const fragment = fs.readFileSync(FRAG, 'utf8');
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body>' + fragment +
  // The create-conversation launcher pre-fills the REAL composer.
  '<textarea id="userInput"></textarea></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;

win.Icon = global.Icon = (name) => '<svg data-icon="' + name + '"></svg>';
// project-brain's internal _t(key, fallback) calls the GLOBAL t(key) with ONE
// arg — the stub must serve the strings we probe via a dictionary.
const T = {
  'projectBrain.blockedRetry': 'auto-retry in',
  'projectBrain.blockedCount': 'blocked %d×',
  'projectBrain.laneAwaiting': 'Awaiting your answer',
  'projectBrain.awaitingAnswerMeta': 'waiting for your answer',
  'projectBrain.actGoAnswer': 'Go answer',
  'projectBrain.actCreateConv': 'New chat',
  'projectBrain.epicChatPrompt': 'Claim and advance this board epic: {id}\n{title}\nProject: {path}',
  'projectBrain.yourAnswer': 'Your answer',
  'projectBrain.blockReasonPrompt': 'Why is this blocked?',
  'projectBrain.blockNoteSubmit': 'Mark blocked',
  'projectBrain.blockNoteCancel': 'Cancel',
};
win.t = global.t = (k) => (T[k] != null ? T[k] : k);
win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
win.loadConversation = global.loadConversation = () => {};
win.getActiveConv = global.getActiveConv = () => ({ id: 'c1', projectPath: '/proj/real' });
win._getConvProjectPath = global._getConvProjectPath = (c) => (c && c.projectPath) || '';
win.pushSubscribe = global.pushSubscribe = () => {};
win.pushUnsubscribe = global.pushUnsubscribe = () => {};
win.updateSendButton = global.updateSendButton = () => {};
let promptCalls = 0;
win.prompt = global.prompt = () => { promptCalls++; return 'x'; };

const now = Date.now();
const calls = [];
// ORDER-SENSITIVE spy: the REAL newChat() clears the project attachment when
// the composer is empty at call time, so the launcher MUST pre-fill before
// invoking it. Record what the composer held at the moment newChat ran — a
// spy that only counts invocations is exactly how this regression hid.
let composerNonEmptyAtNewChat = null;
win.newChat = global.newChat = () => {
  if (composerNonEmptyAtNewChat === null) {
    const inp = win.document.getElementById('userInput');
    composerNonEmptyAtNewChat = !!(inp && (inp.value || '').trim());
  }
  calls.push({ fn: 'newChat' });
};
// The Board's deep-link delegates card-focus to the attention module (not
// loaded in this unit) — stub its channel and record the hand-off.
win.ProjectBrainAttention = global.ProjectBrainAttention = {
  focusItem: (id) => { calls.push({ fn: 'focusItem', id: id }); },
};
const boardPayload = {
  open: 3, claimed: 0, done: 0,
  tasks: [
    { id: 'pt_plain', title: 'PLAIN OPEN EPIC', status: 'open', kind: 'epic',
      owner_conv_id: '', depends_on: [], block_question: null, human_answer: '' },
    { id: 'pt_q', title: 'QUESTION EPIC ' + '**bold title**', status: 'open', kind: 'epic',
      owner_conv_id: '', depends_on: [],
      blocked_until: now + 3600000, block_count: 2,
      block_reason: '[human-gated] owner decides the default',
      block_question: { q: 'Force-push on divergence, or abort?',
        options: [ { label: 'A keep force (safely scoped)' },
                   { label: 'B abort on divergence', description: 'adds a flag' } ] },
      human_answer: '' },
    { id: 'pt_blk', title: LONG_TITLE, status: 'open', kind: 'epic',
      owner_conv_id: '', depends_on: [],
      blocked_until: now + 3600000, block_count: 3, block_reason: LONG_REASON,
      block_question: null, human_answer: '' },
    { id: 'pt_ans', title: 'ANSWERED EPIC', status: 'open', kind: 'epic',
      owner_conv_id: '', depends_on: [], block_question: null,
      human_answer: 'B — abort on divergence' },
    { id: 'pt_xss', title: 'XSS **b** `c` <script>alert(1)</script> [x](javascript:alert(1)) [ok](https://a.b/c)',
      status: 'open', kind: 'epic', owner_conv_id: '', depends_on: [],
      block_question: null, human_answer: '' },
  ] };
win.Api = global.Api = { project: {
  feed: (p) => Promise.resolve({ maxSeq: 0, events: [] }),
  charter: (p) => Promise.resolve({ exists: false, decisions: [] }),
  charterPending: (p) => Promise.resolve({ pending: [] }),
  board: (p) => Promise.resolve(boardPayload),
  brainSummary: (p) => Promise.resolve({}),
  brainInfluence: (p) => Promise.resolve({}),
  boardReopen: (p, tid, cid) => { calls.push({ fn: 'reopen', tid: tid }); return Promise.resolve({ ok: true }); },
  boardComplete: (p, tid, cid) => { calls.push({ fn: 'complete', tid: tid }); return Promise.resolve({ ok: true }); },
  boardBlock: (p, tid, cid, reason) => { calls.push({ fn: 'block', tid: tid, reason: reason }); return Promise.resolve({ ok: true }); },
} };

eval(fs.readFileSync(SRC, 'utf8'));  // project-brain.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
function flush(n) { let p = Promise.resolve(); for (let i = 0; i < n; i++) p = p.then(()=>{}); return p; }

win.openProjectBrain();

flush(8).then(() => {
  const doc = win.document;
  const board = doc.getElementById('projectBrainBoardBody');

  // ── 1. The awaiting partition survives: compact lane at the TOP ──
  const awaitingLane = board.querySelector('.pb-board-lane-awaiting');
  check('awaiting_lane_exists', !!awaitingLane);
  check('question_epic_in_awaiting', !!awaitingLane &&
        awaitingLane.innerHTML.indexOf('QUESTION EPIC') !== -1);
  check('awaiting_lane_is_first',
        !!board.querySelector('.pb-board-lane') &&
        board.querySelector('.pb-board-lane').classList.contains('pb-board-lane-awaiting'));
  const openLane = board.querySelector('.pb-board-lane-open');
  check('question_epic_not_in_open', !!openLane &&
        openLane.innerHTML.indexOf('QUESTION EPIC') === -1);
  const blockedLane = board.querySelector('.pb-board-lane-blocked');
  check('question_epic_not_in_blocked', !!blockedLane &&
        blockedLane.innerHTML.indexOf('QUESTION EPIC') === -1);

  // ── 2. THE CONSOLIDATION: the card is COMPACT — no question UI here ──
  const qCard = board.querySelector('.pb-board-card[data-task-id="pt_q"]');
  check('awaiting_card_compact', !!qCard && qCard.classList.contains('pb-board-awaiting'));
  check('awaiting_badge_visible', !!qCard && !!qCard.querySelector('.pb-board-badge-awaiting'));
  check('no_question_panel', !!qCard && !qCard.querySelector('.pb-question'));
  check('no_answer_input', !!qCard && !qCard.querySelector('.pb-answer-text'));
  check('no_option_chips', !!qCard && qCard.querySelectorAll('.pb-chip').length === 0);
  check('awaiting_meta_no_retry_countdown', !!qCard &&
        qCard.innerHTML.indexOf('auto-retry in') === -1);
  // …but it is not a dead end: the deep-link into the single answering
  // surface is the card's primary action.
  const gotoBtn = qCard ? qCard.querySelector('.pb-board-act[data-act="gotoAttention"]') : null;
  check('goto_attention_button', !!gotoBtn);

  // ── 3. The deep-link lands the operator on the Needs-you tab, ON the card ──
  const attnTab = doc.querySelector('.pb-tab[data-pb-tab="attention"]');
  check('attention_tab_not_active_initially',
        !!attnTab && !attnTab.classList.contains('pb-tab-active'));
  if (gotoBtn) gotoBtn.click();
  check('goto_switches_attention',
        !!attnTab && attnTab.classList.contains('pb-tab-active'));
  const focusCalls = calls.filter(c => c.fn === 'focusItem');
  check('goto_focuses_card', focusCalls.length === 1 && focusCalls[0].id === 'pt_q');

  // ── 4. "New chat" — EVERY card carries it; clicking launches a seeded
  //       conversation (never auto-sends) ──
  const plainCard = board.querySelector('.pb-board-card[data-task-id="pt_plain"]');
  check('open_card_has_createconv',
        !!plainCard && !!plainCard.querySelector('.pb-board-act[data-act="createConv"]'));
  const blkCard0 = board.querySelector('.pb-board-card[data-task-id="pt_blk"]');
  check('blocked_card_has_createconv',
        !!blkCard0 && !!blkCard0.querySelector('.pb-board-act[data-act="createConv"]'));
  const qConvBtn = qCard ? qCard.querySelector('.pb-board-act[data-act="createConv"]') : null;
  check('awaiting_card_has_createconv', !!qConvBtn);
  const input = doc.getElementById('userInput');
  const overlay = doc.querySelector('.project-brain-overlay');
  check('panel_open_before_launch', !!overlay && overlay.hidden === false);
  if (qConvBtn) qConvBtn.click();
  const launches = calls.filter(c => c.fn === 'newChat');
  check('createconv_launches_chat', launches.length === 1);
  check('createconv_prefills_epic_id', !!input && input.value.indexOf('pt_q') !== -1);
  check('createconv_prefills_title', !!input && input.value.indexOf('QUESTION EPIC') !== -1);
  check('createconv_prefills_project_path',
        !!input && input.value.indexOf('/proj/real') !== -1);
  // THE owner-caught regression: pre-fill must land BEFORE newChat runs, or
  // newChat's empty-composer branch strips the very project the kickoff
  // needs. The spy recorded the composer state at invocation time.
  check('createconv_prefills_before_newchat', composerNonEmptyAtNewChat === true);
  check('createconv_closes_panel', !!overlay && overlay.hidden === true);

  // ── 5. Answered epic: NOT awaiting, carries the decision chip ──
  const board2 = doc.getElementById('projectBrainBoardBody');
  const awaitingLane2 = board2.querySelector('.pb-board-lane-awaiting');
  check('answered_epic_not_awaiting', !awaitingLane2 ||
        awaitingLane2.innerHTML.indexOf('ANSWERED EPIC') === -1);
  const ansCard = board2.querySelector('.pb-board-card[data-task-id="pt_ans"]');
  check('answered_chip_visible', !!ansCard &&
        !!ansCard.querySelector('.pb-board-badge-answered') &&
        ansCard.querySelector('.pb-board-badge-answered').innerHTML.indexOf('abort on divergence') !== -1);

  // ── 6. ONE clamp per card (the multi-展开全文 complaint) ──
  const blkCard = board2.querySelector('.pb-board-card[data-task-id="pt_blk"]');
  check('blocked_card_single_clamp', !!blkCard &&
        blkCard.querySelectorAll('.pb-clamp-toggle').length === 1);
  check('blocked_meta_unified', !!blkCard &&
        blkCard.innerHTML.indexOf('blocked %d×'.replace('%d','3')) !== -1);
  // The compact awaiting card combines title+reason in one clamp too.
  check('awaiting_card_single_clamp', !!qCard &&
        qCard.querySelectorAll('.pb-clamp-toggle').length <= 1);

  // ── 7. md-lite: bold/code render, XSS stays inert, javascript: not linked ──
  const xssCard = board2.querySelector('.pb-board-card[data-task-id="pt_xss"]');
  const html = xssCard ? xssCard.innerHTML : '';
  check('mdlite_bold', html.indexOf('<strong>b</strong>') !== -1);
  check('mdlite_code', html.indexOf('<code>c</code>') !== -1);
  check('mdlite_https_link', html.indexOf('href="https://a.b/c"') !== -1);
  check('xss_no_script_element', !xssCard || !xssCard.querySelector('script'));
  check('xss_no_javascript_href', html.indexOf('href="javascript:') === -1);

  // ── 8. Block button opens the INLINE editor (never window.prompt) ──
  const blockBtn = plainCard ? plainCard.querySelector('.pb-board-act[data-act="block"]') : null;
  if (blockBtn) blockBtn.click();
  const editor = plainCard ? plainCard.querySelector('.pb-note-editor') : null;
  check('block_inline_editor_opens', !!editor);
  check('no_window_prompt_used', promptCalls === 0);
  if (editor) {
    const noteInput = editor.querySelector('.pb-note-text');
    noteInput.value = 'waiting on the gateway team';
    editor.querySelector('[data-note-submit]').click();
  }
  return flush(4);
}).then(() => {
  const blocks = calls.filter(c => c.fn === 'block');
  check('block_note_submitted', blocks.length === 1 &&
        blocks[0].reason === 'waiting on the gateway team');

  // ── 9. Badge counts the awaiting epic (it is THE attention item) ──
  const badge = win.document.getElementById('pbTabCountBoard');
  const badgeTxt = badge ? (badge.textContent || '').trim() : '(missing)';
  check('badge_counts_awaiting', badgeTxt === '4');  // plain + xss + answered + awaiting

  console.log('BADGE=' + badgeTxt);
  console.log(out.join('\n'));
});
"""


def _run(brain_src):
    frag = _extract_panel_fragment()
    frag_file = os.path.join(HERE, '_pb_answer_fragment.html')
    harness = os.path.join(HERE, '_pb_answer_harness.js')
    with open(frag_file, 'w', encoding='utf-8') as f:
        f.write(frag)
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, brain_src, ROOT, frag_file, _LONG_TITLE, _LONG_REASON],
            capture_output=True, text=True, timeout=60)
    finally:
        for p in (frag_file, harness):
            try:
                os.remove(p)
            except OSError:
                pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


_EXPECTED_PASSES = (
    'PASS awaiting_lane_exists', 'PASS question_epic_in_awaiting',
    'PASS awaiting_lane_is_first', 'PASS question_epic_not_in_open',
    'PASS question_epic_not_in_blocked', 'PASS awaiting_card_compact',
    'PASS awaiting_badge_visible', 'PASS no_question_panel',
    'PASS no_answer_input', 'PASS no_option_chips',
    'PASS awaiting_meta_no_retry_countdown', 'PASS goto_attention_button',
    'PASS attention_tab_not_active_initially', 'PASS goto_switches_attention',
    'PASS open_card_has_createconv', 'PASS blocked_card_has_createconv',
    'PASS awaiting_card_has_createconv', 'PASS panel_open_before_launch',
    'PASS createconv_launches_chat', 'PASS createconv_prefills_epic_id',
    'PASS createconv_prefills_title', 'PASS createconv_prefills_project_path',
    'PASS createconv_prefills_before_newchat', 'PASS createconv_closes_panel',
    'PASS goto_focuses_card',
    'PASS answered_epic_not_awaiting', 'PASS answered_chip_visible',
    'PASS blocked_card_single_clamp', 'PASS blocked_meta_unified',
    'PASS awaiting_card_single_clamp', 'PASS mdlite_bold', 'PASS mdlite_code',
    'PASS mdlite_https_link', 'PASS xss_no_script_element',
    'PASS xss_no_javascript_href', 'PASS block_inline_editor_opens',
    'PASS no_window_prompt_used', 'PASS block_note_submitted',
    'PASS badge_counts_awaiting',
)


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_awaiting_lane_is_compact_and_deep_linked():
    """The shipped board renders a pending structured question as a COMPACT
    awaiting card (badge + one clamp, NO question UI — that lives only in the
    Needs-you tab) whose deep-link switches the panel to the attention tab;
    every card leads its actions with "New chat", which closes the panel,
    opens a fresh conversation and pre-fills the composer with the epic id +
    title. Plus the unchanged invariants: single clamp, md-lite with XSS
    inert, inline block editor (zero window.prompt), answered chip, badge."""
    output = _run(_BRAIN_SRC)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'awaiting-lane / create-conversation failures:\n' + output
    for marker in _EXPECTED_PASSES:
        assert marker in output, output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_FE_awaiting_partition_is_load_bearing():
    """NC-FE: byte-revert the awaiting partition in a COPY of project-brain.js
    (drop the pending-question branch so the epic falls through to the
    blocked/open bucket) → no awaiting lane exists and the question epic
    leaks into a lifecycle lane → the assertions flip. Shipped file
    untouched."""
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = (
        "      if (t.status === 'open' && t.block_question &&\n"
        "          !String(t.human_answer || '').trim()) { cols.awaiting.push(t); continue; }\n")
    assert anchor in original, 'awaiting-partition anchor not found (source changed?)'
    patched = original.replace(anchor, '', 1)
    copy_path = os.path.join(HERE, '_pb_answer_nc_copy.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _run(copy_path)
        assert ('FAIL awaiting_lane_exists' in output
                or 'FAIL question_epic_not_in_blocked' in output), \
            ('NC-FE: without the partition, the question epic must leak out of '
             'the awaiting lane (assertions should fail):\n' + output)
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain.js must be byte-identical'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_FE_goto_attention_handler_is_load_bearing():
    """NC-FE: strip the gotoAttention handler in a COPY → the deep-link button
    is rendered but clicking it no longer switches tabs →
    goto_switches_attention FAILS. A rendered-but-dead deep-link is exactly
    the failure mode this guards (the board would become a cul-de-sac)."""
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = "    } else if (act === 'gotoAttention') {"
    assert anchor in original, 'gotoAttention anchor not found (source changed?)'
    patched = original.replace(
        anchor, "    } else if (false && act === 'gotoAttention') {  // NC", 1)
    copy_path = os.path.join(HERE, '_pb_goto_nc_copy.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _run(copy_path)
        assert 'FAIL goto_switches_attention' in output, \
            ('NC-FE: without the handler, the deep-link must stop switching '
             'tabs:\n' + output)
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain.js must be byte-identical'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_FE_createconv_handler_is_load_bearing():
    """NC-FE: strip the createConv handler in a COPY → the "New chat" button
    renders on every card but clicking it launches nothing →
    createconv_launches_chat FAILS. Guards against a button that exists but
    does not act (the same dead-button disease, new surface)."""
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = "      if (task) _openEpicConversation(task.id, task.title);"
    assert anchor in original, 'createConv anchor not found (source changed?)'
    patched = original.replace(
        anchor, "      if (false) _openEpicConversation(task.id, task.title);  // NC", 1)
    copy_path = os.path.join(HERE, '_pb_conv_nc_copy.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _run(copy_path)
        assert 'FAIL createconv_launches_chat' in output, \
            ('NC-FE: without the handler, "New chat" must stop launching a '
             'conversation:\n' + output)
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain.js must be byte-identical'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_FE_prefill_must_land_before_newchat():
    """NC: strip the pre-fill in a COPY (the degenerate form of the inverted
    order — composer empty when newChat runs) → the order spy records an
    EMPTY composer at newChat time → createconv_prefills_before_newchat FAILS.

    This is the assertion that would have caught the owner-reported defect:
    the launcher used to invoke newChat() first, whose empty-composer branch
    clears the project attachment the kickoff's board tools resolve from."""
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = "      input.value = _t('projectBrain.epicChatPrompt',"
    assert anchor in original, 'pre-fill anchor not found (source changed?)'
    patched = original.replace(
        anchor, "      if (false) input.value = _t('projectBrain.epicChatPrompt',  // NC", 1)
    copy_path = os.path.join(HERE, '_pb_prefill_nc_copy.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _run(copy_path)
        assert 'FAIL createconv_prefills_before_newchat' in output, \
            ('NC-FE: without a pre-fill before newChat, the order spy must '
             'record an empty composer:\n' + output)
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain.js must be byte-identical'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_FE_goto_focus_delegation_is_load_bearing():
    """NC: strip the focusItem hand-off in a COPY → the deep-link switches
    tabs but never says WHICH card → goto_focuses_card FAILS (the operator
    lands on the tab top and hunts, the exact gap the focus channel closes)."""
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = '        window.ProjectBrainAttention.focusItem(taskId);'
    assert anchor in original, 'focus-delegation anchor not found (source changed?)'
    patched = original.replace(
        anchor, '        // NC (focus hand-off stripped)', 1)
    copy_path = os.path.join(HERE, '_pb_focus_nc_copy.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _run(copy_path)
        assert 'FAIL goto_focuses_card' in output, \
            ('NC-FE: without the hand-off, the focus channel must stay '
             'silent:\n' + output)
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain.js must be byte-identical'


# ════════════════════════════════════════════════════════════════════
#  Static guards — no browser needed
# ════════════════════════════════════════════════════════════════════

def test_no_window_prompt_anywhere_in_panel():
    """Pillar #2: the panel's interactions are unified on in-panel editors —
    a blocking browser-native prompt() inside a custom overlay must never
    come back."""
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        src = f.read()
    assert 'window.prompt(' not in src
    assert 'prompt(_t(' not in src, 'bare prompt() call reintroduced'
    assert "typeof prompt === 'function'" not in src


def test_i18n_keys_present_zh_and_en():
    """Every new UI string must exist in BOTH languages (the '自动重试于 ~850m'
    mixed-locale complaint)."""
    with open(_I18N_SRC, encoding='utf-8') as f:
        src = f.read()
    for key in (
        'projectBrain.kind.answered', 'projectBrain.blockedCount',
        'projectBrain.laneAwaiting', 'projectBrain.awaitingAnswerMeta',
        'projectBrain.actGoAnswer', 'projectBrain.actCreateConv',
        'projectBrain.epicChatPrompt', 'projectBrain.convCreateFailed',
        'projectBrain.answerPlaceholder', 'projectBrain.answerSubmit',
        'projectBrain.yourAnswer', 'projectBrain.blockNoteSubmit',
        'projectBrain.blockNoteCancel',
    ):
        m = re.search(re.escape("'" + key + "'") +
                      r":\s*\{\s*zh:\s*'[^']+',\s*en:\s*'[^']+'\s*\}", src)
        assert m, f'i18n key missing or not bilingual: {key}'


def test_css_unified_primitives_present():
    with open(_CSS_SRC, encoding='utf-8') as f:
        css = f.read()
    for sel in ('.pb-board-act.pb-btn-primary', '.pb-board-lane-awaiting',
                '.pb-board-card.pb-board-awaiting', '.pb-board-badge-awaiting',
                '.pb-board-awaiting-meta', '.pb-chip{',
                '.pb-board-act.pb-board-act-goto',
                '.pb-board-act.pb-board-act-createConv',
                '.pb-attn-act.pb-attn-act-conv',
                '.pb-note-editor', '.pb-note-text',
                '.pb-board-badge-answered'):
        assert sel in css, f'missing CSS primitive: {sel}'


def test_css_dead_question_primitives_absent():
    """The consolidation removed the Board-side question UI; its CSS went with
    it. Guard against a reintroduction that would re-split the answering
    surface across two tabs (the Needs-you tab keeps .pb-attn-* — untouched)."""
    with open(_CSS_SRC, encoding='utf-8') as f:
        css = f.read()
    for dead in ('.pb-question{', '.pb-question-label', '.pb-question-q',
                 '.pb-answer-input-row', '.pb-answer-text'):
        assert dead not in css, f'dead CSS primitive reintroduced: {dead}'
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        js = f.read()
    for dead in ('pb-question', 'pb-answer-text', 'answerSubmit', 'answerOpt'):
        assert dead not in js, f'dead question-UI hook reintroduced in JS: {dead}'
