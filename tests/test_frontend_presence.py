"""jsdom end-to-end test for the cross-conversation presence strip.

Loads the REAL shipped static/js/presence.js under jsdom, drives it with
`presence` push frames exactly as the backend broadcasts them, and asserts the
rendered DOM. The decisive checks prove the PURE-RENDER contract:

  • The "who's working" strip paints title + the backend-formed `statusLabel`
    VERBATIM and a relative time the frontend formats from `lastBeatTs`.
  • A `conflict` frame's `message` is rendered VERBATIM (backend-formed).
  • The strip filters to the project root of the DISPLAYED conversation, and
    EXCLUDES the conversation you're viewing (no cursor for yourself).
  • A `depart` frame removes the peer; an empty strip hides itself.
  • The frontend computes NOTHING but the timestamp: a peer whose statusLabel
    is a fabricated/unknown string is rendered as-is (the frontend never
    re-derives or overrides the backend status word).

A fake pushSubscribe captures the registered handler so the test can feed
frames synchronously. Skips cleanly when node + jsdom aren't installed.
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


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body><div class="chat-wrapper">' +
  '<div class="presence-strip" id="presenceStrip" hidden></div></body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
// setInterval must NOT actually fire in the harness (we render via the handler).
global.setInterval = win.setInterval = () => 0;

win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
// t(): echo the en value with simple {n} interpolation so labels are readable.
const _I18N = {
  'presence.title': 'Others working here',
  'presence.untitled': '(untitled)',
  'presence.justNow': 'just now',
  'presence.secsAgo': '{n}s ago',
  'presence.minsAgo': '{n}m ago',
};
win.t = global.t = (k, p) => {
  let s = _I18N[k] || k;
  if (p) for (const kk in p) s = s.replace(new RegExp('\\{' + kk + '\\}', 'g'), p[kk]);
  return s;
};

// Capture the presence push handler.
let _handler = null;
win.pushSubscribe = global.pushSubscribe = (channel, taskId, fn) => {
  if (channel === 'presence') _handler = fn;
};

// Displayed conversation = conv-self, project root /proj/A.
win.activeConvId = global.activeConvId = 'conv-self';
win.conversations = global.conversations = [
  { id: 'conv-self', projectPath: '/proj/A' },
];
win.getActiveConv = global.getActiveConv = () =>
  win.conversations.find(c => c.id === win.activeConvId) || null;
win._getConvProjectPath = global._getConvProjectPath = (conv) =>
  (conv && conv.projectPath) || '';

eval(fs.readFileSync(process.argv[2], 'utf8'));  // presence.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof _handler !== 'function') {
  console.log('FAIL handler_registered presence handler not registered');
  console.log(out.join('\n'));
  process.exit(0);
}
check('handler_registered', true);

const strip = document.getElementById('presenceStrip');
const now = Date.now();

// ── Update: a peer on the displayed root (other conversation) ──
_handler({ channel: 'presence', taskId: '*', type: 'presence', kind: 'update',
  root: '/proj/A',
  peer: { convId: 'conv-b', title: 'Refactor parser',
          objective: 'make the parser ship',
          status: 'active', statusLabel: 'editing lib/llm/stream.py',
          currentFile: 'lib/llm/stream.py', files: ['lib/llm/stream.py'],
          lastBeatTs: now, startedTs: now } });

check('strip_visible', strip.hidden === false);
check('peer_title_rendered', strip.innerHTML.indexOf('Refactor parser') !== -1);
// Backend statusLabel rendered VERBATIM (frontend did not re-derive it).
check('statuslabel_verbatim', strip.innerHTML.indexOf('editing lib/llm/stream.py') !== -1);
check('objective_rendered', strip.innerHTML.indexOf('make the parser ship') !== -1);
check('count_is_1', (strip.querySelector('.presence-count') || {}).textContent === '1');
// Relative time the FRONTEND formatted from lastBeatTs (the only computation).
check('reltime_just_now', strip.innerHTML.indexOf('just now') !== -1);

// ── Self-exclusion: an update for the DISPLAYED conversation is not shown ──
_handler({ channel: 'presence', taskId: '*', type: 'presence', kind: 'update',
  root: '/proj/A',
  peer: { convId: 'conv-self', title: 'MY OWN CONV',
          status: 'active', statusLabel: 'working', lastBeatTs: now } });
check('self_excluded', strip.innerHTML.indexOf('MY OWN CONV') === -1);
check('count_still_1', (strip.querySelector('.presence-count') || {}).textContent === '1');

// ── Root filtering: a peer on a DIFFERENT root must not appear ──
_handler({ channel: 'presence', taskId: '*', type: 'presence', kind: 'update',
  root: '/proj/OTHER',
  peer: { convId: 'conv-x', title: 'OTHER PROJECT WORK',
          status: 'active', statusLabel: 'working', lastBeatTs: now } });
check('other_root_filtered', strip.innerHTML.indexOf('OTHER PROJECT WORK') === -1);

// ── Pure-render proof: an UNKNOWN/fabricated statusLabel is rendered as-is.
//    The frontend has no status vocabulary of its own; whatever the backend
//    sends is what shows. ──
_handler({ channel: 'presence', taskId: '*', type: 'presence', kind: 'update',
  root: '/proj/A',
  peer: { convId: 'conv-c', title: 'Peer C',
          status: 'active', statusLabel: 'BACKEND-ONLY-PHRASE-XYZ',
          lastBeatTs: now } });
check('arbitrary_label_verbatim', strip.innerHTML.indexOf('BACKEND-ONLY-PHRASE-XYZ') !== -1);
check('count_is_2', (strip.querySelector('.presence-count') || {}).textContent === '2');

// ── Conflict: the advisory message is rendered VERBATIM ──
_handler({ channel: 'presence', taskId: '*', type: 'presence', kind: 'conflict',
  root: '/proj/A',
  conflict: { path: 'lib/llm/stream.py', peers: ['conv-b', 'conv-c'],
              message: 'Refactor parser and Peer C are concurrently editing lib/llm/stream.py' } });
check('conflict_rendered', !!strip.querySelector('.presence-conflict'));
check('conflict_message_verbatim',
  strip.innerHTML.indexOf('Refactor parser and Peer C are concurrently editing lib/llm/stream.py') !== -1);

// ── Depart: removing the two peers + (conflict still within TTL) ──
_handler({ channel: 'presence', taskId: '*', type: 'presence', kind: 'depart',
  root: '/proj/A', peer: { convId: 'conv-b' } });
_handler({ channel: 'presence', taskId: '*', type: 'presence', kind: 'depart',
  root: '/proj/A', peer: { convId: 'conv-c' } });
// No peer ROWS now, but the conflict advisory is still fresh → strip stays
// visible showing just the conflict. (Check the .presence-peer rows are gone,
// NOT the names — the conflict message legitimately still contains them.)
check('peer_rows_gone', strip.querySelectorAll('.presence-peer').length === 0);
check('conflict_persists_in_ttl', !!strip.querySelector('.presence-conflict'));

// ── HTML-injection safety: a malicious title is escaped, not interpreted ──
_handler({ channel: 'presence', taskId: '*', type: 'presence', kind: 'update',
  root: '/proj/A',
  peer: { convId: 'conv-evil', title: '<img src=x onerror=alert(1)>',
          status: 'active', statusLabel: 'working', lastBeatTs: now } });
check('title_escaped', strip.innerHTML.indexOf('<img src=x') === -1
                       && strip.innerHTML.indexOf('&lt;img src=x') !== -1);

// ════════════════════════════════════════════════════════════════════
// SUB-AGENT NESTING — two sub-agents of another conversation render as
// nested rows UNDER that conversation's peer; status/label verbatim.
// ════════════════════════════════════════════════════════════════════
// Fresh start: clear the mirror by departing everyone we added, then add a
// conversation peer with two sub-agents.
for (const cid of ['conv-b', 'conv-c', 'conv-evil']) {
  _handler({ channel: 'presence', taskId: '*', type: 'presence', kind: 'depart',
    root: '/proj/A', peer: { convId: cid } });
}
_handler({ channel: 'presence', taskId: '*', type: 'presence', kind: 'update',
  root: '/proj/A',
  peer: { convId: 'conv-swarm', title: 'Swarm session', agentId: '',
          status: 'active', statusLabel: 'working', lastBeatTs: now } });
_handler({ channel: 'presence', taskId: '*', type: 'presence', kind: 'update',
  root: '/proj/A',
  peer: { convId: 'conv-swarm', agentId: 'agent-coder-1', title: 'coder',
          parentTitle: 'Swarm session', status: 'active',
          statusLabel: 'editing lib/a.py', currentFile: 'lib/a.py', lastBeatTs: now } });
_handler({ channel: 'presence', taskId: '*', type: 'presence', kind: 'update',
  root: '/proj/A',
  peer: { convId: 'conv-swarm', agentId: 'agent-coder-2', title: 'coder',
          parentTitle: 'Swarm session', status: 'active',
          statusLabel: 'editing lib/a.py', currentFile: 'lib/a.py', lastBeatTs: now } });

// The conversation peer renders as a group containing a .presence-subagents block.
check('sub_group_rendered', !!strip.querySelector('.presence-group'));
check('sub_block_rendered', !!strip.querySelector('.presence-subagents'));
// TWO distinct sub-agent rows (not collapsed into one).
check('two_subagent_rows', strip.querySelectorAll('.presence-subagent').length === 2);
// The parent conversation title still shows as the group header.
check('parent_title_shown', strip.innerHTML.indexOf('Swarm session') !== -1);
// Sub-agent backend statusLabel rendered verbatim inside the nested block.
check('subagent_label_verbatim',
  strip.querySelector('.presence-subagents').innerHTML.indexOf('editing lib/a.py') !== -1);
// Group count badge counts the CONVERSATION (1 group), not 3 peers.
check('group_count_is_1', (strip.querySelector('.presence-count') || {}).textContent === '1');

// Departing one sub-agent leaves the other nested row + the parent.
_handler({ channel: 'presence', taskId: '*', type: 'presence', kind: 'depart',
  root: '/proj/A', peer: { convId: 'conv-swarm', agentId: 'agent-coder-1' } });
check('one_subagent_left', strip.querySelectorAll('.presence-subagent').length === 1);
check('parent_survives_subagent_depart', strip.innerHTML.indexOf('Swarm session') !== -1);

console.log(out.join('\n'));
"""


def _run():
    harness = os.path.join(HERE, '_presence_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'presence.js'),   # argv[2]
             ROOT,                                   # argv[3]
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
    assert not fails, 'presence render failures:\n' + output
    assert output.count('PASS') >= 23, f'expected >=23 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_presence_strip_renders_end_to_end():
    _run()
