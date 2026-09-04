"""Frontend guards for the per-response interaction in the watch lane
(Increment 2 slice, 2026-08-05).

Owner directive: "each brain answer should be a conversation — click in to keep
asking, or request a fix." Every response on a watch item (goal / concern /
question) therefore carries two doors:

  • 继续追问 (Follow up) — an inline composer; the answer lands in the SAME
    append-only trail as a trigger='follow_up' entry, with the question
    rendered as a labelled line above it;
  • 请求修复 (Request fix) — an inline epic-draft editor pre-filled from the
    response; submitting rides the EXISTING human-gated board-post channel
    (never a new write path), so the brain dispatches the fix to a
    conversation the human can open from the Board tab.

What needs node rather than a source scan: the editors are built by branch
logic in the shipped renderer (which response anchors the follow-up, what the
fix title is drafted from, whether a second click closes the composer). Each
test loads the REAL ``static/js/project-brain-status.js`` into a node harness
with a minimal DOM and drives the exported ``buildWatchItem``.
"""

import json
import os
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit

_JS = runtime_section_path('project-brain-status.js')

_HARNESS = r"""
// ── Minimal DOM good enough for the builders under test ──────────────
function mkEl(tag) {
  const el = {
    tagName: String(tag).toUpperCase(),
    className: '', textContent: '', hidden: false, disabled: false,
    type: '', rows: 0, value: '', placeholder: '', title: '',
    children: [], _attrs: {}, _listeners: {}, parentNode: null,
    appendChild(c) { c.parentNode = el; el.children.push(c); return c; },
    removeChild(c) { el.children = el.children.filter(x => x !== c); return c; },
    setAttribute(k, v) { el._attrs[k] = String(v); },
    getAttribute(k) { return el._attrs[k] === undefined ? null : el._attrs[k]; },
    addEventListener(t, fn) { (el._listeners[t] = el._listeners[t] || []).push(fn); },
    click() { (el._listeners['click'] || []).forEach(fn => fn({})); },
    closest(sel) {
      const want = sel.replace(/^\./, '');
      let n = el;
      while (n) { if ((n.className || '').split(/\s+/).includes(want)) return n; n = n.parentNode; }
      return null;
    },
    querySelector(sel) {
      const want = sel.replace(/^\./, '');
      const walk = (n) => {
        for (const c of n.children) {
          if ((c.className || '').split(/\s+/).includes(want)) return c;
          const hit = walk(c); if (hit) return hit;
        }
        return null;
      };
      return walk(el);
    },
    get innerHTML() { return ''; },
    set innerHTML(_v) { el.children = []; },
  };
  return el;
}
const _byId = {};
global.document = {
  createElement: mkEl,
  createDocumentFragment: () => mkEl('fragment'),
  getElementById: (id) => _byId[id] || null,
};
global.window = { ProjectBrain: { _state: { path: '/proj/x' } }, activeConvId: 'convH' };
global.escapeHtml = (s) => String(s == null ? '' : s);
global.t = (k) => k;                       // labels come back as their key
global.Icon = () => '';
global.activeConvId = 'convH';             // the human's proxy conv (boardPost)
global.__fuCalls = [];
global.__boardCalls = [];
global.Api = { project: {
  brainWatchAddress: () => Promise.resolve({}),
  brainWatchUpdate: () => Promise.resolve({}),
  brainWatchPromote: () => Promise.resolve({}),
  brainWatchList: () => Promise.resolve({ items: [] }),
  brainWatchFollowUp: (...a) => { global.__fuCalls.push(a); return Promise.resolve({ ok: true }); },
  boardPost: (...a) => { global.__boardCalls.push(a); return Promise.resolve({ ok: true }); },
} };

require(process.env.PB_STATUS_JS);
const PBS = global.window.ProjectBrainStatus;

function flatten(node, out) {
  out = out || [];
  out.push({ cls: node.className || '', text: node.textContent || '', value: node.value || '' });
  (node.children || []).forEach(c => flatten(c, out));
  return out;
}
function hasClass(node, cls) { return flatten(node).some(n => (n.cls || '').split(/\s+/).includes(cls)); }
function textsOf(node) { return flatten(node).map(n => n.text).filter(Boolean); }
function countClass(node, cls) {
  return flatten(node).filter(n => (n.cls || '').split(/\s+/).includes(cls)).length;
}

global.__probe = { PBS, flatten, hasClass, textsOf, countClass, mkEl };
"""


def _run(script: str) -> dict:
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available')
    src = _HARNESS + '\n' + script
    env = dict(os.environ, PB_STATUS_JS=_JS)
    proc = subprocess.run([node, '-e', src], capture_output=True, text=True,
                          timeout=60, env=env)
    assert proc.returncode == 0, (
        f'node harness failed\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}')
    return json.loads(proc.stdout.strip().splitlines()[-1])


_CTX = '{charterVersion: 7}'

# A two-entry trail: seq 2 is a follow_up answer (question recorded in the
# evidence JSON), seq 1 the original assessment it followed up on.
_ITEM = json.dumps({
    'item_id': 'w1', 'kind': 'concern', 'text': 'Artifacts may desync',
    'status': 'open', 'promotionState': 'none', 'divergedSide': '',
    'injected': False, 'promotedAudit': False,
    'responses': [
        {'seq': 2, 'response': 'Follow-up answer text.', 'trigger': 'follow_up',
         'ts': 2000,
         'pillar_state': {'followUpQuestion': 'Why is seq1 worried?',
                          'anchorSeq': 1}},
        {'seq': 1, 'response': 'First assessment. PG seeding is stalled and '
                                'the release pipeline lags!',
         'trigger': 'manual', 'ts': 1000, 'pillar_state': {}},
    ]})


