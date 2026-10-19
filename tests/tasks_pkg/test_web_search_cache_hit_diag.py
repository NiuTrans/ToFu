"""tests/tasks_pkg/test_web_search_cache_hit_diag.py — 0-result web_search cache-hit honesty.

Regression suite for the 2026-08-20 incident: a streaming-prefetched
``web_search`` returned 0 results (dead proxy → every engine connection
failed), but the prefetch cache-hit path

  1. DROPPED the orchestrator's ``search_diag`` at injection time
     (``inject_into_cache`` had no slot for it), and
  2. FABRICATED a single synthetic meta
     (``title='Search: {query}'``, ``fetched=True``, ``fetchedChars=N``)
     out of the 182-char model-facing "no results" text.

The UI therefore rendered "1 result ✓ 182 chars" for what was in fact a
TOTAL network failure — the descriptive zero-result diagnostic row
(``round.searchDiag``) the frontend already owns could never fire.

The fix threads ``search_diag`` through the dedup-cache tuple (7th slot)
on every writer (streaming prefetch injector ×2, parallel in-flight dedup
store), forwards it (plus a ``cacheSource`` marker) onto the round on a
hit, and removes the fabricating ``_build_cache_hit_meta`` web_search
branch entirely.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest \
        tests/tasks_pkg/test_web_search_cache_hit_diag.py -v
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future

import pytest

pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════════════
#  Harness (same discipline as tests/tools/test_tool_settle_all_lanes.py)
# ═══════════════════════════════════════════════════════════════════

def _mk_task(**over):
    t = {
        'id': 'diag-task-1',
        'convId': 'cv-diag-1',
        '_userId': 1,
        'status': 'running',
        'aborted': False,
        'model': 'test-model',
        'events': [],
        'events_lock': threading.Lock(),
        '_dispatch_heartbeat': 0.0,
        '_t_last_event': 0.0,
        '_attended': False,
    }
    t.update(over)
    return t


def _mk_tc(tc_id: str, fn_name: str, seq: int, *, args=None):
    from lib.tasks_pkg.tool_display import _build_tool_round_entry
    _n, round_entry, _ev = _build_tool_round_entry(
        fn_name, args or {}, tc_id, '{}', seq, False)
    tc = {'id': tc_id, 'type': 'function',
          'function': {'name': fn_name, 'arguments': '{}'}}
    return (tc, fn_name, tc_id, dict(args or {}), round_entry['roundNum'],
            round_entry, None)


class _Recorder:
    def __init__(self):
        self.events: list[dict] = []
        self._lock = threading.Lock()

    def __call__(self, task, event):
        with self._lock:
            self.events.append(dict(event))

    def find(self, tc_id: str, etype: str):
        for e in self.events:
            if e.get('toolCallId') == tc_id and e.get('type') == etype:
                return e
        return None


@pytest.fixture()
def rec(monkeypatch):
    r = _Recorder()
    import lib.tasks_pkg.tool_dispatch._heartbeat as facade
    from lib.tasks_pkg.executor import _finalize as exec_finalize
    import lib.tasks_pkg.tool_dispatch._pipeline as _pipeline
    monkeypatch.setattr(_pipeline, 'append_event', r, raising=False)
    monkeypatch.setattr(facade, 'append_event', r, raising=False)
    monkeypatch.setattr(exec_finalize, 'append_event', r, raising=False)
    return r


def _run(task, tcs, messages=None):
    from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
    execute_tool_pipeline(
        task, tcs, cfg={'autoApply': True}, project_path=None,
        project_enabled=False, tool_list=[], messages=messages or [],
        all_search_results_text=[], round_num=0, model='test-model')


_NET_DIAG = {
    'reason': 'network_error',
    'reason_detail': 'All 7 search engines failed due to network errors.',
    'engine_errors': {'Bing': 'NameResolutionError', 'Brave': 'ProxyError'},
    'engine_empty': [],
}


# ═══════════════════════════════════════════════════════════════════
#  1. Tuple format — legacy 6-slot entries still unpack, 7th defaults None
# ═══════════════════════════════════════════════════════════════════

def test_unpack_cache_entry_pads_legacy_lengths():
    from lib.tasks_pkg.tool_dispatch._flags import _unpack_cache_entry

    legacy6 = ('BODY', True, 'prefetch', ['d'], {'e': 1}, {'v': 2})
    content, is_search, source, disp, bkdn, vert, diag = _unpack_cache_entry(legacy6)
    assert (content, is_search, source, disp, bkdn, vert) == legacy6
    assert diag is None, 'a legacy 6-slot entry must default search_diag to None'

    full7 = ('BODY', True, 'prefetch', ['d'], {'e': 1}, {'v': 2}, _NET_DIAG)
    assert _unpack_cache_entry(full7) == full7

    bare = _unpack_cache_entry('BODY')
    assert bare[0] == 'BODY' and bare[6] is None


# ═══════════════════════════════════════════════════════════════════
#  2. Streaming prefetch injector stores search_diag (the dropped slot)
# ═══════════════════════════════════════════════════════════════════

def test_inject_into_cache_stores_search_diag():
    from lib.tasks_pkg.streaming_tool_executor import (
        StreamingToolAccumulator, _ContentWithDisplayResults,
    )
    from lib.tasks_pkg.tool_dispatch._flags import _make_cache_key

    task = _mk_task()
    acc = StreamingToolAccumulator(task, None)

    # A zero-result web_search: display_results EMPTY, diagnostic attached —
    # exactly what _execute_one returns when every engine is down.
    content = _ContentWithDisplayResults(
        'Search returned 0 results — no matching content was found.', [])
    content.search_diag = _NET_DIAG

    fut = Future()
    fut.set_result(content)
    fn_args = {'query': 'github deepseek harness repository'}
    acc._futures['tc-1'] = (fut, 'web_search', fn_args, time.time())

    assert acc.inject_into_cache(task) == 1
    entry = task['_tool_result_cache'][_make_cache_key('web_search', fn_args)]
    assert len(entry) == 7, f'cache entry must carry search_diag (7 slots); got {len(entry)}'
    assert entry[6] == _NET_DIAG
    assert entry[3] == []          # display_results: genuinely zero
    assert entry[2] == 'prefetch'


# ═══════════════════════════════════════════════════════════════════
#  3. THE LOAD-BEARING FACE — prefetch hit of a failed search renders the
#     honest diagnostic, never a fabricated "1 result"
# ═══════════════════════════════════════════════════════════════════

def test_prefetch_hit_zero_result_search_renders_diag_not_fake_result(rec):
    from lib.tasks_pkg.tool_dispatch._flags import _make_cache_key

    fn_args = {'query': 'github deepseek harness repository'}
    task = _mk_task()
    task['_tool_result_cache'] = {
        _make_cache_key('web_search', fn_args):
            ('Search returned 0 results — no matching content was found '
             'across all engines. Try rephrasing with different keywords, '
             'using fewer/broader terms, or searching in a different '
             'language.', True, 'prefetch', [], None, None, _NET_DIAG),
    }
    tcs = [_mk_tc('tc-hit', 'web_search', 1, args=fn_args)]
    _run(task, tcs)

    round_entry = tcs[0][5]
    assert round_entry['results'] == [], (
        'a zero-result search must finalize with ZERO result rows; got %r'
        % (round_entry['results'],))
    assert round_entry.get('searchDiag') == _NET_DIAG, (
        'the cached diagnostic must reach the round so the frontend renders '
        'the descriptive network-error row')
    assert round_entry.get('cacheSource') == 'prefetch'

    ev = rec.find('tc-hit', 'tool_result')
    assert ev is not None
    assert ev.get('results') == []
    assert ev.get('searchDiag') == _NET_DIAG
    assert ev.get('cacheSource') == 'prefetch'

    # The old defect, pinned shut: no synthetic meta may be materialized —
    # no 'Search: …' title, no fetched:True, no char count masquerading as
    # a fetched page.
    for item in round_entry['results']:
        assert not str(item.get('title', '')).startswith('Search:'), item
        assert item.get('fetched') is not True, item


def test_dedup_hit_zero_result_search_after_real_execution(rec, monkeypatch):
    """The parallel in-flight dedup STORE must carry searchDiag too: a real
    (non-prefetch) zero-result execution followed by an identical call hits
    the dedup lane and renders the same honest diagnostic."""
    diag = {'reason': 'network_error', 'engine_errors': {'Bing': 'x'},
            'engine_empty': []}

    def _fake(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
              cfg, project_path, project_enabled, all_tools=None):
        assert fn_name == 'web_search'
        round_entry['searchDiag'] = diag
        from lib.tasks_pkg.executor._finalize import _finalize_tool_round
        _finalize_tool_round(task, rn, round_entry, [],
                             extra_event_fields={'searchDiag': diag})
        return tc_id, 'Search failed: all search engines encountered network errors.', True

    import lib.tasks_pkg.tool_dispatch._heartbeat as _heartbeat
    import lib.tasks_pkg.tool_dispatch._pipeline as _pipeline
    monkeypatch.setattr(_heartbeat, '_execute_tool_one', _fake, raising=False)
    monkeypatch.setattr(_pipeline, '_execute_tool_one', _fake, raising=False)

    fn_args = {'query': 'github deepseek harness repository'}
    task = _mk_task()
    _run(task, [_mk_tc('tc-real', 'web_search', 1, args=fn_args)])

    # Second round, identical call → dedup HIT against the stored entry.
    tcs2 = [_mk_tc('tc-replay', 'web_search', 2, args=fn_args)]
    _run(task, tcs2)

    round_entry = tcs2[0][5]
    assert round_entry['results'] == []
    assert round_entry.get('searchDiag') == diag, (
        'the dedup-store path dropped searchDiag — a replayed failed search '
        'would again render a fabricated single result')
    assert round_entry.get('cacheSource') == 'cache'

    ev = rec.find('tc-replay', 'tool_result')
    assert ev is not None and ev.get('results') == []
    assert ev.get('searchDiag') == diag


# ═══════════════════════════════════════════════════════════════════
#  4. The fabricating fallback is UNREACHABLE for web_search — the hit
#     branch always finalizes from display/diag data, never from
#     _build_cache_hit_meta's synthetic single meta
# ═══════════════════════════════════════════════════════════════════

def test_cache_hit_never_calls_meta_fallback_for_web_search(rec, monkeypatch):
    import lib.tasks_pkg.tool_dispatch._pipeline as _pipeline
    from lib.tasks_pkg.tool_dispatch._flags import _make_cache_key

    calls = []
    real = _pipeline._build_cache_hit_meta

    def _spy(fn_name, *a, **kw):
        calls.append(fn_name)
        return real(fn_name, *a, **kw)

    monkeypatch.setattr(_pipeline, '_build_cache_hit_meta', _spy)

    fn_args = {'query': 'github deepseek harness repository'}
    task = _mk_task()
    task['_tool_result_cache'] = {
        _make_cache_key('web_search', fn_args):
            ('Search returned 0 results.', True, 'prefetch',
             [], None, None, _NET_DIAG),
    }
    _run(task, [_mk_tc('tc-hit', 'web_search', 1, args=fn_args)])

    assert 'web_search' not in calls, (
        'a web_search cache hit must finalize from stored display/diag data; '
        'routing it through _build_cache_hit_meta is what fabricated the '
        '"Search: … ✓ 182 chars" pseudo-result')

    # The generic fallback also lost its web_search special case: asking for
    # one directly must NOT yield the old 'Search: {query}' fabrication.
    meta = real('web_search', {'query': 'q'}, 'body', True)
    assert not str(meta.get('title', '')).startswith('Search:'), meta


# ═══════════════════════════════════════════════════════════════════
#  5. A zero-result batch surfaces the SINGULAR searchDiag the frontend
#     actually reads — and the failure is NOT cached for replay.
#
#  Regression for 2026-10-17: a 2-query batch failed during a network
#  outage (reason=partial_network_error / deadline), but the handler only
#  stamped the PLURAL ``searchDiags``. The frontend's zero-result renderer
#  reads ``searchDiag`` (singular) only, so both queries rendered the
#  fabricated "All engines responded but found no matching results — try
#  different keywords" row. Worse, the failed result was cached, so a later
#  identical search replayed the outage instead of re-executing.
# ═══════════════════════════════════════════════════════════════════

from types import SimpleNamespace


def _fake_batch_backend(diags):
    """A search backend whose per-query results/diags come from ``diags``."""
    import importlib
    formatter = importlib.import_module(
        'tofu_search.search').format_search_for_tool_response

    def _one(query, user_question, freshness='', vertical='auto'):
        return [], diags[query], None, None

    return SimpleNamespace(_web_search_one=_one), formatter


def _capture_tool_result_events(monkeypatch):
    events = []
    import lib.tasks_pkg.handlers.search._handlers as handlers
    monkeypatch.setattr(handlers, 'append_event',
                        lambda task, ev, *a, **k: events.append(dict(ev)))
    return events


def test_batch_zero_result_emits_singular_search_diag(monkeypatch):
    """A fully-failed batch must stamp the singular ``searchDiag`` (the field
    the frontend's zero-result renderer reads), not only the plural list."""
    from lib.tasks_pkg.handlers.search import _handlers

    diags = {
        'query one': {'reason': 'partial_network_error',
                      'engine_errors': {'Bing': 'down'}, 'engine_empty': []},
        'query two': {'reason': 'deadline', 'deadline_hit': True,
                      'engine_errors': {}, 'engine_empty': []},
    }
    backend, formatter = _fake_batch_backend(diags)
    monkeypatch.setattr(_handlers, '_load_search_backend',
                        lambda: (backend, formatter))
    events = _capture_tool_result_events(monkeypatch)

    task = _mk_task(lastUserQuery='q')
    round_entry = {'query': 'batch'}
    args = {'queries': [{'query': 'query one'}, {'query': 'query two'}]}
    _handlers._handle_web_search(
        task, {}, 'web_search', 'tc-batch', args, 1, round_entry,
        {'autoApply': True}, '/tmp', False)

    # Singular diag surfaces the PRIMARY failing reason (not a bare no_matches).
    primary = round_entry.get('searchDiag')
    assert primary is not None, 'batch must stamp the singular searchDiag'
    assert primary['reason'] in ('partial_network_error', 'deadline')
    # Plural per-query list is preserved for completeness.
    assert len(round_entry.get('searchDiags') or []) == 2
    # The tool_result event carries the same singular diag.
    tool_results = [e for e in events if e.get('type') == 'tool_result']
    assert tool_results and tool_results[-1].get('searchDiag') == primary


def test_batch_zero_result_prefers_failing_diag_over_clean_no_matches(
        monkeypatch):
    """When one query cleanly finds nothing and another fails, the surfaced
    singular diag must be the FAILURE, so the UI never reports a failed
    search as a clean 'no matches'."""
    from lib.tasks_pkg.handlers.search import _handlers

    diags = {
        'clean': {'reason': 'no_matches', 'engine_errors': {},
                  'engine_empty': ['Bing']},
        'failed': {'reason': 'deadline', 'deadline_hit': True,
                   'engine_errors': {}, 'engine_empty': []},
    }
    backend, formatter = _fake_batch_backend(diags)
    monkeypatch.setattr(_handlers, '_load_search_backend',
                        lambda: (backend, formatter))
    _capture_tool_result_events(monkeypatch)

    task = _mk_task(lastUserQuery='q')
    round_entry = {'query': 'batch'}
    # 'clean' is listed FIRST — the picker must not just take [0].
    args = {'queries': [{'query': 'clean'}, {'query': 'failed'}]}
    _handlers._handle_web_search(
        task, {}, 'web_search', 'tc-batch', args, 1, round_entry,
        {'autoApply': True}, '/tmp', False)

    assert round_entry['searchDiag']['reason'] == 'deadline'


def test_zero_result_search_is_not_cached(monkeypatch):
    """A zero-result search (single or batch) must set ``_cacheableResult``
    False so a later identical query re-executes instead of replaying the
    failure from the prefetch/dedup cache."""
    from lib.tasks_pkg.handlers.search import _handlers

    diag = {'reason': 'deadline', 'deadline_hit': True,
            'engine_errors': {}, 'engine_empty': []}
    backend, formatter = _fake_batch_backend({'q': diag, 'q2': diag})
    monkeypatch.setattr(_handlers, '_load_search_backend',
                        lambda: (backend, formatter))
    _capture_tool_result_events(monkeypatch)

    # Single-query path.
    task = _mk_task(lastUserQuery='q')
    single_entry = {'query': 'q'}
    _handlers._handle_web_search(
        task, {}, 'web_search', 'tc-single', {'query': 'q'}, 1, single_entry,
        {'autoApply': True}, '/tmp', False)
    assert single_entry.get('_cacheableResult') is False

    # Batch path.
    batch_entry = {'query': 'batch'}
    _handlers._handle_web_search(
        task, {}, 'web_search', 'tc-batch', {'queries': ['q', 'q2']}, 2,
        batch_entry, {'autoApply': True}, '/tmp', False)
    assert batch_entry.get('_cacheableResult') is False


def test_successful_batch_stays_cacheable_and_diag_free(monkeypatch):
    """A batch with real results must NOT be marked uncacheable and must not
    fabricate a singular searchDiag — the new behavior is failure-only."""
    import importlib
    from lib.tasks_pkg.handlers.search import _handlers

    formatter = importlib.import_module(
        'tofu_search.search').format_search_for_tool_response

    def _one(query, user_question, freshness='', vertical='auto'):
        return ([{'title': 'T', 'url': 'https://x.org', 'source': 'Web',
                  'snippet': 's'}], None, None, None)

    backend = SimpleNamespace(_web_search_one=_one)
    monkeypatch.setattr(_handlers, '_load_search_backend',
                        lambda: (backend, formatter))
    _capture_tool_result_events(monkeypatch)

    task = _mk_task(lastUserQuery='q')
    round_entry = {'query': 'batch'}
    _handlers._handle_web_search(
        task, {}, 'web_search', 'tc-ok', {'queries': ['a', 'b']}, 1,
        round_entry, {'autoApply': True}, '/tmp', False)
    assert '_cacheableResult' not in round_entry
    assert 'searchDiag' not in round_entry
