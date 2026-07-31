"""tests/test_paper_report_push_transport.py — pt_67ffc2b700094ce9 face ③.

THE ASYMMETRY
-------------
For the paper Reading-Mode report the owner's instruction ("solve it from the
backend root cause, then sync to the frontend") is inverted: the BACKEND IS
ALREADY DONE. Measured:

  * ``lib/paper/report_runtime.py`` builds its TaskRuntime with
    ``push_channel='paper'``, so every ``_append_report_event`` is broadcast on
    the unified ``/api/push`` WebSocket the instant it is appended;
  * ``report_engine._execute_tool`` appends ``tool_done`` immediately after the
    tool call returns — no barrier, no batching.

The backend therefore already announces "this search is finished" in real time.
``static/js/paper/report.js`` never listens: ``pushSubscribe`` appears ZERO
times in it. Its only transport is ``setTimeout(_pollReportTask, 1200)`` (3000ms
after a transient error). So a search that finished at t=0 has its spinner
stopped somewhere in t+1.2s … t+3s, for no reason other than that nobody
subscribed to the channel that already carries the news.

``static/js/paper/research.js:167`` does exactly the right thing on the very
same mechanism:

    try { pushSubscribe('research', s.taskId, function (ev) { … }); }
    catch (e) { console.debug(…); }

…with the poll kept as the reconnect/catch-up net. That is the precedent this
face has to reach.

WHAT THIS SUITE PINS
--------------------
  1. The backend runtime really is on the push channel (so the frontend half is
     the ONLY missing leg — this is the premise, asserted rather than assumed).
  2. ``report.js`` subscribes to the ``'paper'`` channel for its task id.
  3. It subscribes at EVERY site where a task id becomes known — fresh start,
     local resume, and lookup-attach — not just the happy path. A reconnect
     after a refresh is precisely when a long report is mid-flight.
  4. It UNSUBSCRIBES on terminal/abort, so a long session does not accumulate
     dead handlers for finished tasks.
  5. The poll survives as the catch-up net (do NOT delete it — a WS-blocked
     client must still converge), but it must no longer be the only transport:
     a push frame has to be able to advance the view without waiting for the
     next timer tick.
  6. END-TO-END, driving the REAL shipped function: a ``tool_done`` frame
     arriving over push must settle the round (status 'done') WITHOUT any poll
     round-trip. This is the guard that cannot be satisfied by a subscription
     that receives frames and drops them.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest \
        tests/test_paper_report_push_transport.py -v
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
JS_DIR = os.path.join(ROOT, 'static', 'js')
REPORT_JS = os.path.join(JS_DIR, 'paper', 'report.js')
RESEARCH_JS = os.path.join(JS_DIR, 'paper', 'research.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


def _report_src() -> str:
    from tests._source_scan import strip_comments
    with open(REPORT_JS, encoding='utf-8') as fh:
        return strip_comments(fh.read(), lang='js')


# ═══════════════════════════════════════════════════════════════════
#  Face 1 — the premise: the backend leg already exists
# ═══════════════════════════════════════════════════════════════════

def test_report_runtime_is_already_on_the_push_channel():
    """The premise, asserted.

    If this ever regressed, the frontend fix would be built on sand — a
    subscription to a channel nobody publishes on is indistinguishable from no
    subscription at all, and the whole face would silently become a no-op.
    """
    from lib.paper.report_runtime import _report_runtime

    ch = getattr(_report_runtime, 'push_channel', None)
    assert ch == 'paper', (
        "report_runtime must publish on the 'paper' push channel — the "
        'frontend half of this fix depends on it; got %r' % (ch,))


def test_report_engine_appends_tool_done_without_a_barrier():
    """The backend announces per-tool completion promptly.

    ``_execute_tool`` must append its ``tool_done`` inside the per-tool body,
    not after some batch/join step — otherwise face ①'s barrier defect exists
    here too and the frontend fix alone would not help.
    """
    import ast
    import inspect
    import textwrap

    from lib.paper import report_engine
    from tests._source_scan import strip_comments

    src = strip_comments(inspect.getsource(report_engine._run_report_task),
                         lang='python')
    tree = ast.parse(textwrap.dedent(src))

    exec_fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == '_execute_tool':
            exec_fn = node
            break
    assert exec_fn is not None, 'could not locate _execute_tool'

    body_src = '\n'.join(ast.unparse(s) for s in exec_fn.body)
    assert 'tool_done' in body_src, (
        'the per-tool completion event must be appended inside _execute_tool '
        '(the per-tool body), so it is emitted the moment that tool returns')
    assert '_append_report_event' in body_src, (
        'the event must go through the runtime append seam so it is pushed')


# ═══════════════════════════════════════════════════════════════════
#  Faces 2-4 — the missing frontend leg
# ═══════════════════════════════════════════════════════════════════

def test_report_js_subscribes_to_the_paper_push_channel():
    """★ THE LOAD-BEARING FACE.

    Comments are stripped first (charter #24): prose describing a subscription
    must not be able to satisfy this. The backend has been broadcasting the
    whole time; the report view has to actually listen.
    """
    src = _report_src()
    assert 'pushSubscribe' in src, (
        'report.js must subscribe to the push channel. The backend already '
        "publishes every report event on 'paper' (report_runtime sets "
        "push_channel='paper'), so the ONLY reason a finished search keeps "
        'spinning for up to 1.2s is that the frontend polls instead of '
        'listening. See static/js/paper/research.js:167 for the precedent.')
    assert re.search(r"pushSubscribe\(\s*'paper'", src), (
        "the subscription must name the 'paper' channel (the one "
        'report_runtime publishes on); found pushSubscribe but not for '
        "'paper'")


def test_subscription_covers_every_task_attach_site():
    """Face 3 — a reconnect is exactly when this matters most.

    ``report.js`` learns a task id at THREE places:
      * fresh ``/start`` returned a task_id,
      * local stream resume on tab re-entry,
      * ``reportLookup`` re-attach after a refresh.

    A report can run for minutes, so the refresh/re-attach path is the common
    one for a long generation. Subscribing on only the fresh-start path would
    leave the reconnected view back on 1.2s polling — the defect surviving in
    the case the user hits most.

    Structural: every call site that starts the poll chain must be adjacent to a
    subscribe (directly, or via a shared attach helper that subscribes).
    """
    src = _report_src()

    # Locate the shared helper, if the implementation chose that route.
    helper = None
    m = re.search(r'function\s+(_\w*[Rr]eportPush\w*|_attachReport\w*)\s*\(', src)
    if m:
        helper = m.group(1)

    poll_starts = [mm.start() for mm in
                   re.finditer(r'_pollReportTask\(view\)', src)]
    # Drop the definition site itself.
    poll_starts = [p for p in poll_starts
                   if 'function' not in src[max(0, p - 40):p]]
    assert len(poll_starts) >= 3, (
        'expected at least 3 poll-start sites (fresh start / resume / lookup '
        'attach); found %d — the file shape changed, re-check this guard'
        % len(poll_starts))

    for pos in poll_starts:
        window = src[max(0, pos - 900):pos + 900]
        subscribed = ('pushSubscribe' in window
                      or (helper is not None and helper in window))
        assert subscribed, (
            'a poll-start site at offset %d has no adjacent push subscription '
            '(neither pushSubscribe nor a shared attach helper). A report that '
            'is re-attached after a refresh would fall back to 1.2s polling — '
            'the exact latency this epic removes, surviving in the most common '
            'case. Context:\n%s' % (pos, window[600:1200]))


def test_subscription_is_released_on_terminal():
    """Face 4 — no handler leak.

    Every finished report would otherwise leave a live handler bound to a dead
    task id for the lifetime of the page; over a session of a dozen reports the
    push frame fan-out grows without bound. ``research.js`` unsubscribes on
    abort for the same reason.
    """
    src = _report_src()
    assert 'pushUnsubscribe' in src, (
        'report.js must release its subscription when the task reaches a '
        'terminal state (done/aborted/error), else handlers for finished tasks '
        'accumulate for the life of the page')


def test_poll_is_kept_as_the_catch_up_net():
    """Face 5 — the poll must NOT be deleted.

    A client whose WebSocket is blocked by a corporate proxy has no push
    channel at all; removing the poll would make the report permanently
    unobservable for that population. Push is the fast path, poll is the floor —
    the same layering research.js uses.

    (This face is expected to be GREEN before the fix; it exists so the fix
    cannot pass by swapping one single transport for another.)
    """
    src = _report_src()
    assert '_pollReportTask' in src, 'the poll loop must survive as the floor'
    assert re.search(r'setTimeout\(\s*function\s*\(\)\s*\{\s*_pollReportTask',
                     src), (
        'the poll timer must remain — push is an accelerator, not a '
        'replacement, or a WS-blocked client can never converge')


# ═══════════════════════════════════════════════════════════════════
#  Face 6 — END-TO-END on the real shipped code
# ═══════════════════════════════════════════════════════════════════

_HARNESS = r"""
const fs = require('fs');
const path = require('path');
global.window = global;

// Keep console.log — it is this harness's only channel back to the test.
const _log = console.log.bind(console);
global.console = { log: _log, warn: () => {}, error: () => {}, debug: () => {} };

const out = [];
function check(name, cond, detail) {
  out.push((cond ? 'PASS ' : 'FAIL ') + name + (cond ? '' : '  :: ' + (detail || '')));
}

const JS_DIR = process.argv[1];

// ── Minimal ambient stubs the module bodies touch at load time ──
global.escapeHtml = (s) => String(s == null ? '' : s);
global.t = (k) => k;
global.debugLog = () => {};
global.showToast = () => {};
global.renderMarkdown = (s) => s;
global.document = {
  getElementById: () => null,
  querySelectorAll: () => [],
  createElement: () => ({ style: {}, classList: { add(){}, remove(){}, toggle(){} },
                          appendChild(){}, click(){} }),
  addEventListener: () => {},
  removeEventListener: () => {},
  body: { appendChild(){} },
};
global.addEventListener = () => {};       // paper-reader.js wires window listeners
global.removeEventListener = () => {};
global.navigator = {};
global.location = { href: '' };
global.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
global.requestAnimationFrame = (fn) => fn();
global.setTimeout = (fn, ms) => 0;      // never actually fire a poll
global.clearTimeout = () => {};
global._activePaperId = 'paper-1';
global._paperHash = 'hash-1';

// Record push subscriptions instead of opening a socket.
const subs = [];
global.pushSubscribe = (channel, taskId, handler) => {
  subs.push({ channel, taskId, handler });
};
global.pushUnsubscribe = (channel, taskId) => {
  for (let i = subs.length - 1; i >= 0; i--) {
    if (subs[i].channel === channel && subs[i].taskId === taskId) subs.splice(i, 1);
  }
};

// Any poll round-trip is a FAILURE for this face: the push frame alone must
// advance the view.
let pollCalls = 0;
global.Api = {
  paper: {
    reportPoll: (...a) => { pollCalls++;
      return Promise.resolve({ ok: true, status: 200,
        json: () => Promise.resolve({ ok: true, events: [], next_cursor: 0,
                                      status: 'running' }) }); },
    reportLookup: () => Promise.resolve({ ok: false }),
    reportStart: () => Promise.resolve({ ok: false }),
    reportAbort: () => Promise.resolve({}),
  },
};

// ``_reportView`` lives in paper-reader.js while the stream/poll cluster lives
// in paper/report.js — the push transport spans both, so BOTH are loaded (in
// bundle order) or the harness would report a missing symbol as if it were a
// product defect.
(0, eval)(fs.readFileSync(path.join(JS_DIR, 'paper/report.js'), 'utf8'));
(0, eval)(fs.readFileSync(path.join(JS_DIR, 'paper-reader.js'), 'utf8'));

// ── Build a live stream state the way the module does ──
if (typeof _makeReportStreamState !== 'function') {
  console.log('FAIL harness_no_stream_factory :: _makeReportStreamState missing');
  process.exit(0);
}
const view = (typeof _reportView === 'function') ? _reportView('report') : null;
if (!view) {
  console.log('FAIL harness_no_view :: _reportView missing');
  process.exit(0);
}

const s = _makeReportStreamState('paper-1', 'zh', 'task-abc', 'report');
view.stream = s;
s.status = 'running';

// A tool round is open and spinning (what the screenshot shows).
if (!Array.isArray(s.toolRounds)) s.toolRounds = [];
s.toolRounds.push({ roundNum: 1, toolName: 'web_search', query: 'q',
                    toolCallId: 'tc-1', status: 'searching', results: null });

// ── Attach the push transport the way the production code must ──
let attached = false;
if (typeof _attachReportPush === 'function') {
  _attachReportPush(view, s);
  attached = true;
} else if (typeof _subscribeReportPush === 'function') {
  _subscribeReportPush(view, s);
  attached = true;
}
check('push_attach_helper_exists', attached,
      'report.js must expose a helper that binds the push subscription for a '
    + 'stream (so all three attach sites share one implementation)');

if (attached) {
  const paperSubs = subs.filter((x) => x.channel === 'paper');
  check('subscribed_to_paper_channel', paperSubs.length === 1,
        'expected exactly 1 subscription on the paper channel, got '
      + JSON.stringify(subs.map((x) => x.channel)));
  check('subscribed_with_task_id',
        paperSubs.length === 1 && paperSubs[0].taskId === 'task-abc',
        'subscription must be keyed by the task id; got '
      + (paperSubs[0] && paperSubs[0].taskId));

  if (paperSubs.length === 1) {
    const handler = paperSubs[0].handler;
    const before = pollCalls;

    // The backend already emitted this the moment the search returned.
    handler({ type: 'tool_done', roundNum: 1, toolCallId: 'tc-1',
              toolName: 'web_search', elapsed: 2.1,
              toolContent: 'RESULT BODY',
              results: [{ toolName: 'web_search', title: 'r', snippet: 's',
                          source: 'x', fetched: true, fetchedChars: 5 }] });

    const r = s.toolRounds[0];
    check('push_frame_settles_the_round', r.status === 'done',
          'a tool_done arriving over push MUST settle the round immediately; '
        + 'status=' + r.status + ' — a subscription that receives frames and '
        + 'drops them stops nothing spinning');
    check('push_frame_carries_results',
          Array.isArray(r.results) && r.results.length === 1,
          'the settled round must carry the results from the push frame; got '
        + JSON.stringify(r.results));
    check('no_poll_round_trip_needed', pollCalls === before,
          'settling must not require a poll round-trip (that is the 1.2s '
        + 'latency being removed); poll calls=' + (pollCalls - before));

    // Terminal frame must release the subscription.
    handler({ type: 'done', report: 'FULL REPORT', paperHash: 'hash-1' });
    const stillSubbed = subs.filter((x) => x.channel === 'paper'
                                        && x.taskId === 'task-abc').length;
    check('terminal_frame_unsubscribes', stillSubbed === 0,
          'a terminal push frame must release the handler, else finished '
        + 'tasks leak subscriptions for the life of the page');
  }
}

console.log(out.join('\n'));
"""


@pytest.mark.skipif(not _node_available(), reason='node not available')
def test_push_frame_settles_a_round_without_polling():
    """★ END-TO-END on the SHIPPED file.

    Drives the real ``report.js`` in node with a stubbed ``pushSubscribe`` and
    an ``Api.paper.reportPoll`` that COUNTS calls. A ``tool_done`` frame handed
    to the registered handler must settle the open round with zero poll
    round-trips.

    Why this specific shape: a Python-only guard (or a static grep for
    ``pushSubscribe``) would go green on a subscription that registers a
    handler and then ignores every frame — the "fix present in the file,
    unreachable in production" failure mode this project has hit repeatedly.
    Only driving the real function proves the frame lands.
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
                        + '\n  '.join(failed)
                        + '\n\nfull:\n  ' + '\n  '.join(lines))
    assert len(lines) >= 7, (
        'expected the full push matrix (7 checks), got %d:\n%s'
        % (len(lines), '\n'.join(lines)))


