"""tests/test_batch_tool_per_item_visibility.py — pt_67ffc2b700094ce9 face ②.

THE SCREENSHOT
--------------
The owner's screenshot shows a tool row reading::

    3 searches:
      • 小红书 曼谷 度假 攻略 site:xiaohongshu.com
      • xiaohongshu.com 曼谷五天四夜 行程 攻略 笔记
      • 小红书 曼谷 酒店 推荐 度假 打卡 2025

…with the spinner still turning. That row is ONE tool call
(``web_search(queries=[...])``) handled by ``_handle_web_search_batch``, which:

  1. hands all N queries to ``run_batch_concurrent`` (its own thread pool),
  2. blocks on ``with ThreadPoolExecutor(...)`` until the LAST one returns,
  3. THEN emits a single ``tool_result`` carrying every merged row.

So the round has exactly ONE observable transition for N independent network
operations. When query 1 comes back in 2s and query 3 hangs for 40s, the user
watches an undifferentiated spinner for 40s — and there is no way, even in
principle, to tell whether all three are slow or just one is. Same shape in
``_handle_fetch_url_batch`` (10 URLs, one barrier).

This is NOT the same defect as face ① (the round-level ``pool.shutdown``
barrier). Fixing ① makes the BATCH TOOL settle promptly relative to its
siblings; it does nothing for the N items INSIDE one batch call, because they
share a single ``round_entry``. Both barriers have to go.

WHAT THIS SUITE PINS
--------------------
  1. web_search batch: a ``tool_progress`` naming the FIRST-completed query
     must be emitted before the batch's terminal ``tool_result``.
  2. Each per-item progress event carries enough to be useful: which query, how
     many are done, how many total.
  3. Progress must NOT use ``chunk`` — that field is run_command's live
     terminal buffer (``_handleToolProgress`` appends it to ``_partialOutput``),
     so reusing it would inject search prose into a terminal pane.
  4. fetch_url batch: same contract, keyed by URL.
  5. The terminal ``tool_result`` still carries the complete merged row set, in
     the ORIGINAL query order (progress must not replace or reorder it).
  6. A per-item FAILURE is still reported as progress — otherwise a batch where
     item 1 dies silently looks identical to one where item 1 never ran.
  7. Single (non-batch) calls emit no per-item progress (no new noise on the
     overwhelmingly common path).

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest \
        tests/test_batch_tool_per_item_visibility.py -v
"""

from __future__ import annotations

import threading
import time

import pytest

pytestmark = pytest.mark.unit


def _mk_task(**over):
    t = {
        'id': 'batch-task-1',
        'convId': 'cv-batch-1',
        'status': 'running',
        'aborted': False,
        'model': 'test-model',
        'events': [],
        'events_lock': threading.Lock(),
        'lastUserQuery': 'bangkok holiday',
    }
    t.update(over)
    return t


def _mk_round_entry(rn=1, tool='web_search'):
    return {
        'roundNum': rn,
        'toolCallId': 'tc-batch',
        'toolName': tool,
        'query': tool,
        'status': 'searching',
        'results': None,
    }


class _Recorder:
    def __init__(self):
        self.events: list[dict] = []
        self._lock = threading.Lock()

    def __call__(self, task, event):
        with self._lock:
            self.events.append(dict(event))

    @property
    def types(self) -> list[str]:
        return [e.get('type') for e in self.events]

    def of_type(self, etype: str) -> list[dict]:
        return [e for e in self.events if e.get('type') == etype]

    def first_index(self, etype: str) -> int:
        for i, e in enumerate(self.events):
            if e.get('type') == etype:
                return i
        return -1


@pytest.fixture()
def rec(monkeypatch):
    """Capture events from every emission site the batch handlers reach."""
    r = _Recorder()
    from lib.tasks_pkg.executor import _finalize as exec_finalize
    from lib.tasks_pkg.handlers.search import _handlers as search_handlers

    monkeypatch.setattr(search_handlers, 'append_event', r, raising=False)
    monkeypatch.setattr(exec_finalize, 'append_event', r, raising=False)
    return r


