"""tests/test_frontend_board_answer_lane.py — the human-facing AWAITING-ANSWER
lane + unified interaction primitives of the Project Brain board (Pillars
#1-#3 of the panel redesign).

The operator problems this closes (owner complaint 2026-07-24, with board
screenshots):

  1. A [human-gated] blocked epic showed only a bare Reopen/Done pair — the
     decision the agent asked for ("Owner decides: (A)… (B)…") was buried in a
     long English reason string with NO way to answer it. The redesign renders
     the structured question as the card's PRIMARY content with one-click
     option chips + a free-text input (the ask_human interaction model), in a
     dedicated lane at the TOP of the board.
  2. One epic carried SEVERAL "展开全文" toggles (title clamp + reason clamp).
     Blocked/awaiting cards now combine title+reason into ONE clamp → at most
     one toggle per card.
  3. Raw `**asterisks**` and dead `[sibling] path=` tokens rendered verbatim —
     a markdown-LITE renderer (escape-first, then **bold**/`code`/https-links)
     is applied uniformly at every board clamp.
  4. window.prompt() (a blocking browser-native dialog inside a custom overlay)
     is GONE — Block and New-epic use the same inline editor family as the
     answer input.

The suite drives the REAL shipped renderBoard + _boardMutate through jsdom
over the actual #projectBrainBoardBody fragment from index.html, plus static
guards (no prompt(, i18n keys zh+en, CSS primitives present) and an md-lite
XSS probe. NC-FE byte-reverts the awaiting partition in a COPY → the pending
epic leaks back into the open/blocked lane → assertions flip. Shipped file
untouched.

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
const dom = new JSDOM('<!DOCTYPE html><body>' + fragment + '</body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;

win.Icon = global.Icon = (name) => '<svg data-icon="' + name + '"></svg>';
// project-brain's internal _t(key, fallback) calls the GLOBAL t(key) with ONE
// arg — the stub must serve the strings we probe via a dictionary.
const T = {
  'projectBrain.blockedRetry': 'auto-retry in',
  'projectBrain.blockedCount': 'blocked %d×',
  'projectBrain.laneAwaiting': 'Awaiting your answer',
  'projectBrain.needsYourDecision': 'Your decision needed',
  'projectBrain.awaitingAnswerMeta': 'waiting for your answer',
  'projectBrain.answerPlaceholder': 'type your answer',
  'projectBrain.answerSubmit': 'Submit answer',
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
let promptCalls = 0;
win.prompt = global.prompt = () => { promptCalls++; return 'x'; };

const now = Date.now();
const calls = [];
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
  boardAnswer: (p, tid, cid, answer) => { calls.push({ fn: 'answer', tid: tid, answer: answer }); return Promise.resolve({ ok: true }); },
} };

eval(fs.readFileSync(SRC, 'utf8'));  // project-brain.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
function flush(n) { let p = Promise.resolve(); for (let i = 0; i < n; i++) p = p.then(()=>{}); return p; }

win.openProjectBrain();

flush(8).then(() => {
  const doc = win.document;
  const board = doc.getElementById('projectBrainBoardBody');

  // ── 1. Awaiting-answer lane at the TOP, pending question inside ──
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

  // ── 2. The question panel is the card's primary content ──
  const qCard = board.querySelector('.pb-board-card[data-task-id="pt_q"]');
  check('question_panel_exists', !!qCard && !!qCard.querySelector('.pb-question'));
  check('question_text_visible', !!qCard &&
        qCard.querySelector('.pb-question-q') &&
        qCard.querySelector('.pb-question-q').innerHTML.indexOf('Force-push on divergence') !== -1);
  const chips = qCard ? qCard.querySelectorAll('.pb-chip') : [];
  check('option_chips_rendered', chips.length === 2);
  check('free_text_input_present', !!qCard && !!qCard.querySelector('.pb-answer-text'));
  check('awaiting_meta_no_retry_countdown', !!qCard &&
        qCard.innerHTML.indexOf('auto-retry in') === -1);

  // ── 3. Answering via an option chip ──
  if (chips.length === 2) chips[1].click();  // 'B abort on divergence'
  return flush(4).then(() => {
    const ans = calls.filter(c => c.fn === 'answer');
    check('chip_answer_submitted', ans.length === 1 && ans[0].tid === 'pt_q');
    check('chip_answer_label', ans.length === 1 &&
          ans[0].answer === 'B abort on divergence');

    // ── 4. Answering via free text (re-render happened after the chip answer;
    //       re-query the fresh card) ──
    const qCard2 = board.querySelector('.pb-board-card[data-task-id="pt_q"]');
    const input = qCard2 ? qCard2.querySelector('.pb-answer-text') : null;
    const submit = qCard2 ? qCard2.querySelector('.pb-board-act[data-act="answerSubmit"]') : null;
    if (input) input.value = 'do B, but keep the flag off by default';
    if (submit) submit.click();
    return flush(4);
  }).then(() => {
    const ans = calls.filter(c => c.fn === 'answer');
    check('free_text_answer_submitted', ans.length === 2 &&
          ans[1].answer === 'do B, but keep the flag off by default');

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

    // ── 7. md-lite: bold/code render, XSS stays inert, javascript: not linked ──
    const xssCard = board2.querySelector('.pb-board-card[data-task-id="pt_xss"]');
    const html = xssCard ? xssCard.innerHTML : '';
    check('mdlite_bold', html.indexOf('<strong>b</strong>') !== -1);
    check('mdlite_code', html.indexOf('<code>c</code>') !== -1);
    check('mdlite_https_link', html.indexOf('href="https://a.b/c"') !== -1);
    check('xss_no_script_element', !xssCard || !xssCard.querySelector('script'));
    check('xss_no_javascript_href', html.indexOf('href="javascript:') === -1);

    // ── 8. Block button opens the INLINE editor (never window.prompt) ──
    const plainCard = board2.querySelector('.pb-board-card[data-task-id="pt_plain"]');
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
    const badge = doc.getElementById('pbTabCountBoard');
    const badgeTxt = badge ? (badge.textContent || '').trim() : '(missing)';
    check('badge_counts_awaiting', badgeTxt === '4');  // plain + xss + answered + awaiting

    console.log('BADGE=' + badgeTxt);
    console.log(out.join('\n'));
  });
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
    'PASS question_epic_not_in_blocked', 'PASS question_panel_exists',
    'PASS question_text_visible', 'PASS option_chips_rendered',
    'PASS free_text_input_present', 'PASS awaiting_meta_no_retry_countdown',
    'PASS chip_answer_submitted', 'PASS chip_answer_label',
    'PASS free_text_answer_submitted', 'PASS answered_epic_not_awaiting',
    'PASS answered_chip_visible', 'PASS blocked_card_single_clamp',
    'PASS blocked_meta_unified', 'PASS mdlite_bold', 'PASS mdlite_code',
    'PASS mdlite_https_link', 'PASS xss_no_script_element',
    'PASS xss_no_javascript_href', 'PASS block_inline_editor_opens',
    'PASS no_window_prompt_used', 'PASS block_note_submitted',
    'PASS badge_counts_awaiting',
)


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_awaiting_answer_lane_and_unified_interactions():
    """The shipped board renders a pending structured question in a top
    awaiting-answer lane with one-click options + free text (both submitting
    board/answer), one clamp per card, md-lite rendering with XSS inert, the
    inline block editor (zero window.prompt calls), and the answered chip."""
    output = _run(_BRAIN_SRC)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'awaiting-lane / unified-interaction failures:\n' + output
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
        'projectBrain.laneAwaiting', 'projectBrain.needsYourDecision',
        'projectBrain.awaitingAnswerMeta', 'projectBrain.answerPlaceholder',
        'projectBrain.answerSubmit', 'projectBrain.yourAnswer',
        'projectBrain.blockNoteSubmit', 'projectBrain.blockNoteCancel',
    ):
        m = re.search(re.escape("'" + key + "'") +
                      r":\s*\{\s*zh:\s*'[^']+',\s*en:\s*'[^']+'\s*\}", src)
        assert m, f'i18n key missing or not bilingual: {key}'


def test_css_unified_primitives_present():
    with open(_CSS_SRC, encoding='utf-8') as f:
        css = f.read()
    for sel in ('.pb-board-act.pb-btn-primary', '.pb-board-lane-awaiting',
                '.pb-board-card.pb-board-awaiting', '.pb-question{',
                '.pb-question-label', '.pb-chip{', '.pb-answer-text',
                '.pb-note-editor', '.pb-note-text',
                '.pb-board-badge-answered'):
        assert sel in css, f'missing CSS primitive: {sel}'
