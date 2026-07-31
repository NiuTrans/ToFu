"""tests/test_paper_qa_push_transport.py — pt_f6aec3ad0efb40de.

THE SAME ASYMMETRY, TWO MORE TIMES
----------------------------------
pt_67ffc2b7 fixed the paper REPORT: its backend already broadcast every event
on the unified /api/push socket (``report_runtime`` sets
``push_channel='paper'``) while the frontend only polled, so a search that
finished at t=0 kept its spinner for up to 1.2s.

The objective says "the paper reading mode report also has this issue" — and
Reading Mode is not one engine. A script census of every
``lib/paper/*runtime*.py`` that declares a ``push_channel``, cross-referenced
against its frontend consumer, finds FIVE runtimes and TWO still-broken
surfaces:

  runtime               channel          per-tool events?   frontend      verdict
  report_runtime        paper            yes                report.js     FIXED (pt_67ffc2b7)
  qa_runtime            paper            yes                qa.js         BROKEN — poll 700ms
  recommend_runtime     paper            yes (via
                                         recommend_task.
                                         _on_tool_event)    arxiv.js      BROKEN — poll 600ms
  podcast_runtime       paper            NO (measured:
                                         tool_start count
                                         is 0 across every
                                         podcast_engine/*)  podcast.js    EXEMPT
  translate_runtime     paper-translate  NO (tool_start=0)  babel.js      EXEMPT

``video`` declares no push_channel at all, so it is out of scope by
construction.

The two EXEMPT verdicts are measured, not assumed: those capabilities emit
stage progress only, never a per-tool lifecycle, so subscribing would add a
socket handler that carries nothing this epic is about.

WHY A SHARED MODULE
-------------------
Fixing consumers two and three by copy-pasting report.js's inline helper would
leave three near-identical implementations of one contract. The contract now
lives once in ``static/js/paper/push_transport.js`` and all consumers ride it.

WHAT THIS SUITE PINS
--------------------
  1. The census itself — a new push_channel runtime whose frontend does not
     subscribe fails this suite, so the next surface cannot be added silently.
  2. qa.js and arxiv.js subscribe to the 'paper' channel.
  3. Every task-attach site subscribes (Q&A mints a NEW task per question, so
     "only the first one" would leave every follow-up question polling).
  4. The subscription is released on terminal.
  5. The poll survives as the floor.
  6. EXACTLY-ONCE: both transports deliver the same events; without a seq gate
     every delta is applied twice and the answer renders doubled.
  7. END-TO-END on the shipped files: a ``tool_done`` frame handed to the
     registered handler settles the round with NO poll round-trip.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest \
        tests/test_paper_qa_push_transport.py -v
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')
PAPER_JS = os.path.join(JS_DIR, 'paper')
TRANSPORT_JS = os.path.join(PAPER_JS, 'push_transport.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


def _src(path: str) -> str:
    """Comment-stripped source (charter #24: prose must not satisfy a guard)."""
    from tests._source_scan import strip_comments
    with open(path, encoding='utf-8') as fh:
        return strip_comments(fh.read(), lang='js')


# ═══════════════════════════════════════════════════════════════════
#  Face 1 — the census: enumerate, don't trust a list
# ═══════════════════════════════════════════════════════════════════

#: Runtimes whose frontend deliberately does NOT subscribe, with the measured
#: reason. A capability may only appear here if it emits no per-tool lifecycle.
_EXEMPT = {
    'podcast_runtime.py': 'emits stage progress only — tool_start count is 0 '
                          'across every podcast_engine/*.py',
    'translate_runtime.py': 'separate channel paper-translate and '
                            'translate_engine.py tool_start=0',
}

#: runtime file → the frontend that consumes it.
_CONSUMER = {
    'report_runtime.py': 'report.js',
    'qa_runtime.py': 'qa.js',
    'recommend_runtime.py': 'arxiv.js',
}


def test_every_push_runtime_is_classified():
    """★ Enumerate the runtimes; every one must be wired or explicitly exempt.

    The originating ticket named the surfaces by hand. This walks
    ``lib/paper/*runtime*.py`` instead, so a capability added later cannot slip
    through by simply not being on anyone's list.
    """
    found = {}
    for path in sorted(glob.glob(os.path.join(ROOT, 'lib', 'paper',
                                              '*runtime*.py'))):
        with open(path, encoding='utf-8') as fh:
            src = fh.read()
        m = re.search(r"push_channel\s*=\s*'([^']+)'", src)
        if m:
            found[os.path.basename(path)] = m.group(1)

    assert found, 'no push_channel runtimes found — the census broke'

    unclassified = [f for f in found
                    if f not in _EXEMPT and f not in _CONSUMER]
    assert not unclassified, (
        'these paper runtimes declare a push_channel but are neither wired to '
        'a subscribing frontend nor listed as exempt: %r. Classify each one: '
        'does its frontend subscribe, or does it emit no per-tool lifecycle?'
        % (unclassified,))

    # The exempt claims must stay TRUE — if such a capability grows per-tool
    # events later, the exemption silently becomes a defect.
    for runtime in _EXEMPT:
        stem = runtime.replace('_runtime.py', '')
        emitters = []
        for path in glob.glob(os.path.join(ROOT, 'lib', 'paper',
                                           '%s*' % stem, '*.py')) + \
                    glob.glob(os.path.join(ROOT, 'lib', 'paper',
                                           '%s*.py' % stem)):
            if '.tofu_trash' in path:
                continue
            with open(path, encoding='utf-8') as fh:
                if "'type': 'tool_start'" in fh.read():
                    emitters.append(os.path.relpath(path, ROOT))
        assert not emitters, (
            '%s is listed EXEMPT because it emits no per-tool lifecycle, but '
            'these files now emit tool_start: %r. Either wire its frontend to '
            'the push channel or update the exemption.' % (runtime, emitters))


def test_wired_consumers_all_subscribe():
    """Each non-exempt runtime's frontend must actually subscribe."""
    missing = []
    for runtime, consumer in sorted(_CONSUMER.items()):
        src = _src(os.path.join(PAPER_JS, consumer))
        if 'pushSubscribe' not in src and 'paperAttachPush' not in src:
            missing.append('%s (consumer of %s)' % (consumer, runtime))
    assert not missing, (
        'these Reading-Mode frontends never subscribe to the push channel '
        'their backend already broadcasts on, so a finished tool keeps '
        'spinning until the next poll tick: %r' % (missing,))


# ═══════════════════════════════════════════════════════════════════
#  Face 2 — the shared transport exists and is used (not copy-pasted)
# ═══════════════════════════════════════════════════════════════════

def test_shared_transport_module_exists():
    """One implementation of the contract, not one per capability."""
    assert os.path.exists(TRANSPORT_JS), (
        'static/js/paper/push_transport.js must hold the shared push/poll '
        'contract — three copy-pasted variants of the same seq gate is the '
        'shape this project keeps paying for')
    src = _src(TRANSPORT_JS)
    for fn in ('paperIngestEvent', 'paperAttachPush', 'paperDetachPush'):
        assert fn in src, '%s missing from the shared transport' % fn


def test_shared_transport_loads_before_its_consumers():
    """Bundle order: the leaf must be concatenated before every consumer.

    These files share window scope with no imports, so a consumer bundled
    first would hit a ReferenceError at call time — invisible to a unit test
    that evals the files in its own order.
    """
    with open(os.path.join(ROOT, 'lib', 'js_bundler.py'), encoding='utf-8') as fh:
        bundler = fh.read()
    pos_t = bundler.find("'paper/push_transport.js'")
    assert pos_t > 0, 'push_transport.js is not registered in _BUNDLE_FILES'
    for consumer in sorted(set(_CONSUMER.values())):
        pos_c = bundler.find("'paper/%s'" % consumer)
        assert pos_c > 0, 'paper/%s missing from _BUNDLE_FILES' % consumer
        assert pos_t < pos_c, (
            'paper/push_transport.js must be bundled BEFORE paper/%s (they '
            'share window scope with no imports)' % consumer)


def test_consumers_use_the_shared_gate_not_a_private_copy():
    """qa.js / arxiv.js must ROUTE through the shared helpers.

    A private re-implementation would pass the subscribe check while drifting
    from the exactly-once contract — which is the half that silently corrupts
    the rendered answer rather than merely delaying it.
    """
    for consumer in ('qa.js', 'arxiv.js'):
        src = _src(os.path.join(PAPER_JS, consumer))
        assert 'paperAttachPush' in src, (
            '%s must use the shared paperAttachPush' % consumer)
        assert 'paperIngestEvent' in src, (
            '%s must route BOTH transports through the shared seq gate '
            '(paperIngestEvent) — otherwise push + poll double-apply every '
            'delta and the answer renders twice' % consumer)
        assert 'paperDetachPush' in src, (
            '%s must release its subscription' % consumer)


def test_poll_is_kept_as_the_floor():
    """Push accelerates; it must not REPLACE the poll.

    A client behind a WS-blocking proxy has no push channel at all, so
    removing the poll would make those surfaces permanently unobservable.
    """
    for consumer, token in (('qa.js', '_pollQATask'),
                            ('arxiv.js', '_pollRecommendTask')):
        src = _src(os.path.join(PAPER_JS, consumer))
        assert token in src, (
            '%s must keep its poll loop (%s) as the floor' % (consumer, token))


# ═══════════════════════════════════════════════════════════════════
#  Face 3 — END-TO-END on the shipped files
# ═══════════════════════════════════════════════════════════════════

_HARNESS = r"""
const fs = require('fs');
const path = require('path');
global.window = global;
const _log = console.log.bind(console);
global.console = { log: _log, warn: () => {}, error: () => {}, debug: () => {} };

const out = [];
function check(name, cond, detail) {
  out.push((cond ? 'PASS ' : 'FAIL ') + name + (cond ? '' : '  :: ' + (detail || '')));
}

const JS_DIR = process.argv[1];

// ── ambient stubs ──
global.escapeHtml = (s) => String(s == null ? '' : s);
global.t = (k) => k;
global.Icon = () => '';
global.renderMarkdown = (s) => s;
global.renderToolRoundsHTML = () => '';
global.debugLog = () => {};
global._saveActivePaperState = () => {};
global._activePaperId = 'paper-1';
global._paperHash = 'hash-1';
global._paperQAHistory = [];
global._paperQAStreaming = false;
global._paperQAAbortRequested = false;
global._paperParsedText = 'TEXT';
global._switchPaperTab = () => {};
global._setPaperMobileView = () => {};
global.document = { getElementById: () => null, querySelectorAll: () => [],
                    querySelector: () => null, createElement: () => ({ style: {},
                      classList: { add(){}, remove(){}, contains(){ return false; } } }),
                    addEventListener: () => {}, removeEventListener: () => {} };
global.addEventListener = () => {};
global.getSelection = () => null;
global.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
global.setTimeout = (fn, ms) => 0;      // never fire a poll tick
global.clearTimeout = () => {};

const subs = [];
global.pushSubscribe = (ch, taskId, handler) => { subs.push({ ch, taskId, handler }); };
global.pushUnsubscribe = (ch, taskId) => {
  for (let i = subs.length - 1; i >= 0; i--) {
    if (subs[i].ch === ch && subs[i].taskId === taskId) subs.splice(i, 1);
  }
};

let pollCalls = 0;
global.Api = { paper: {
  qaPoll: () => { pollCalls++; return Promise.resolve({ ok: true, status: 200,
    json: () => Promise.resolve({ ok: true, events: [], next_cursor: 0,
                                  status: 'running' }) }); },
} };

// Bundle order: the shared leaf first, then the consumer.
(0, eval)(fs.readFileSync(path.join(JS_DIR, 'paper/push_transport.js'), 'utf8'));
(0, eval)(fs.readFileSync(path.join(JS_DIR, 'paper/qa.js'), 'utf8'));

check('shared_helpers_present',
      typeof paperAttachPush === 'function' && typeof paperIngestEvent === 'function'
      && typeof paperDetachPush === 'function',
      'the shared transport did not load');

// ── the exactly-once gate ──
{
  const st = { content: '' };
  const apply = (s, ev) => { s.content += (ev.delta || ''); return true; };
  const ev = { seq: 0, type: 'delta', delta: 'HELLO' };
  paperIngestEvent(st, ev, apply);
  paperIngestEvent(st, ev, apply);       // the OTHER transport replays it
  check('duplicate_seq_applied_once', st.content === 'HELLO',
        'push + poll deliver the same events; without the gate every delta is '
        + 'applied twice and the answer renders doubled. got ' + JSON.stringify(st.content));

  paperIngestEvent(st, { seq: 1, type: 'delta', delta: ' WORLD' }, apply);
  check('new_seq_still_applied', st.content === 'HELLO WORLD',
        'REVERSE: the gate must not swallow a NEW seq. got ' + JSON.stringify(st.content));

  paperIngestEvent(st, { seq: 0, type: 'delta', delta: 'AGAIN' }, apply);
  check('older_seq_dropped', st.content === 'HELLO WORLD',
        'a late/replayed older frame must not re-append');

  paperIngestEvent(st, { type: 'delta', delta: '!' }, apply);
  check('seqless_frame_applied', st.content === 'HELLO WORLD!',
        'a frame with no seq must still apply — dropping it is worse than a '
        + 'rare duplicate');
}

// ── attach / terminal release ──
{
  const st = {};
  let seen = 0;
  paperAttachPush(st, 'task-1', {
    isCurrent: () => true,
    onEvent: () => { seen++; },
  });
  const mine = subs.filter((x) => x.ch === 'paper' && x.taskId === 'task-1');
  check('subscribed_to_paper_channel', mine.length === 1,
        'expected 1 subscription, got ' + JSON.stringify(subs.map((x) => x.ch)));

  // Idempotent: a second attach for the same task must not double-subscribe.
  paperAttachPush(st, 'task-1', { isCurrent: () => true, onEvent: () => { seen++; } });
  check('attach_is_idempotent',
        subs.filter((x) => x.taskId === 'task-1').length === 1,
        'every attach point may call this; a duplicate handler would double-apply');

  if (mine.length === 1) {
    mine[0].handler({ seq: 5, type: 'tool_done', roundNum: 1, toolCallId: 'tc-1' });
    check('frame_reaches_the_handler', seen === 1, 'seen=' + seen);
    mine[0].handler({ seq: 6, type: 'done' });
    check('terminal_frame_unsubscribes',
          subs.filter((x) => x.taskId === 'task-1').length === 0,
          'finished tasks must not leak handlers for the life of the page');
  }

  // A superseded state must ignore frames (paper switch / newer question).
  const st2 = {};
  let seen2 = 0;
  let current = true;
  paperAttachPush(st2, 'task-2', { isCurrent: () => current, onEvent: () => { seen2++; } });
  const m2 = subs.filter((x) => x.taskId === 'task-2');
  current = false;
  if (m2.length === 1) m2[0].handler({ seq: 1, type: 'delta', delta: 'x' });
  check('superseded_state_ignores_frames', seen2 === 0,
        'a stale handler must not repaint into a dead view; seen=' + seen2);
  paperDetachPush(st2);
}

// ── Q&A end-to-end: a tool_done settles the round with NO poll round-trip ──
{
  const asst = { role: 'assistant', content: '', toolRounds: [], status: 'running' };
  const before = pollCalls;
  // The backend emits these the instant the tool returns.
  _applyQAEvent(asst, { seq: 0, type: 'tool_start', roundNum: 1,
                        toolName: 'web_search', query: 'q', toolCallId: 'tc-1' });
  check('qa_tool_start_opens_round',
        asst.toolRounds.length === 1 && asst.toolRounds[0].status === 'searching',
        JSON.stringify(asst.toolRounds));
  _applyQAEvent(asst, { seq: 1, type: 'tool_done', roundNum: 1, elapsed: 2.1,
                        toolContent: 'BODY',
                        results: [{ toolName: 'web_search', title: 'r' }] });
  check('qa_tool_done_settles_the_round',
        asst.toolRounds[0].status === 'done',
        'a tool_done arriving over push must settle the round immediately; '
        + 'status=' + asst.toolRounds[0].status);
  check('qa_no_poll_needed', pollCalls === before,
        'settling must not require a poll round-trip — that IS the 700ms '
        + 'latency being removed; polls=' + (pollCalls - before));
}

console.log(out.join('\n'));
"""


@pytest.mark.skipif(not _node_available(), reason='node not available')
def test_push_transport_end_to_end():
    """★ Drives the SHIPPED push_transport.js + qa.js.

    A static grep for ``pushSubscribe`` goes green on a subscription that
    registers a handler and then ignores every frame — the "present in the
    file, unreachable in production" failure mode. Only driving the real
    functions proves the frame lands, the gate de-duplicates, and the terminal
    frame releases the handler.
    """
    proc = subprocess.run(['node', '-e', _HARNESS, JS_DIR],
                          capture_output=True, text=True, timeout=90)
    assert proc.returncode == 0, (
        'harness crashed (rc=%s)\nstdout:\n%s\nstderr:\n%s'
        % (proc.returncode, proc.stdout, proc.stderr))
    lines = [ln for ln in proc.stdout.strip().splitlines()
             if ln.startswith(('PASS', 'FAIL'))]
    failed = [ln for ln in lines if ln.startswith('FAIL')]
    assert not failed, ('push transport faces failed:\n  '
                        + '\n  '.join(failed))
    assert len(lines) >= 12, (
        'expected the full matrix (12 checks), got %d:\n%s'
        % (len(lines), '\n'.join(lines)))