@pytest.fixture()
def fake_search(monkeypatch):
    """Stub ``_web_search_one`` with per-query scripted latency.

    Patched on the PACKAGE FACADE because the orchestrators deliberately
    resolve it through ``lib.tasks_pkg.handlers.search`` at call time
    (monkeypatch-parity contract documented in _handlers.py).
    """
    script: dict[str, float] = {}
    order: list[str] = []
    lock = threading.Lock()

    def _one(query, user_question, freshness, vertical='auto'):
        time.sleep(script.get(query, 0.0))
        with lock:
            order.append(query)
        results = [{'title': 'r for %s' % query, 'url': 'https://x/%s' % query,
                    'snippet': 's', 'source': 'x', 'fetched': True,
                    'fetchedChars': 10}]
        return results, None, None, None

    from lib.tasks_pkg.handlers import search as facade
    monkeypatch.setattr(facade, '_web_search_one', _one, raising=False)
    return script, order


@pytest.fixture()
def fake_fetch(monkeypatch):
    script: dict[str, float] = {}

    def _one(url, user_question, fetch_reason=''):
        time.sleep(script.get(url, 0.0))
        return {'url': url, 'page_content': 'body of %s' % url, 'is_pdf': False,
                'raw_chars': 20, 'filtered_chars': 20, 'error_msg': None}

    from lib.tasks_pkg.handlers import search as facade
    monkeypatch.setattr(facade, '_fetch_url_one', _one, raising=False)
    return script


# ═══════════════════════════════════════════════════════════════════
#  Faces 1-3 — web_search batch
# ═══════════════════════════════════════════════════════════════════

def test_first_finished_query_is_visible_before_the_batch_settles(rec,
                                                                  fake_search):
    """★ THE LOAD-BEARING FACE (the screenshot).

    Query 1 returns immediately, query 3 takes 1.5s. A ``tool_progress`` naming
    query 1 MUST be emitted before the batch's terminal ``tool_result`` — i.e.
    while the slow query is still in flight.

    Today the handler blocks inside ``run_batch_concurrent`` and emits exactly
    one event at the end, so there is no progress event at all: the user stares
    at "3 searches" with a spinner and cannot tell which query is slow.
    """
    script, _order = fake_search
    script['fast-q'] = 0.0
    script['mid-q'] = 0.4
    script['slow-q'] = 1.5

    from lib.tasks_pkg.handlers.search._handlers import _handle_web_search
    task = _mk_task()
    re_ = _mk_round_entry()
    _handle_web_search(
        task, {'id': 'tc-batch'}, 'web_search', 'tc-batch',
        {'queries': ['fast-q', 'mid-q', 'slow-q']}, 1, re_,
        {}, None, False, None)

    progress = rec.of_type('tool_progress')
    assert progress, (
        'a batch of 3 searches emitted NO tool_progress — the round has one '
        'observable transition for three independent network calls, so a '
        'single slow query is indistinguishable from three. Events: %r'
        % rec.types)

    first_progress = rec.first_index('tool_progress')
    result_idx = rec.first_index('tool_result')
    assert result_idx >= 0, 'the batch must still emit a terminal tool_result'
    assert first_progress < result_idx, (
        'per-item progress (idx=%d) must precede the terminal tool_result '
        '(idx=%d); emitting it afterwards tells the user nothing while they '
        'are waiting' % (first_progress, result_idx))

    # The FIRST progress event must name the query that actually finished first.
    named = [p for p in progress
             if 'fast-q' in str(p.get('batchItem') or p.get('detail') or '')]
    assert named, (
        'the first progress event must identify WHICH query came back — '
        'otherwise "1/3 done" cannot be matched to a row. Progress payloads: %r'
        % progress)


