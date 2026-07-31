"""jsdom end-to-end test for the Project **Collaboration Bar** (presence.js).

The old multi-row "who's working" presence strip was REPLACED (see the
presence.js header) by a single slim project-coordination bar docked under the
top bar. It no longer echoes per-peer activity you already see in the sidebar;
it surfaces the Project Brain's coordination state, action-ordered:

    🧠 Project · N conflicts · N decisions awaiting you · M in progress ·
       K open · P online          + per-peer "advancing «epic»" join lines

Contract this pins (loads the REAL shipped presence.js under jsdom):

  • The bar is PROJECT-scoped: no displayed project root → hidden.
  • Counts come from the backend one-shot summary (Api.project.brainSummary),
    rendered VERBATIM into `.collab-seg-decisions / -progress / -open / -peers /
    -conflict` — the frontend re-derives none of them. Peer count is
    backend-authoritative (summary.activePeers), falling back to / max-ed with
    the local push mirror so a degraded push stream never under-reports.
  • Each online peer that owns a live epic is joined to it: `.collab-peer-epic`
    → `.collab-epic-title` shows the epic title VERBATIM (the deep-collab join);
    the displayed conversation itself (selfId) is excluded.
  • A conflict's backend-formed message renders VERBATIM in `.collab-conflict-line`.
  • Nothing collaborative to surface (solo, empty board) → the whole bar hides.
  • HTML-injection safety: a malicious epic title is escaped, not interpreted.
  • The 'presence' push subscription maintains the live-peer mirror (used for
    hide/show + the epic join), self excluded.

Sub-agent rows are DELIBERATELY not surfaced by this bar (the presence handler
ignores frames carrying `peer.agentId`), so the old nested sub-agent assertions
were retired — that contract no longer exists.

Drives the module via its shipped jsdom hooks (`window.CollabBar._setSummary /
._setPeers / ._render`) plus the real 'presence' push handler. Skips cleanly
when node + jsdom aren't installed.
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
  '<div class="presence-strip" id="presenceStrip" hidden></div></div></body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
// Neuter the periodic tick + the debounced refetch — we drive render by hand.
global.setInterval = win.setInterval = () => 0;
global.clearInterval = win.clearInterval = () => {};
global.setTimeout = win.setTimeout = () => 0;
global.clearTimeout = win.clearTimeout = () => {};

win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
// t(): the module's internal _t calls t(key, params) WITHOUT the fallback, so
// this stub must own the templates (with {n} interpolation) itself — mirroring
// the real i18n keys the bar renders.
const _I18N = {
  'collab.project': 'Project',
  'collab.conflicts': '{n} conflict',
  'collab.decisionsAwaiting': '{n} decisions awaiting you',
  'collab.epicsInProgress': '{n} in progress',
  'collab.epicsOpen': '{n} open',
  'collab.peersOnline': '{n} online',
  'collab.peerAdvancing': 'advancing',
  'collab.openBrain': 'Open Project Brain',
};
win.t = global.t = (k, p) => {
  let s = (k in _I18N) ? _I18N[k] : k;
  if (p) for (const kk in p) s = s.replace(new RegExp('\\{' + kk + '\\}', 'g'), p[kk]);
  return s;
};

// Capture the presence push handler (the module subscribes to 'presence' + 'project').
let _presenceHandler = null;
win.pushSubscribe = global.pushSubscribe = (channel, taskId, fn) => {
  if (channel === 'presence') _presenceHandler = fn;
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
// Defensive Api stub (the debounced refetch is neutered, but _refetchSummary
// guards on Api.project.brainSummary existing anyway).
win.Api = global.Api = { project: { brainSummary: () => Promise.resolve(null) } };
win.openProjectBrain = global.openProjectBrain = () => {};

eval(fs.readFileSync(process.argv[2], 'utf8'));  // presence.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (!win.CollabBar || typeof win.CollabBar._render !== 'function') {
  console.log('FAIL hooks_exposed CollabBar test hooks missing');
  console.log(out.join('\n'));
  process.exit(0);
}
check('hooks_exposed', true);
check('handler_registered', typeof _presenceHandler === 'function');

const strip = document.getElementById('presenceStrip');
const setS = win.CollabBar._setSummary;
const render = win.CollabBar._render;

// ── Solo / empty board (no segments) → the bar is hidden ──
setS('/proj/A', { epicsOpen: 0, epicsClaimed: 0, epicsDone: 0,
                  pendingDecisions: 0, activePeers: 0, peerEpics: {}, charterExists: true });
render();
check('empty_board_hidden', strip.hidden === true);

// ── A summary with counts → the bar shows, segments rendered VERBATIM ──
setS('/proj/A', {
  epicsOpen: 3, epicsClaimed: 1, epicsDone: 5,
  pendingDecisions: 2, activePeers: 2,
  peerEpics: { 'conv-b': 'Refactor the parser', 'conv-self': 'MY OWN EPIC' },
  conflicts: 0, conflictMessages: [], charterExists: true,
});
render();
check('bar_visible', strip.hidden === false);
check('bar_testid', !!strip.querySelector('[data-testid="collab-bar"]'));
check('lead_project_label', !!strip.querySelector('.collab-label'));
// Counts (backend numbers rendered verbatim inside their action-ordered segs).
const segDec = strip.querySelector('.collab-seg-decisions');
check('seg_decisions', !!segDec && segDec.textContent.indexOf('2') !== -1);
const segProg = strip.querySelector('.collab-seg-progress');
check('seg_progress', !!segProg && segProg.textContent.indexOf('1') !== -1);
const segOpen = strip.querySelector('.collab-seg-open');
check('seg_open', !!segOpen && segOpen.textContent.indexOf('3') !== -1);
const segPeers = strip.querySelector('.collab-seg-peers');
check('seg_peers', !!segPeers && segPeers.textContent.indexOf('2') !== -1);
// decisions-present emphasis class on the bar inner.
check('has_decisions_class', !!strip.querySelector('.collab-bar-inner.collab-has-decisions'));

// ── The deep-collab join: a peer's live epic title is shown VERBATIM ──
const epicTitle = strip.querySelector('.collab-epic-title');
check('peer_epic_title_verbatim', !!epicTitle && epicTitle.textContent.indexOf('Refactor the parser') !== -1);
// ── Self is excluded from the epic join (never a line for the displayed conv) ──
check('self_epic_excluded', strip.innerHTML.indexOf('MY OWN EPIC') === -1);

// ── Conflicts: highest-urgency segment + the backend-formed message verbatim ──
setS('/proj/A', {
  epicsOpen: 0, epicsClaimed: 0, epicsDone: 0, pendingDecisions: 0, activePeers: 2,
  peerEpics: {}, conflicts: 1,
  conflictMessages: ['Refactor parser and Peer C are concurrently editing lib/llm/stream.py'],
  charterExists: true,
});
render();
check('seg_conflict', !!strip.querySelector('.collab-seg-conflict'));
check('has_conflicts_class', !!strip.querySelector('.collab-bar-inner.collab-has-conflicts'));
const cline = strip.querySelector('.collab-conflict-line');
check('conflict_message_verbatim',
  !!cline && cline.textContent.indexOf('Refactor parser and Peer C are concurrently editing lib/llm/stream.py') !== -1);

// ── HTML-injection safety: a malicious epic title is escaped, not interpreted ──
setS('/proj/A', {
  epicsOpen: 1, epicsClaimed: 0, epicsDone: 0, pendingDecisions: 0, activePeers: 1,
  peerEpics: { 'conv-evil': '<img src=x onerror=alert(1)>' },
  conflicts: 0, conflictMessages: [], charterExists: true,
});
render();
check('title_escaped', strip.innerHTML.indexOf('<img src=x') === -1
                       && strip.innerHTML.indexOf('&lt;img src=x') !== -1);

// ── The 'presence' push subscription maintains the live-peer mirror (self
//    excluded). With NO backend activePeers, the peer count falls back to the
//    local push mirror → a pushed peer surfaces the "N online" segment. ──
_presenceHandler({ channel: 'presence', taskId: '*', type: 'presence', kind: 'update',
  root: '/proj/A', peer: { convId: 'conv-push', title: 'Pushed peer',
                           status: 'active', statusLabel: 'working', lastBeatTs: Date.now() } });
setS('/proj/A', {   // summary with NO activePeers field → mirror is the source
  epicsOpen: 0, epicsClaimed: 0, epicsDone: 0, pendingDecisions: 1,
  peerEpics: {}, conflicts: 0, conflictMessages: [], charterExists: true,
});
render();
const segPeers2 = strip.querySelector('.collab-seg-peers');
check('push_peer_counted', !!segPeers2 && segPeers2.textContent.indexOf('1') !== -1);
// A depart frame removes it from the mirror → the peers segment drops.
_presenceHandler({ channel: 'presence', taskId: '*', type: 'presence', kind: 'depart',
  root: '/proj/A', peer: { convId: 'conv-push' } });
render();
check('push_peer_departed', !strip.querySelector('.collab-seg-peers'));

// ── A sub-agent frame (peer.agentId set) is IGNORED by the mirror (sub-agents
//    are deliberately not surfaced by this bar) ──
_presenceHandler({ channel: 'presence', taskId: '*', type: 'presence', kind: 'update',
  root: '/proj/A', peer: { convId: 'conv-swarm', agentId: 'agent-coder-1', title: 'coder',
                           status: 'active', statusLabel: 'editing lib/a.py', lastBeatTs: Date.now() } });
render();
check('subagent_frame_ignored', !strip.querySelector('.collab-seg-peers'));

// ── Not in project mode (displayed conv has no root) → the bar hides ──
win.conversations = global.conversations = [{ id: 'conv-self' }];  // no projectPath
render();
check('no_root_hidden', strip.hidden === true);

// ── projectState fallback (parity with the panel's _displayedProjectPath):
//    a New Chat with pending input keeps the project ARMED in the global
//    singleton while there is no active conv to read projectPath from — the
//    bar must resolve the same root the panel would, or the two surfaces
//    disagree about whether a project is displayed. ──
win.activeConvId = global.activeConvId = null;   // New Chat: no active conv at all
win.projectState = global.projectState = { active: true, path: '/proj/B', extraRoots: [] };
setS('/proj/B', { epicsOpen: 2, epicsClaimed: 0, epicsDone: 0,
                  pendingDecisions: 0, activePeers: 0, peerEpics: {}, charterExists: true });
render();
check('projectstate_fallback_visible', strip.hidden === false);
check('projectstate_fallback_counts', !!strip.querySelector('.collab-seg-open'));
// Clearing the singleton hides the bar again (the project is truly gone).
win.projectState = global.projectState = { active: false, path: '', extraRoots: [] };
render();
check('projectstate_cleared_hidden', strip.hidden === true);

console.log(out.join('\n'));
"""


