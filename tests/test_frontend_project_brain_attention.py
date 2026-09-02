"""jsdom regression for the Project Brain "Needs you" tab + the collab bar's
attention segment (project-brain-attention.js / presence.js).

The redesign's thesis in one line: **severity, not source, decides urgency.**

Before it, the always-visible collaboration bar led with `pendingDecisions`
(charter proposals) rendered with emphasis — but agents have self-committed
charter decisions since the 2026-07-12 de-gating, so a pending proposal blocks
nothing. Meanwhile an epic halted on a structured question — which
`project_dispatch` skips on EVERY heartbeat, so it never resolves on its own —
was not on the bar at all. The loudest signal was the least urgent one.

This suite pins the corrected behaviour end to end:

  • the Needs-you tab renders one card per attention item, in the SERVER's
    order (it must not re-sort — the SSOT's whole point);
  • a halted epic renders its question + answer controls INLINE, so the
    operator resolves it without hopping to the Board tab;
  • the empty state is a positive statement + the "waiting on their own gates"
    reassurance count, NOT a list of cooldown items;
  • the collab bar shows the aggregate `needsYou` count but reserves its
    ALARM (.collab-has-blocking) for `blocking > 0`;
  • an older backend (no needsYou field) still renders the legacy segment.

NCs: neuter the server-order preservation → a blocking-last payload renders
blocking-last; neuter the blocking-drives-emphasis rule → an advisory-only
project renders alarmed.

Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest
from tests._runtime_sections import orchestration_legacy_test_root as _legacy_test_root

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = _legacy_test_root()
JS_DIR = os.path.join(ROOT, 'static', 'js')
_ATTN_SRC = os.path.join(JS_DIR, 'project-brain-attention.js')
_PRESENCE_SRC = os.path.join(JS_DIR, 'presence.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


def _extract_panel_fragment():
    """Pull the REAL Project Brain overlay out of shipped index.html, so the
    test renders into the actual DOM the app ships (not a hand-built stub that
    can silently drift from it)."""
    with open(os.path.join(ROOT, 'index.html'), encoding='utf-8') as f:
        html = f.read()
    start = html.find('<div class="project-brain-overlay"')
    assert start != -1, 'project-brain-overlay not found in index.html'
    end = html.find('<div class="chat-container"', start)
    assert end > start, 'could not bound the project-brain overlay'
    return html[start:end]


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const SRC = process.argv[2];
const ROOT = process.argv[3];
const FRAG = process.argv[4];
const fragment = fs.readFileSync(FRAG, 'utf8');
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body>' + fragment + '</body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;

win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s);
win.t = global.t = (k) => k;
win.Icon = global.Icon = () => '<svg></svg>';
win.activeConvId = global.activeConvId = 'c-self';
// The attention module resolves the path via ProjectBrain._state — provide the
// minimal shim (the real project-brain.js is not loaded in this unit test).
let _selectedTab = '';
win.ProjectBrain = global.ProjectBrain = {
  _state: { path: '/proj/real' },
  _selectTab: (n) => { _selectedTab = n; },
  _wireClampToggles: () => {},
  // The shared relative-time grammar (the REAL one lives in project-brain.js).
  _relTime: (ts) => (ts ? '2h ago' : ''),
  // The shared epic→conversation launcher (the REAL one lives in
  // project-brain.js, bundled before this module). The stub records the
  // delegation so the test can assert id + ORIGINAL title are handed over.
  _openEpicConversation: (id, title) => { _calls.push(['createConv', id, title]); },
};
// Record every API call so the tests can assert WHICH backend route a control
// hits (the "one contract per action" invariant — no reimplementation).
const _calls = [];
// The provenance chip deep-links into the originating conversation.
win.loadConversation = global.loadConversation = (id) => { _calls.push(['loadConversation', id]); };
win.Api = global.Api = { project: {
  boardAnswer: (p, id, conv, ans) => { _calls.push(['boardAnswer', id, ans]); return Promise.resolve({}); },
  commitCharter: (p, body) => { _calls.push(['commitCharter', body.resolves_proposal, body.add_decision]); return Promise.resolve({}); },
  dismissProposal: (p, id) => { _calls.push(['dismissProposal', id]); return Promise.resolve({}); },
  brainAttention: () => Promise.resolve({}),
}};

eval(fs.readFileSync(SRC, 'utf8'));  // project-brain-attention.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const PBA = win.ProjectBrainAttention;
check('module_exposed', !!(PBA && typeof PBA.renderAttention === 'function'));
const body = win.document.getElementById('projectBrainAttentionBody');
check('real_panel_element_exists', !!body);

// ── Scenario: one blocking epic + one advisory proposal, in the SERVER's
//    priority order (blocking first), plus 2 on cooldown. (A live file
//    conflict is deliberately NOT an attention item — 2026-08-01 owner
//    directive — so none appears in this fixture.) ──
PBA.renderAttention({
  blocking: 1, advisory: 1, needsYou: 2, waiting: 2,
  items: [
    { type: 'board_question', severity: 'blocking', id: 'pt_halted',
      title: 'Migrate the schema', question: 'Postgres or SQLite?',
      options: [{ label: 'Postgres', description: 'full power, more ops' },
                { label: 'SQLite', description: 'zero ops, single box' }],
      reason: '[human-gated] needs a call', blockCount: 3, tab: 'board',
      askedByConvId: 'conv-asker', askedByTitle: 'Egress 调研', ts: 1754000000000 },
    { type: 'charter_proposal', severity: 'advisory', id: 'prop_1',
      text: 'Adopt the new parser', tab: 'charter',
      convId: 'conv-proposer', askedByConvId: 'conv-proposer',
      askedByTitle: '架构决策讨论' },
  ],
});

const html = body.innerHTML;
const cards = body.querySelectorAll('.pb-attn-card');
check('two_cards', cards.length === 2);
// SERVER ORDER PRESERVED — the blocking card is first. The module must not
// re-sort; the backend already ordered it (that is the SSOT contract).
check('blocking_card_first', cards[0] && cards[0].classList.contains('pb-attn-blocking'));
check('blocking_is_the_epic', cards[0] && cards[0].getAttribute('data-attn-type') === 'board_question');
check('advisory_card_follows',
  cards[1] && cards[1].classList.contains('pb-attn-advisory') && !cards[2]);
// The halted epic renders its question + INLINE resolving controls.
check('question_rendered', html.indexOf('Postgres or SQLite?') !== -1);
check('option_chips', body.querySelectorAll('.pb-attn-act[data-act="answerOpt"]').length === 2);
check('answer_input', !!body.querySelector('.pb-attn-answer'));
check('answer_submit', !!body.querySelector('.pb-attn-act[data-act="answerSubmit"]'));
// ── Provenance + background (2026-08 owner complaint): WHO asked, WHY it
//    stopped, WHEN, and what each option MEANS — all ON the card. ──
const fromChip = cards[0] ? cards[0].querySelector('.pb-attn-from[data-conv-id="conv-asker"]') : null;
check('from_chip_rendered', !!fromChip && fromChip.textContent.indexOf('Egress 调研') !== -1);
check('reason_section', !!cards[0].querySelector('.pb-attn-reason') &&
      cards[0].querySelector('.pb-attn-reason').textContent.indexOf('[human-gated] needs a call') !== -1);
check('yourcall_label', html.indexOf('projectBrain.attnYourCall') !== -1 ||
      html.indexOf('Your call') !== -1);
const chipRowText = (cards[0].querySelector('.pb-chip-row') || {}).textContent || '';
check('option_desc_visible', cards[0].querySelectorAll('.pb-attn-opt-desc').length === 2 &&
      chipRowText.indexOf('zero ops') !== -1 && chipRowText.indexOf('full power') !== -1);
check('rel_time_rendered', !!cards[0].querySelector('.pb-attn-meta') &&
      cards[0].querySelector('.pb-attn-meta').textContent.indexOf('2h ago') !== -1);
if (fromChip) fromChip.click();
check('from_chip_loads_conv',
      !!_calls.find(c => c[0] === 'loadConversation' && c[1] === 'conv-asker'));
// The SAME provenance chip on the proposal card — every decision the panel
// shows must carry its author, not only the blocking one.
const propChip = cards[1] ? cards[1].querySelector('.pb-attn-from[data-conv-id="conv-proposer"]') : null;
check('proposal_from_chip', !!propChip && propChip.textContent.indexOf('架构决策讨论') !== -1);
if (propChip) propChip.click();
check('proposal_from_loads_conv',
      !!_calls.find(c => c[0] === 'loadConversation' && c[1] === 'conv-proposer'));
// The proposal renders commit + reject inline.
check('commit_btn', !!body.querySelector('.pb-attn-act[data-act="commit"]'));
check('reject_btn', !!body.querySelector('.pb-attn-act[data-act="reject"]'));
// Cooldown epics are a muted reassurance FOOTNOTE, never cards (they need
// nothing from the human — listing them would devalue the surface).
check('waiting_footnote', !!body.querySelector('.pb-attn-waiting'));
check('waiting_not_a_card', cards.length === 2);
// Tab badge reflects the count AND the blocking alarm.
const badge = win.document.getElementById('pbTabCountAttention');
check('badge_count', badge && badge.textContent === '2' && badge.hidden === false);
check('badge_blocking_class', badge && badge.classList.contains('pb-tab-count-blocking'));

// ── Clicking an option chip must call boardAnswer with the option label ──
const chip = body.querySelector('.pb-attn-act[data-act="answerOpt"][data-idx="1"]');
if (chip) chip.click();
// ── Reject must call dismissProposal with the proposal id ──
const reject = body.querySelector('.pb-attn-act[data-act="reject"]');
if (reject) reject.click();
// ── "New chat" on the halted-epic card delegates to the shared launcher ──
const convBtn = cards[0] ? cards[0].querySelector('.pb-attn-act[data-act="createConv"]') : null;
check('createconv_btn_on_epic', !!convBtn);
check('createconv_absent_on_proposal',
      cards[1] && !cards[1].querySelector('[data-act="createConv"]'));
if (convBtn) convBtn.click();

// ── Focus channel (Board "go answer" deep-link): scroll + flash the card ──
PBA.focusItem('pt_halted');
check('focus_flashes_card',
      !!(cards[0] && cards[0].classList.contains('pb-attn-flash')));
// An id whose card is NOT rendered yet stays pending and is honored by the
// NEXT render (the tab's data loads async — the deep-link must survive that).
PBA.focusItem('pt_future');
check('focus_not_flashed_before_render',
      !body.querySelector('.pb-attn-card[data-attn-id="pt_future"].pb-attn-flash'));
PBA.renderAttention({ blocking: 1, advisory: 0, needsYou: 1, waiting: 0,
  items: [{ type: 'board_question', severity: 'blocking', id: 'pt_future',
            title: 'A later epic', question: 'Which default?',
            options: [], reason: '', blockCount: 1, tab: 'board' }] });
check('focus_pending_honored_after_render',
      !!body.querySelector('.pb-attn-card[data-attn-id="pt_future"].pb-attn-flash'));

setTimeout(() => {
  const answered = _calls.find(c => c[0] === 'boardAnswer');
  check('answer_hits_boardAnswer', !!answered && answered[1] === 'pt_halted');
  check('answer_sends_option_label', !!answered && answered[2] === 'SQLite');
  const dismissed = _calls.find(c => c[0] === 'dismissProposal');
  check('reject_hits_dismissProposal', !!dismissed && dismissed[1] === 'prop_1');
  const launched = _calls.find(c => c[0] === 'createConv');
  check('createconv_delegates', !!launched && launched[1] === 'pt_halted');
  check('createconv_sends_original_title',
        !!launched && launched[2] === 'Migrate the schema');

  // ── Empty state: a POSITIVE statement + the reassurance count ──
  PBA.renderAttention({ blocking: 0, advisory: 0, needsYou: 0, waiting: 3, items: [] });
  const empty = body.innerHTML;
  check('empty_state', !!body.querySelector('.pb-attn-empty'));
  check('empty_no_cards', body.querySelectorAll('.pb-attn-card').length === 0);
  check('empty_mentions_waiting', empty.indexOf('attnEmptyWaiting') !== -1);
  const b2 = win.document.getElementById('pbTabCountAttention');
  check('empty_badge_hidden', b2 && b2.hidden === true);
  check('empty_badge_not_blocking', b2 && !b2.classList.contains('pb-tab-count-blocking'));

  // ── Advisory-only: the badge must NOT wear the blocking alarm ──
  PBA.renderAttention({ blocking: 0, advisory: 1, needsYou: 1, waiting: 0,
    items: [{ type: 'charter_proposal', severity: 'advisory', id: 'p2',
              text: 'Adopt Y', tab: 'charter' }] });
  const b3 = win.document.getElementById('pbTabCountAttention');
  check('advisory_badge_calm', b3 && b3.hidden === false &&
    !b3.classList.contains('pb-tab-count-blocking'));

  console.log(out.join('\n'));
  process.exit(0);
}, 30);
"""