def test_research_js_precedent_is_intact():
    """The precedent this face copies must still exist.

    If ``research.js`` ever lost its subscription, the "one capability already
    does this correctly" argument would be stale and the next reader would have
    no reference implementation to compare against.
    """
    from tests._source_scan import strip_comments
    with open(RESEARCH_JS, encoding='utf-8') as fh:
        src = strip_comments(fh.read(), lang='js')
    assert re.search(r"pushSubscribe\(\s*'research'", src), (
        'research.js is the reference implementation for push-first + '
        'poll-as-floor; it must keep its subscription')


# ═══════════════════════════════════════════════════════════════════
#  Face 8 — EXACTLY-ONCE across the two transports
# ═══════════════════════════════════════════════════════════════════

_DEDUP_HARNESS = r"""
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
global.escapeHtml = (s) => String(s == null ? '' : s);
global.t = (k) => k;
global.debugLog = () => {}; global.showToast = () => {};
global.renderMarkdown = (s) => s;
global.document = { getElementById: () => null, querySelectorAll: () => [],
                    createElement: () => ({ style: {}, classList: { add(){}, remove(){}, toggle(){} },
                                            appendChild(){}, click(){} }),
                    addEventListener: () => {}, removeEventListener: () => {},
                    body: { appendChild(){} } };
global.addEventListener = () => {}; global.removeEventListener = () => {};
global.navigator = {}; global.location = { href: '' };
global.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
global.requestAnimationFrame = (fn) => fn();
global.setTimeout = () => 0; global.clearTimeout = () => {};
global._activePaperId = 'paper-1'; global._paperHash = 'hash-1';
global.pushSubscribe = () => {}; global.pushUnsubscribe = () => {};

(0, eval)(fs.readFileSync(path.join(JS_DIR, 'paper/report.js'), 'utf8'));
(0, eval)(fs.readFileSync(path.join(JS_DIR, 'paper-reader.js'), 'utf8'));

const s = _makeReportStreamState('paper-1', 'zh', 'task-abc', 'report');
s.status = 'running';

// The SAME delta arrives twice: once over push (fast) and again in the poll's
// cursor replay. Applying it twice would duplicate the report body — the exact
// double-render this file's delta_reset logic already exists to prevent.
const ev = { seq: 0, type: 'delta', delta: 'HELLO' };
_applyReportEvent(s, ev);
_applyReportEvent(s, ev);
check('duplicate_seq_applied_once', s.fullText === 'HELLO',
      'a delta delivered by BOTH transports must be applied exactly once; '
    + 'fullText=' + JSON.stringify(s.fullText));

// A genuinely NEW event still lands.
_applyReportEvent(s, { seq: 1, type: 'delta', delta: ' WORLD' });
check('next_seq_applied', s.fullText === 'HELLO WORLD',
      'the gate must not swallow a new event; fullText='
    + JSON.stringify(s.fullText));

// An OUT-OF-ORDER (older) frame is dropped, not re-applied.
_applyReportEvent(s, { seq: 0, type: 'delta', delta: 'AGAIN' });
check('older_seq_dropped', s.fullText === 'HELLO WORLD',
      'a late/replayed older frame must not re-append; fullText='
    + JSON.stringify(s.fullText));

// A seq-less frame (defensive: older server / synthetic) is still applied —
// dropping it would be worse than a rare duplicate.
_applyReportEvent(s, { type: 'delta', delta: '!' });
check('seqless_frame_applied', s.fullText === 'HELLO WORLD!',
      'a frame with no seq must still apply; fullText=' + JSON.stringify(s.fullText));

// Tool rounds must not be double-pushed either.
const s2 = _makeReportStreamState('paper-1', 'zh', 'task-x', 'report');
const ts = { seq: 5, type: 'tool_start', roundNum: 1, toolName: 'web_search',
             query: 'q', toolCallId: 'tc-1' };
_applyReportEvent(s2, ts);
_applyReportEvent(s2, ts);
check('duplicate_tool_start_pushes_one_round', s2.toolRounds.length === 1,
      'a replayed tool_start must not create a twin round; got '
    + s2.toolRounds.length);

console.log(out.join('\n'));
"""