def test_progress_carries_done_and_total(rec, fake_search):
    """Face 2 — the payload must be actionable, not a bare ping.

    "something happened" does not let the UI draw ``2/3``; the counters have to
    be on the wire. Both are read straight off the event by the renderer.
    """
    script, _order = fake_search
    script['q1'] = 0.0
    script['q2'] = 0.3
    script['q3'] = 0.9

    from lib.tasks_pkg.handlers.search._handlers import _handle_web_search
    task = _mk_task()
    _handle_web_search(
        task, {'id': 'tc-batch'}, 'web_search', 'tc-batch',
        {'queries': ['q1', 'q2', 'q3']}, 1, _mk_round_entry(),
        {}, None, False, None)

    progress = rec.of_type('tool_progress')
    assert len(progress) >= 3, (
        'each of the 3 queries must produce its own progress event; got %d: %r'
        % (len(progress), progress))

    for p in progress:
        assert p.get('batchTotal') == 3, (
            'every progress event must carry batchTotal=3; got %r' % (p,))
        assert isinstance(p.get('batchDone'), int) and p['batchDone'] >= 1, (
            'batchDone must be a positive running count; got %r' % (p,))
        assert p.get('toolCallId') == 'tc-batch', (
            'progress must be addressed to the batch round, else the reducer '
            'cannot locate the row to update; got %r' % (p,))

    dones = sorted(p['batchDone'] for p in progress)
    assert dones == [1, 2, 3], (
        'batchDone must advance monotonically 1..N so the UI never goes '
        'backwards; got %r' % (dones,))


def test_progress_does_not_hijack_the_terminal_output_buffer(rec, fake_search):
    """Face 3 — must NOT reuse ``chunk``.

    ``_handleToolProgress`` (static/js/ui/sse_handlers_io.js) appends
    ``ev.chunk`` to ``round._partialOutput``, the LIVE TERMINAL PANE for
    run_command / code_exec. Shipping search progress through that field would
    render query text inside a terminal box and, worse, that buffer is replaced
    wholesale by ``meta.output`` on tool_result — so the data would silently
    vanish. The batch progress needs its own field.
    """
    script, _order = fake_search
    script['q1'] = 0.0
    script['q2'] = 0.5

    from lib.tasks_pkg.handlers.search._handlers import _handle_web_search
    task = _mk_task()
    _handle_web_search(
        task, {'id': 'tc-batch'}, 'web_search', 'tc-batch',
        {'queries': ['q1', 'q2']}, 1, _mk_round_entry(),
        {}, None, False, None)

    for p in rec.of_type('tool_progress'):
        assert 'chunk' not in p, (
            'batch progress must not travel in `chunk` — that field is '
            "run_command's live terminal buffer and is wiped by tool_result. "
            'Offending event: %r' % (p,))


# ═══════════════════════════════════════════════════════════════════
#  Face 4 — fetch_url batch
# ═══════════════════════════════════════════════════════════════════

def test_fetch_batch_reports_each_url(rec, fake_fetch):
    """Face 4 — the same contract for ``fetch_url(urls=[...])``.

    A 10-URL batch is the worst case of this defect: one dead host holds the
    whole row's spinner while nine pages are already read.
    """
    fake_fetch['https://a'] = 0.0
    fake_fetch['https://b'] = 0.3
    fake_fetch['https://c'] = 1.0

    from lib.tasks_pkg.handlers.search._handlers import _handle_fetch_url
    task = _mk_task()
    _handle_fetch_url(
        task, {'id': 'tc-batch'}, 'fetch_url', 'tc-batch',
        {'urls': ['https://a', 'https://b', 'https://c']}, 1,
        _mk_round_entry(tool='fetch_url'), {}, None, False, None)

    progress = rec.of_type('tool_progress')
    assert len(progress) >= 3, (
        'each URL must produce its own progress event; got %d: %r'
        % (len(progress), progress))
    first_progress = rec.first_index('tool_progress')
    result_idx = rec.first_index('tool_result')
    assert first_progress < result_idx, (
        'the first fetched URL must be visible before the slowest one returns '
        '(progress idx=%d, result idx=%d)' % (first_progress, result_idx))
    items = {str(p.get('batchItem')) for p in progress}
    assert any('https://a' in i for i in items), (
        'progress must name the URL that completed; got %r' % (items,))


# ═══════════════════════════════════════════════════════════════════
#  Face 5 — the terminal result is unchanged
# ═══════════════════════════════════════════════════════════════════