def test_every_response_carries_both_doors():
    """Latest response AND each trail row offer follow-up + request-fix — the
    'each answer is a conversation' contract."""
    out = _run(f"""
      const card = __probe.PBS.buildWatchItem({_ITEM}, {_CTX});
      console.log(JSON.stringify({{
        followUpBtns: __probe.countClass(card, 'pb-watch-resp-followup'),
        fixBtns: __probe.countClass(card, 'pb-watch-resp-fix'),
        texts: __probe.textsOf(card),
      }}));
    """)
    assert out['followUpBtns'] == 2, 'latest + 1 trail row = 2 follow-up doors'
    assert out['fixBtns'] == 2, 'latest + 1 trail row = 2 request-fix doors'
    assert 'projectBrain.watchFollowUp' in out['texts']
    assert 'projectBrain.watchRequestFix' in out['texts']


def test_the_follow_up_question_is_rendered_as_a_labelled_line():
    """A follow_up trail entry shows WHAT was asked above the answer — the
    thread must be readable without opening anything."""
    out = _run(f"""
      const card = __probe.PBS.buildWatchItem({_ITEM}, {_CTX});
      const q = __probe.flatten(card).filter(n =>
        (n.cls || '').split(/\\s+/).includes('pb-watch-followup-q'));
      console.log(JSON.stringify({{
        qCount: q.length,
        qText: q.length ? q[0].text : '',
        // A plain assessment must NOT grow a question line.
        plainHasQ: __probe.flatten(card).some(n =>
          (n.cls || '').split(/\\s+/).includes('pb-watch-followup-q') &&
          n.text.indexOf('First assessment') >= 0),
      }}));
    """)
    assert out['qCount'] == 1, 'exactly the follow_up entry shows the question'
    assert 'Why is seq1 worried?' in out['qText']
    assert 'projectBrain.watchFollowUpQ' in out['qText']
    assert out['plainHasQ'] is False


def test_follow_up_editor_anchors_the_clicked_response():
    """Submitting the composer under the LATEST response calls the follow-up
    API with (item_id, question, THAT response's seq) — the anchor is the
    whole point of putting the door on each answer."""
    out = _run(f"""
      const card = __probe.PBS.buildWatchItem({_ITEM}, {_CTX});
      // First follow-up door = the one on the latest response (seq 2).
      const doors = __probe.flatten(card).filter(() => false); // placeholder
      const findBtns = (n, acc) => {{
        acc = acc || [];
        if ((n.className || '').split(/\\s+/).includes('pb-watch-resp-followup')) acc.push(n);
        (n.children || []).forEach(c => findBtns(c, acc));
        return acc;
      }};
      const btn = findBtns(card)[0];
      btn.click();
      const ed = card.querySelector('.pb-watch-resp-editor');
      const ta = ed.querySelector('.pb-status-ask-input');
      ta.value = 'and what unblocks it?';
      // send = the pb-status-ask-btn inside the editor
      ed.querySelector('.pb-status-ask-btn').click();
      console.log(JSON.stringify({{ fu: global.__fuCalls }}));
    """)
    assert out['fu'] == [['w1', 'and what unblocks it?', 2]], out['fu']


def test_follow_up_door_toggles_closed_on_second_click():
    out = _run(f"""
      const card = __probe.PBS.buildWatchItem({_ITEM}, {_CTX});
      const findBtns = (n, acc) => {{
        acc = acc || [];
        if ((n.className || '').split(/\\s+/).includes('pb-watch-resp-followup')) acc.push(n);
        (n.children || []).forEach(c => findBtns(c, acc));
        return acc;
      }};
      const btn = findBtns(card)[0];
      btn.click();
      const opened = !!card.querySelector('.pb-watch-resp-editor');
      btn.click();
      const closed = !card.querySelector('.pb-watch-resp-editor');
      console.log(JSON.stringify({{ opened, closed }}));
    """)
    assert out['opened'] is True and out['closed'] is True


def test_request_fix_posts_a_prefilled_epic_via_the_board_channel():
    """The fix door pre-fills the FULL response — NOT just the first
    sentence (in a status diagnosis the praise leads and the actual problem
    lives in later sentences; owner 2026-08-05: 'why is only the first
    sentence posted as the task??') — in a multi-line editor, posted through
    the existing human-gated board-post channel, never a new write path."""
    out = _run(f"""
      const card = __probe.PBS.buildWatchItem({_ITEM}, {_CTX});
      const findBtns = (n, acc) => {{
        acc = acc || [];
        if ((n.className || '').split(/\\s+/).includes('pb-watch-resp-fix')) acc.push(n);
        (n.children || []).forEach(c => findBtns(c, acc));
        return acc;
      }};
      findBtns(card)[1].click();   // the trail-row door (seq 1)
      const ed = card.querySelector('.pb-watch-resp-editor');
      const input = ed.querySelector('.pb-watch-fix-title');
      const prefill = input.value;
      const tag = input.tagName;
      ed.querySelector('.pb-status-ask-btn').click();
      console.log(JSON.stringify({{ prefill, tag, board: global.__boardCalls }}));
    """)
    full = ('First assessment. PG seeding is stalled and the release '
            'pipeline lags!')
    assert out['prefill'] == full, out['prefill']
    assert out['tag'] == 'TEXTAREA', 'multi-line editor, not a one-line input'
    assert len(out['board']) == 1
    _path, opts = out['board'][0]
    assert opts['title'] == full
    assert opts['convId'] == 'convH', 'dispatch target = the displayed conv'


def test_trigger_label_knows_follow_up():
    out = _run("""
      console.log(JSON.stringify({
        label: __probe.PBS._triggerLabel('follow_up'),
      }));
    """)
    assert out['label'] == 'projectBrain.statusTrigFollowUp'