@pytest.mark.skipif(not _node_available(), reason='node not available')
def test_two_transports_apply_each_event_exactly_once():
    """★ Face 8 — the correctness price of adding a second transport.

    Push and poll both deliver the SAME events. Without de-duplication the
    report body would be appended twice for every frame that arrives on both —
    a regression strictly worse than the 1.2s latency being removed, and one
    this file already carries scar tissue about (see the ``delta_reset``
    double-render comments).

    The gate keys on the monotonic ``seq`` every event carries (assigned in
    ``TaskRuntime.append_event``), so de-duplication is exact rather than
    heuristic, and both transports stay ordered with respect to each other.
    """
    proc = subprocess.run(['node', '-e', _DEDUP_HARNESS, JS_DIR],
                          capture_output=True, text=True, timeout=90)
    assert proc.returncode == 0, (
        'harness crashed (rc=%s)\nstdout:\n%s\nstderr:\n%s'
        % (proc.returncode, proc.stdout, proc.stderr))
    lines = [ln for ln in proc.stdout.strip().splitlines()
             if ln.startswith(('PASS', 'FAIL'))]
    failed = [ln for ln in lines if ln.startswith('FAIL')]
    assert not failed, ('exactly-once faces failed:\n  ' + '\n  '.join(failed))
    assert len(lines) >= 5, (
        'expected 5 checks, got %d:\n%s' % (len(lines), '\n'.join(lines)))