def _write_and_run(harness_src, js_src, tag):
    frag_file = os.path.join(HERE, '_attn_frag_%s.html' % tag)
    harness = os.path.join(HERE, '_attn_harness_%s.js' % tag)
    with open(frag_file, 'w', encoding='utf-8') as f:
        f.write(_extract_panel_fragment())
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(harness_src)
    try:
        proc = subprocess.run(['node', harness, js_src, ROOT, frag_file],
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


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_attention_tab_renders_and_resolves_inline():
    output = _write_and_run(_HARNESS, _ATTN_SRC, 'main')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'attention-tab failures:\n' + output
    for must in ('PASS two_cards', 'PASS blocking_card_first',
                 'PASS blocking_is_the_epic', 'PASS question_rendered',
                 'PASS option_chips', 'PASS answer_submit',
                 'PASS from_chip_rendered', 'PASS from_chip_loads_conv',
                 'PASS proposal_from_chip', 'PASS proposal_from_loads_conv',
                 'PASS reason_section', 'PASS yourcall_label',
                 'PASS option_desc_visible', 'PASS rel_time_rendered',
                 'PASS commit_btn', 'PASS reject_btn',
                 'PASS waiting_footnote',
                 'PASS waiting_not_a_card', 'PASS badge_blocking_class',
                 'PASS answer_hits_boardAnswer', 'PASS answer_sends_option_label',
                 'PASS reject_hits_dismissProposal',
                 'PASS createconv_btn_on_epic', 'PASS createconv_absent_on_proposal',
                 'PASS createconv_delegates',
                 'PASS createconv_sends_original_title',
                 'PASS focus_flashes_card', 'PASS focus_not_flashed_before_render',
                 'PASS focus_pending_honored_after_render',
                 'PASS empty_state', 'PASS empty_mentions_waiting',
                 'PASS advisory_badge_calm'):
        assert must in output, output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_server_order_is_preserved():
    """NC: make the renderer re-sort items by type instead of trusting the
    server's order → a blocking-first payload no longer renders blocking-first
    → blocking_card_first FAILS.

    This is the SSOT contract: the panel and the collab bar must express ONE
    judgment. A frontend that re-derives priority is exactly how the bar's
    count and the panel's list drift apart.

    The neuter sorts ADVISORY-FIRST explicitly. An earlier draft used an
    alphabetical sort by `type` and the test still passed — 'board_question'
    happens to sort before 'charter_proposal', so the blocking card stayed
    first by luck. A neuter has to actually invert the property under test.
    """
    with open(_ATTN_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = "    for (var i = 0; i < items.length; i++) {\n      var it = items[i] || {};"
    assert anchor in original, 'render-loop anchor not found (source changed?)'
    patched = original.replace(
        anchor,
        ("    items = items.slice().sort(function (a, b) {  // NC (client re-sort)\n"
         "      var ra = a.severity === 'blocking' ? 1 : 0;\n"
         "      var rb = b.severity === 'blocking' ? 1 : 0;\n"
         "      return ra - rb; });\n"
         "    for (var i = 0; i < items.length; i++) {\n      var it = items[i] || {};"),
        1)
    copy_path = os.path.join(HERE, '_attn_nc_order.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _write_and_run(_HARNESS, copy_path, 'ncorder')
        assert 'FAIL blocking_card_first' in output, \
            ('NC: a client-side re-sort must break the server-order '
             'contract:\n' + output)
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_ATTN_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain-attention.js must be byte-identical'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_provenance_chip_is_load_bearing():
    """NC: drop the provenance lookup in a COPY → BOTH decision cards render
    without the from-chip → from_chip_rendered AND proposal_from_chip FAIL.

    The 2026-08 owner complaint was precisely "no indication of which
    conversation sent this". The shared `_fromChip` builder is what makes
    the chip impossible to lose on ONE card while keeping the other — this
    neuter proves both cards draw it from the SAME wiring, not from two
    hand-copies that could drift apart."""
    with open(_ATTN_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = "    var fromId = item.askedByConvId || item.convId || '';"
    assert anchor in original, 'fromId anchor not found (source changed?)'
    patched = original.replace(
        anchor, "    var fromId = '';  // NC (provenance dropped)", 1)
    copy_path = os.path.join(HERE, '_attn_nc_from.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _write_and_run(_HARNESS, copy_path, 'ncfrom')
        assert 'FAIL from_chip_rendered' in output, \
            ('NC: dropping the provenance lookup must remove the from-chip '
             'from the halted-epic card:\n' + output)
        assert 'FAIL proposal_from_chip' in output, \
            ('NC: the SAME neuter must remove it from the proposal card too '
             '— one builder, not two copies:\n' + output)
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_ATTN_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain-attention.js must be byte-identical'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_createconv_delegation_is_load_bearing():
    """NC: strip the launcher call in a COPY → the button renders but hands
    nothing over → createconv_delegates FAILS. A rendered-but-inert action is
    the same dead-button disease on the new surface."""
    with open(_ATTN_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = '      launcher(id, ttl);'
    assert anchor in original, 'createConv anchor not found (source changed?)'
    patched = original.replace(
        anchor, '      if (false) launcher(id, ttl);  // NC', 1)
    copy_path = os.path.join(HERE, '_attn_nc_createconv.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _write_and_run(_HARNESS, copy_path, 'nccreateconv')
        assert 'FAIL createconv_delegates' in output, \
            ('NC: without the delegation, "New chat" must hand nothing to the '
             'launcher:\n' + output)
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_ATTN_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain-attention.js must be byte-identical'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_focus_pending_is_honored_by_render():
    """NC: strip the render-time _applyFocus() in a COPY → a pending focus id
    is never honored when the card arrives → focus_pending_honored_after_render
    FAILS. Without the render hook the deep-link only works when the attention
    data happens to be warm — an async race masquerading as a feature."""
    with open(_ATTN_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = ('    // A Board deep-link may have asked for a specific card BEFORE this render\n'
              '    // resolved — honor it now that the card exists.\n'
              '    _applyFocus();')
    assert anchor in original, 'render-focus anchor not found (source changed?)'
    patched = original.replace(
        anchor, '    // NC (render-time focus honor stripped)', 1)
    copy_path = os.path.join(HERE, '_attn_nc_focus.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _write_and_run(_HARNESS, copy_path, 'ncfocus')
        assert 'FAIL focus_pending_honored_after_render' in output, \
            ('NC: without the render hook, a pending focus must never be '
             'honored:\n' + output)
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_ATTN_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain-attention.js must be byte-identical'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_blocking_drives_badge_alarm():
    """NC: drive the badge alarm off the raw COUNT instead of `blocking` → an
    advisory-only project renders alarmed → advisory_badge_calm FAILS.

    This is the inversion fix stated as a contract: a pending charter proposal
    must not make the UI shout, because it stops nothing.
    """
    with open(_ATTN_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = "      el.classList.toggle('pb-tab-count-blocking', blocking > 0);"
    assert anchor in original, 'badge-alarm anchor not found (source changed?)'
    patched = original.replace(
        anchor,
        "      el.classList.toggle('pb-tab-count-blocking', n > 0);  // NC (count-driven)",
        1)
    copy_path = os.path.join(HERE, '_attn_nc_alarm.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _write_and_run(_HARNESS, copy_path, 'ncalarm')
        assert 'FAIL advisory_badge_calm' in output, \
            ('NC: a count-driven alarm must make an advisory-only project '
             'render alarmed:\n' + output)
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_ATTN_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain-attention.js must be byte-identical'


# ════════════════════════════════════════════════════════════════════
#  The collaboration bar's attention segment
# ════════════════════════════════════════════════════════════════════


def _extract_strip_fragment():
    with open(os.path.join(ROOT, 'index.html'), encoding='utf-8') as f:
        html = f.read()
    needle = 'id="presenceStrip"'
    i = html.find(needle)
    assert i != -1, 'presenceStrip not found in index.html'
    start = html.rfind('<div', 0, i)
    end = html.find('</div>', i) + len('</div>')
    return html[start:end]


_BAR_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const SRC = process.argv[2];
const ROOT = process.argv[3];
const FRAG = process.argv[4];
const fragment = fs.readFileSync(FRAG, 'utf8');
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body>' + fragment + '</body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;

win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s);
win.t = global.t = (k, params) => {
  if (params && typeof params === 'object') {
    let s = k;
    for (const key of Object.keys(params)) s += ' ' + params[key];
    return s;
  }
  return k;
};
win.activeConvId = global.activeConvId = 'c-self';
win.getActiveConv = global.getActiveConv = () => ({ id: 'c-self', projectPath: '/proj/real' });
win._getConvProjectPath = global._getConvProjectPath = (c) => (c && c.projectPath) || '';
win.pushSubscribe = global.pushSubscribe = () => {};
win.pushUnsubscribe = global.pushUnsubscribe = () => {};
let _openArg = null;
win.openProjectBrain = global.openProjectBrain = (o) => { _openArg = o || null; };
win.Api = global.Api = { project: {} };

eval(fs.readFileSync(SRC, 'utf8'));  // presence.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
const CB = win.CollabBar;
const stripEl = win.document.getElementById('presenceStrip');

// ── BLOCKING: one halted epic + one proposal → bar shows 2, reads urgent ──
CB._setSummary('/proj/real', {
  epicsOpen: 0, epicsClaimed: 1, epicsDone: 0,
  pendingDecisions: 1, needsYou: 2, blocking: 1, advisory: 1, waiting: 0,
  activePeers: 0, charterExists: true, peerEpics: {},
});
CB._setPeers('/proj/real', []);
CB._render();
let html = stripEl.innerHTML;
check('needsyou_segment', html.indexOf('collab-seg-needsyou') !== -1);
check('needsyou_count_is_aggregate', html.indexOf('needsYouBlocking 2') !== -1);
check('blocking_seg_class', html.indexOf('collab-seg-blocking') !== -1);
check('bar_reads_blocking', html.indexOf('collab-has-blocking') !== -1);
// The legacy class is kept as an ALIAS so existing styling/selectors survive.
check('legacy_decisions_alias', html.indexOf('collab-seg-decisions') !== -1);
// Clicking hands the count to the panel so it lands on the Needs-you tab.
const inner = stripEl.querySelector('.collab-bar-inner');
if (inner) inner.click();
check('click_passes_needsyou', !!_openArg && _openArg.needsYou === 2);

// ── ADVISORY-ONLY: a proposal blocks nothing → the bar must stay CALM ──
CB._setSummary('/proj/real', {
  epicsOpen: 0, epicsClaimed: 1, epicsDone: 0,
  pendingDecisions: 1, needsYou: 1, blocking: 0, advisory: 1, waiting: 0,
  activePeers: 0, charterExists: true, peerEpics: {},
});
CB._render();
html = stripEl.innerHTML;
check('advisory_segment_present', html.indexOf('collab-seg-needsyou') !== -1);
check('advisory_wording', html.indexOf('needsYou 1') !== -1);
check('advisory_bar_calm', html.indexOf('collab-has-blocking') === -1);
check('advisory_seg_not_blocking', html.indexOf('collab-seg-blocking') === -1);

// ── LEGACY BACKEND: no needsYou field → the old segment still renders ──
CB._setSummary('/proj/real', {
  epicsOpen: 0, epicsClaimed: 1, epicsDone: 0, pendingDecisions: 2,
  activePeers: 0, charterExists: true, peerEpics: {},
});
CB._render();
html = stripEl.innerHTML;
check('legacy_fallback_segment', html.indexOf('decisionsAwaiting 2') !== -1);

console.log(out.join('\n'));
process.exit(0);
"""


def _run_bar(js_src, tag):
    frag_file = os.path.join(HERE, '_attn_bar_frag_%s.html' % tag)
    harness = os.path.join(HERE, '_attn_bar_harness_%s.js' % tag)
    with open(frag_file, 'w', encoding='utf-8') as f:
        f.write(_extract_strip_fragment())
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_BAR_HARNESS)
    try:
        proc = subprocess.run(['node', harness, js_src, ROOT, frag_file],
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


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_collab_bar_attention_segment():
    output = _run_bar(_PRESENCE_SRC, 'main')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'collab-bar attention failures:\n' + output
    for must in ('PASS needsyou_segment', 'PASS needsyou_count_is_aggregate',
                 'PASS bar_reads_blocking', 'PASS legacy_decisions_alias',
                 'PASS click_passes_needsyou', 'PASS advisory_bar_calm',
                 'PASS advisory_seg_not_blocking', 'PASS legacy_fallback_segment'):
        assert must in output, output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_bar_alarm_keys_on_blocking_not_count():
    """NC: drive the bar's alarm off the raw needsYou COUNT instead of
    `blocking` → an advisory-only project renders alarmed → advisory_bar_calm
    FAILS.

    This is THE regression guard for the original inversion: the bar used to
    emphasise a pending-proposal count, which stops nothing.
    """
    with open(_PRESENCE_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = "    const hasBlocking = !!(summary && (summary.blocking || 0) > 0);"
    assert anchor in original, 'bar-alarm anchor not found (source changed?)'
    patched = original.replace(
        anchor, "    const hasBlocking = needsYou > 0;  // NC (count-driven)", 1)
    copy_path = os.path.join(HERE, '_attn_bar_nc.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _run_bar(copy_path, 'nc')
        assert 'FAIL advisory_bar_calm' in output, \
            ('NC: a count-driven bar alarm must make an advisory-only project '
             'render alarmed:\n' + output)
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_PRESENCE_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped presence.js must be byte-identical'


# ════════════════════════════════════════════════════════════════════
#  The proposal card's Commit / Reject controls must actually RESOLVE
#
#  Reported symptom: clicking 确认 (Commit) in the Needs-you tab did nothing —
#  the proposal stayed and nothing was said. Two defects behind it:
#
#    1. The commit route REQUIRES a one-line `summary` (the binding rule the
#       per-turn injection renders) and 400s without it. This card shipped a
#       bare button that submitted none, so EVERY click was rejected. The
#       Charter tab's commit control has always rendered that input — the
#       "one contract per action" claim held for the URL but not the PAYLOAD,
#       which is the half that decides whether the click does anything.
#    2. The rejection was swallowed into console.warn, so a refused mutation
#       was indistinguishable from a dead button.
#
#  These assert the CONSEQUENCES (what goes on the wire, what the user sees),
#  not the presence of a particular DOM node, so a restyled card still passes.
# ════════════════════════════════════════════════════════════════════

_COMMIT_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const SRC = process.argv[2];
const ROOT = process.argv[3];
const FRAG = process.argv[4];
const fragment = fs.readFileSync(FRAG, 'utf8');
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body>' + fragment +
  '<div id="toastContainer"></div></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;

win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s).replace(/"/g, '&quot;');
win.t = global.t = (k) => k;
win.Icon = global.Icon = () => '<svg></svg>';
win.activeConvId = global.activeConvId = 'c-self';
const TOASTS = [];
win.showToast = global.showToast = (m, ty) => TOASTS.push(String(ty) + ': ' + String(m));
// Real clamp semantics: the ORIGINAL text stays retrievable from data-pb-src
// even when the translation overlay repaints innerHTML.
win.ProjectBrain = global.ProjectBrain = {
  _state: { path: '/proj/real' },
  _selectTab: () => {},
  _wireClampToggles: () => {},
  _mdLite: (x) => String(x),
  _clampBlock: (inner, raw) =>
    '<div class="pb-clamp" data-pb-src="' + String(raw).replace(/"/g, '&quot;') +
    '">' + inner + '</div>',
};
let FAIL = false;
const CALLS = [];
win.Api = global.Api = { project: {
  commitCharter: (p, body) => {
    CALLS.push(['commit', body]);
    return FAIL ? Promise.reject(new Error('HTTP 400 add_decision requires summary'))
                : Promise.resolve({});
  },
  dismissProposal: (p, id) => { CALLS.push(['dismiss', id]); return Promise.resolve({}); },
  brainAttention: () => Promise.resolve({}),
}};

eval(fs.readFileSync(SRC, 'utf8'));  // project-brain-attention.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
const PBA = win.ProjectBrainAttention;
const body = win.document.getElementById('projectBrainAttentionBody');

// A realistic proposal: a headline first line, then detail, LONGER than the
// clamp threshold so it renders through the clamp path (as the real one does).
const HEAD = 'RULE: keep the two gates separate';
const TEXT = HEAD + '\n' + 'detail '.repeat(120) + 'END';
function renderOne(id, text) {
  PBA.renderAttention({ blocking: 0, advisory: 1, needsYou: 1, waiting: 0,
    items: [{ type: 'charter_proposal', severity: 'advisory', id: id,
              text: text, tab: 'charter' }] });
}
function commitBtn() { return body.querySelector('.pb-attn-act[data-act="commit"]'); }

renderOne('prop_abc', TEXT);
commitBtn().click();

setTimeout(() => {
  const sent = CALLS.find(c => c[0] === 'commit');
  const b = sent ? sent[1] : null;
  // ── THE bug: a commit with no summary is refused by the route ──
  check('commit_was_attempted', !!b);
  check('commit_sends_summary', !!(b && (b.summary || '').trim()));
  check('summary_is_one_line', !!(b && (b.summary || '').indexOf('\n') === -1));
  check('summary_from_proposal_head', !!(b && b.summary === HEAD));
  // The decision text must be the WHOLE proposal, never the clamp's view.
  check('commit_sends_full_text', !!(b && b.add_decision === TEXT));
  check('commit_carries_proposal_id', !!(b && b.resolves_proposal === 'prop_abc'));

  // ── An empty summary must be blocked CLIENT-side, not sent and 400'd ──
  CALLS.length = 0;
  renderOne('prop_empty', '');
  const cb = commitBtn();
  check('empty_summary_disables_commit', !!cb && cb.disabled === true);
  cb.disabled = false;          // force the click past the disabled attribute
  cb.click();
  setTimeout(() => {
    check('empty_summary_sends_nothing', CALLS.length === 0);

    // ── A refused mutation must be VISIBLE (not console-only) ──
    FAIL = true; CALLS.length = 0; TOASTS.length = 0;
    renderOne('prop_fail', TEXT);
    commitBtn().click();
    setTimeout(() => {
      check('failure_is_surfaced', TOASTS.length === 1);
      check('failure_names_the_reason',
        (TOASTS[0] || '').indexOf('requires summary') !== -1);
      check('failure_reenables_button', !!commitBtn() && commitBtn().disabled === false);

      // ── Reject still resolves by id (unchanged contract) ──
      FAIL = false; CALLS.length = 0;
      renderOne('prop_rej', TEXT);
      body.querySelector('.pb-attn-act[data-act="reject"]').click();
      setTimeout(() => {
        const d = CALLS.find(c => c[0] === 'dismiss');
        check('reject_sends_proposal_id', !!d && d[1] === 'prop_rej');
        console.log(out.join('\n'));
        process.exit(0);
      }, 30);
    }, 30);
  }, 30);
}, 30);
"""

_COMMIT_MUSTS = (
    'PASS commit_was_attempted',
    'PASS commit_sends_summary',
    'PASS summary_is_one_line',
    'PASS summary_from_proposal_head',
    'PASS commit_sends_full_text',
    'PASS commit_carries_proposal_id',
    'PASS empty_summary_disables_commit',
    'PASS empty_summary_sends_nothing',
    'PASS failure_is_surfaced',
    'PASS failure_names_the_reason',
    'PASS failure_reenables_button',
    'PASS reject_sends_proposal_id',
)


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_proposal_commit_sends_the_payload_the_route_requires():
    output = _write_and_run(_COMMIT_HARNESS, _ATTN_SRC, 'commit')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'proposal commit/reject failures:\n' + output
    for must in _COMMIT_MUSTS:
        assert must in output, output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_commit_without_summary_is_a_dead_button():
    """NC: drop `summary` from the commit payload (the shipped bug) → the route
    contract is violated → commit_sends_summary FAILS.

    The neuter removes ONLY the payload field, leaving the input rendered, so it
    isolates "does the click carry what the route requires" from "is there an
    input on screen" — a card that renders the box but forgets to read it is the
    same dead button to the user.
    """
    with open(_ATTN_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = 'add_decision: txt, summary: summary, resolves_proposal: id,'
    assert anchor in original, 'commit-payload anchor not found (source changed?)'
    patched = original.replace(
        anchor, 'add_decision: txt, resolves_proposal: id,  // NC (summary dropped)', 1)
    copy_path = os.path.join(HERE, '_attn_nc_summary.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _write_and_run(_COMMIT_HARNESS, copy_path, 'ncsummary')
        assert 'FAIL commit_sends_summary' in output, \
            ('NC: a commit that omits the route-required summary must break the '
             'payload contract:\n' + output)
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_ATTN_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain-attention.js must be byte-identical'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_swallowed_failure_looks_like_a_dead_button():
    """NC: swallow the rejection into console.warn only (the shipped behaviour)
    → nothing reaches the user → failure_is_surfaced FAILS.

    This is the half that made the bug so hard to see from the outside: with the
    payload wrong AND the error invisible, a rejected click and a broken
    listener are the same observation.
    """
    with open(_ATTN_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = "    if (typeof showToast === 'function') {"
    assert anchor in original, 'failure-surface anchor not found (source changed?)'
    patched = original.replace(
        anchor, '    if (false) {  // NC (failure swallowed)', 1)
    copy_path = os.path.join(HERE, '_attn_nc_silent.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _write_and_run(_COMMIT_HARNESS, copy_path, 'ncsilent')
        assert 'FAIL failure_is_surfaced' in output, \
            ('NC: a silently-swallowed rejection must leave the user with no '
             'signal:\n' + output)
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_ATTN_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain-attention.js must be byte-identical'


# ════════════════════════════════════════════════════════════════════
#  Panel-wide invariant: a REFUSED mutation must reach the user
#
#  The reported "dead button" was two defects wearing one face — a payload the
#  route refused, and a rejection swallowed into console.warn. Fixing only the
#  Needs-you tab would leave the SAME disease in the Charter/Board tab, whose
#  13 mutation handlers all ended in a bare console.warn. So the invariant is
#  stated over the WHOLE panel source, not over one function: to a user, a
#  refused write and a broken listener are the same observation.
#
#  Asserted over the REAL catch set (every `.catch(` in the file), so a handler
#  added next month is covered without editing this test — the failure mode a
#  hand-listed set of function names cannot catch.
# ════════════════════════════════════════════════════════════════════

_BRAIN_SRC = os.path.join(JS_DIR, 'project-brain.js')


def _catch_bodies(src):
    """Yield (line_no, body) for every `.catch(function (x) { … })` block.

    Brace-matched rather than regex-sliced: several handlers contain nested
    functions, which a naive slice would truncate — hiding whether the handler
    reports anything at all.
    """
    out = []
    idx = 0
    while True:
        i = src.find('.catch(', idx)
        if i == -1:
            break
        idx = i + 1
        brace = src.find('{', i)
        if brace == -1:
            continue
        depth, j = 0, brace
        while j < len(src):
            if src[j] == '{':
                depth += 1
            elif src[j] == '}':
                depth -= 1
                if depth == 0:
                    break
            j += 1
        out.append((src.count('\n', 0, i) + 1, src[brace:j + 1]))
    return out


def test_every_panel_failure_reaches_the_user():
    """No handler may end in console-only."""
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        src = f.read()
    bodies = _catch_bodies(src)
    assert len(bodies) >= 10, \
        f'the catch parser must actually find the handlers (found {len(bodies)})'
    offenders = [f'project-brain.js:{ln} logs but never tells the user'
                 for ln, body in bodies
                 if '_reportFailure' not in body
                 and ('console.warn' in body or 'console.error' in body)]
    assert not offenders, (
        'a refused Project Brain operation must produce a user-visible signal '
        '(the reported "dead button" was exactly this):\n  ' +
        '\n  '.join(offenders))


def test_reporter_is_shared_not_duplicated():
    """The Needs-you tab must DELEGATE to project-brain.js's reporter.

    Two copies drift: the panel would grow two failure vocabularies and the
    invariant above would only hold for whichever file someone remembered to
    update. The attention module keeps a local fallback for the case where
    project-brain.js is absent, so this pins the DELEGATION, not the absence of
    any local code.
    """
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        brain = f.read()
    with open(_ATTN_SRC, encoding='utf-8') as f:
        attn = f.read()
    assert '_reportFailure: _reportFailure' in brain, \
        'project-brain.js must EXPORT the shared reporter'
    assert 'runtimeScope.ProjectBrain._reportFailure' in attn, \
        ('the Needs-you tab must delegate through the runtime seam to the '
         'shared reporter, not fork it')


def test_charter_tab_commit_sends_no_expected_version():
    """Committing a proposal is a pure APPEND, so the Charter tab must not pin
    the version it rendered.

    Sibling agents self-commit decisions constantly, so that version is stale
    by the time a human clicks — pinning it made the button 409 exactly when
    the project was busy, i.e. the same dead button as the other tab.
    Concurrent appends stay safe via the backend CAS, not via this pin.
    """
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        src = f.read()
    start = src.index('function _commitCharterDecision(')
    end = src.index('function _dismissProposal(', start)
    # Strip comments before asserting: the fix is DOCUMENTED in a comment that
    # names the very field it removes, so matching raw text would fail on the
    # explanation of why the field is gone. What must be absent is the field in
    # the PAYLOAD, not the word on the page.
    body = '\n'.join(
        ln for ln in src[start:end].splitlines()
        if not ln.lstrip().startswith('//'))
    assert 'expected_version' not in body, \
        ('the proposal commit must not send expected_version — an append '
         'commutes, so a stale version is not a conflict')
    assert 'add_decision' in body and 'summary' in body, \
        'it must still send the decision text and its required summary'
    assert 'content:' not in body, \
        ('and never content — content and add_decision are mutually '
         'exclusive, which is what makes the append replay-safe')


def test_NC_a_console_only_catch_is_caught():
    """NC: revert ONE handler to console-only → the panel-wide invariant FAILS.

    Guards against the test above passing vacuously: if `_catch_bodies` ever
    stopped finding handlers, it would assert over an empty list and go green
    while the panel was entirely silent again.
    """
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = "      _reportFailure('projectBrain.commitFailed', 'Commit failed', e);"
    assert anchor in original, 'commit-failure anchor not found (source changed?)'
    patched = original.replace(
        anchor,
        "      if (typeof console !== 'undefined') console.warn("
        "'[ProjectBrain] commit failed', e);  // NC", 1)
    bodies = _catch_bodies(patched)
    offenders = [b for _, b in bodies
                 if '_reportFailure' not in b and 'console.warn' in b]
    assert offenders, \
        'NC: a console-only catch must be reported by the panel-wide invariant'
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain.js must be byte-identical'


def test_NC_a_forked_reporter_is_caught():
    """NC: drop the export → the shared-reporter contract FAILS.

    Without the export the attention tab silently falls back to its own local
    copy, which is exactly the drift this pins.
    """
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        original = f.read()
    patched = original.replace('    _reportFailure: _reportFailure,\n', '', 1)
    assert patched != original, 'export anchor not found (source changed?)'
    assert '_reportFailure: _reportFailure' not in patched, \
        'NC: with the export removed the shared-reporter assertion must fail'