def test_terminal_result_still_complete_and_ordered(rec, fake_search):
    """REGRESSION GUARD: progress must be ADDITIVE.

    The terminal ``tool_result`` still has to carry every merged row tagged
    with its source query, in the ORIGINAL declaration order — that ordering is
    what lets the frontend group rows under per-query subheaders. Completion
    order here is deliberately the reverse of declaration order.
    """
    script, _order = fake_search
    script['first'] = 1.0    # declared first, finishes LAST
    script['second'] = 0.5
    script['third'] = 0.0    # declared last, finishes FIRST

    from lib.tasks_pkg.handlers.search._handlers import _handle_web_search
    task = _mk_task()
    re_ = _mk_round_entry()
    _handle_web_search(
        task, {'id': 'tc-batch'}, 'web_search', 'tc-batch',
        {'queries': ['first', 'second', 'third']}, 1, re_,
        {}, None, False, None)

    results = rec.of_type('tool_result')
    assert len(results) == 1, (
        'a batch must still settle with exactly ONE tool_result; got %d'
        % len(results))
    rows = results[0].get('results') or []
    assert len(rows) == 3, 'all 3 queries must contribute rows; got %d' % len(rows)
    assert [r.get('_q') for r in rows] == ['first', 'second', 'third'], (
        'merged rows must stay in DECLARATION order even though completion '
        'order was inverted; got %r' % ([r.get('_q') for r in rows],))
    assert re_['status'] == 'done', 'the round must end settled'
    assert results[0].get('_batchQueries') == ['first', 'second', 'third']


def test_failed_item_is_still_reported(rec, monkeypatch):
    """Face 6 — a per-item failure must be visible too.

    If only successes emitted progress, a batch whose first query throws would
    show ``1/3`` when two finished — the counter would silently under-report and
    the user would conclude the batch was still working on a query that is
    already dead.
    """
    def _one(query, user_question, freshness, vertical='auto'):
        if query == 'boom':
            raise RuntimeError('engine exploded')
        return ([{'title': 't', 'url': 'u', 'snippet': 's', 'source': 'x',
                  'fetched': True, 'fetchedChars': 1}], None, None, None)

    from lib.tasks_pkg.handlers import search as facade
    monkeypatch.setattr(facade, '_web_search_one', _one, raising=False)

    from lib.tasks_pkg.handlers.search._handlers import _handle_web_search
    task = _mk_task()
    _handle_web_search(
        task, {'id': 'tc-batch'}, 'web_search', 'tc-batch',
        {'queries': ['ok-q', 'boom']}, 1, _mk_round_entry(),
        {}, None, False, None)

    progress = rec.of_type('tool_progress')
    assert len(progress) >= 2, (
        'a failed item must still emit progress — otherwise the done-counter '
        'stalls at 1/2 forever and the row looks stuck on a query that has '
        'already failed; got %r' % (progress,))
    failed = [p for p in progress if p.get('batchOk') is False]
    assert failed, (
        'the failing item must be marked (batchOk=False) so the UI can show it '
        'as failed rather than pending; got %r' % (progress,))


def test_single_query_emits_no_batch_progress(rec, fake_search):
    """Face 7 — no new noise on the common path.

    The single-query form has exactly one network call, so its existing
    ``tool_result`` already IS the per-item signal. Emitting a redundant
    progress frame would double the event volume of the most frequent tool in
    the product for zero information.
    """
    script, _order = fake_search
    script['solo'] = 0.0

    from lib.tasks_pkg.handlers.search._handlers import _handle_web_search
    task = _mk_task()
    _handle_web_search(
        task, {'id': 'tc-solo'}, 'web_search', 'tc-solo',
        {'query': 'solo'}, 1, _mk_round_entry(), {}, None, False, None)

    batch_progress = [p for p in rec.of_type('tool_progress')
                      if p.get('batchTotal') is not None]
    assert not batch_progress, (
        'a single-query web_search must not emit batch progress; got %r'
        % (batch_progress,))


# ═══════════════════════════════════════════════════════════════════
#  Face 8 — the progress actually RENDERS (backend → pixels)
# ═══════════════════════════════════════════════════════════════════

import os
import shutil
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