def _run(presence_src: str | None = None) -> str:
    harness = os.path.join(HERE, '_presence_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             presence_src or os.path.join(JS_DIR, 'presence.js'),  # argv[2]
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
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_presence_strip_renders_end_to_end():
    output = _run()
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'collab-bar render failures:\n' + output
    assert output.count('PASS') >= 24, f'expected >=24 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_projectstate_fallback_is_load_bearing(tmp_path):
    """NEUTER: strip the projectState fallback from _displayedRoot() → a New
    Chat whose project is still armed in the singleton no longer resolves a
    root → the bar hides while the panel would open with data (the two
    surfaces disagree again) → projectstate_fallback_visible goes red."""
    presence_js = os.path.join(JS_DIR, 'presence.js')
    with open(presence_js, encoding='utf-8') as f:
        src = f.read()
    needle = 'if (!p && typeof projectState !== "undefined" && projectState && projectState.active) {'
    assert src.count(needle) == 1, (
        'projectState fallback drifted — update the neuter target')
    copy = tmp_path / 'presence_neutered_fallback.js'
    copy.write_text(src.replace(needle, 'if (!p && false) {', 1),
                    encoding='utf-8')
    output = _run(str(copy))
    assert 'FAIL projectstate_fallback_visible' in output, (
        'NEUTER did not bite: the bar still resolved the singleton project '
        'without the fallback.\n' + output)
    # Everything keyed on the conv's own projectPath must stay green — the
    # neuter hit ONLY the singleton fallback, not the primary accessor.
    assert 'PASS bar_visible' in output, output
    assert 'PASS projectstate_cleared_hidden' in output, output
    with open(presence_js, encoding='utf-8') as f:
        assert f.read() == src, 'harness mutated the shipped presence.js'