def test_batch_progress_has_a_css_rule():
    """★ A class that reaches the DOM with NO rule is invisible.

    Learned the hard way in this very batch: a concurrent sibling rewrite of
    styles.css silently dropped these rules, and NOTHING went red — the backend
    emitted progress, the handler stored it, the renderer emitted the span, and
    the user would have seen unstyled inline text (or, with no distinguishing
    style at all, effectively nothing). Every Python guard stayed green because
    none of them looked at the stylesheet.

    This is the same shape as the project's earlier `.conv-state-unconfirmed`
    lesson: semantic presence in the DOM is not visibility.
    """
    css_path = os.path.join(ROOT, 'static', 'styles.css')
    with open(css_path, encoding='utf-8') as fh:
        css = fh.read()
    assert '.ptool-batch-progress' in css, (
        'styles.css must style .ptool-batch-progress — the renderer emits that '
        'class, so without a rule the batch counter reaches the DOM and changes '
        'nothing the user can see')
    assert '.ptool-batch-failed' in css, (
        'the per-item FAILURE marker must be styled too, else a failed query is '
        'indistinguishable from a pending one — the exact ambiguity this epic '
        'removes')


@pytest.mark.skipif(not _node_available(), reason='node not available')
def test_renderer_emits_the_progress_only_for_a_real_batch():
    """The renderer must show the counter for a batch and NOTHING for a single
    call — driving the SHIPPED _renderBatchProgress.

    A guard on the backend emit alone would stay green if the renderer ignored
    the fields (the 'A-sends ≠ B-reads' gap). And the negative half matters just
    as much: emitting a "1/1" pill on every ordinary single-query search would
    add permanent noise to the most frequent tool in the product.
    """
    harness = r"""
    const fs = require('fs');
    const path = require('path');
    global.window = global;
    const _log = console.log.bind(console);
    global.console = { log: _log, warn: () => {}, error: () => {}, debug: () => {} };
    global.escapeHtml = (s) => String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    global.t = (k) => k;
    global.document = { getElementById: () => null, querySelectorAll: () => [],
                        addEventListener: () => {}, removeEventListener: () => {} };

    const src = fs.readFileSync(path.join(process.argv[1], 'ui/tool_rounds.js'), 'utf8');
    // Extract just the pure helper — loading the whole module would drag in the
    // full render stack this check does not need.
    const m = src.match(/function _renderBatchProgress\([\s\S]*?\n\}/);
    if (!m) { console.log('FAIL helper_missing :: _renderBatchProgress not found'); process.exit(0); }
    (0, eval)(m[0]);

    const out = [];
    function check(n, c, d) { out.push((c ? 'PASS ' : 'FAIL ') + n + (c ? '' : '  :: ' + (d || ''))); }

    const batch = _renderBatchProgress({ _batchTotal: 3, _batchDone: 2 });
    check('batch_renders_counter', batch.indexOf('2/3') >= 0
          && batch.indexOf('ptool-batch-progress') >= 0,
          'a 3-item batch at 2 done must render "2/3"; got ' + JSON.stringify(batch));

    const failed = _renderBatchProgress({ _batchTotal: 3, _batchDone: 3, _batchFailed: 1 });
    check('failure_is_marked', failed.indexOf('ptool-batch-failed') >= 0,
          'a failed item must be visibly marked; got ' + JSON.stringify(failed));

    check('single_call_renders_nothing', _renderBatchProgress({}) === ''
          && _renderBatchProgress({ _batchTotal: 1, _batchDone: 1 }) === '',
          'a non-batch (or 1-item) call must render NOTHING — a "1/1" pill on '
          + 'every ordinary search would be permanent noise');

    check('null_safe', _renderBatchProgress(null) === '',
          'must not throw on a null round');

    console.log(out.join('\n'));
    """
    proc = subprocess.run(['node', '-e', harness, JS_DIR],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, (
        'harness crashed (rc=%s)\nstdout:\n%s\nstderr:\n%s'
        % (proc.returncode, proc.stdout, proc.stderr))
    lines = [ln for ln in proc.stdout.strip().splitlines()
             if ln.startswith(('PASS', 'FAIL'))]
    failed = [ln for ln in lines if ln.startswith('FAIL')]
    assert not failed, ('batch render faces failed:\n  ' + '\n  '.join(failed))
    assert len(lines) >= 4, 'expected 4 checks, got %d:\n%s' % (len(lines), '\n'.join(lines))
