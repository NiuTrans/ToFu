#!/usr/bin/env python3
"""search_arxiv transient-failure retry (lib/paper/arxiv.py).

Regression for the integration bug the R4 real-run surfaced: arXiv rate-limits
(HTTP 429) and times out under load, and search_arxiv used to return [] on the
first failure — which silently starved the research chain's harvest seed (0
papers → gate fail → whole chain dead).

★ Re-anchored 2026-07-28: the adapter no longer owns an HTTP client — the
arXiv transport + Atom parsing live in ``tofu_search.search.vertical.arxiv``
(``search_by_query``) and this module contributes retry/backoff + the title
re-rank on top of its outcome envelope. The old revision of THIS suite
patched ``ax.http_get``, a seam the adapter no longer calls, so every test
here went silently red the day the delegation landed (http_get calls made:
0 — proven by A/B against the pre-refactor file). The retry POLICY is
unchanged; the SEAM moved, so the fakes now patch ``search_by_query``:

  1. a transient request_failed on the first attempt is retried and the
     second (hits) attempt's results are returned;
  2. NEUTER — with the retry budget forced to 1, the first failure's error
     surfaces immediately (the retry is what saves it);
  3. all attempts failed → ( [], error ) after the budget, no exception —
     and the wrapper search_arxiv still returns a bare [];
  4. an exception OUT of the shared helper (e.g. the 2026-07-28 incident: a
     stale server process holding a pre-search_by_query tofu_search raised
     AttributeError on every call) PROPAGATES — it must never be swallowed
     into a silent empty result; the route surfaces it.

Note the per-status retry policy (429 vs 400) now lives inside tofu_search's
own retry loop; this layer sees only the outcome envelope, so "permanent 400
is not retried" cannot be expressed here anymore.

Run standalone:  python tests/test_paper_arxiv_retry.py
Under pytest:    pytest tests/test_paper_arxiv_retry.py -m unit
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


try:
    import pytest
    pytestmark = [pytest.mark.unit]
except ImportError:
    pytest = None


def _envelope(outcome, papers=(), error=''):
    return {'ok': outcome in ('hits', 'no_matches'),
            'query': 'all:q', 'mode': 'terms',
            'papers': list(papers), 'outcome': outcome, 'error': error}


_ONE_PAPER = {'arxiv_id': '2301.12345', 'title': 'A Real Paper',
              'authors': ['A'], 'summary': 's', 'published': '2023-01-01',
              'primary_category': 'cs.CL', 'pdf_url': '', 'abs_url': ''}


def _patch_search(sequence):
    """Patch ts_arxiv.search_by_query to yield envelopes from `sequence`.
    Returns (restore, calls_counter). Also neutralizes the backoff sleep."""
    import lib.paper.arxiv as ax
    from tofu_search.search.vertical import arxiv as ts_arxiv
    calls = []
    it = iter(sequence)

    def fake_search(*a, **kw):
        calls.append(a)
        return next(it)
    orig = ts_arxiv.search_by_query
    ts_arxiv.search_by_query = fake_search
    orig_sleep = ax.time.sleep
    ax.time.sleep = lambda s: None

    def restore():
        ts_arxiv.search_by_query = orig
        ax.time.sleep = orig_sleep
    return restore, calls


def test_transient_failure_then_success_is_retried():
    import lib.paper.arxiv as ax
    restore, calls = _patch_search([
        _envelope('request_failed', error='HTTP 429'),
        _envelope('hits', [_ONE_PAPER]),
    ])
    try:
        results, error = ax.search_arxiv_explained('long context kv cache',
                                                   max_results=5)
        assert error == '', f'unexpected error: {error}'
        assert len(results) == 1, f'expected 1 result after retry, got {len(results)}'
        assert results[0]['arxiv_id'].startswith('2301.12345'), results[0]['arxiv_id']
        assert len(calls) == 2, f'should have retried once (2 calls), got {len(calls)}'
    finally:
        restore()
    _ok('transient request_failed then hits → retried, second attempt returned')


def test_no_retry_would_drop_the_result_NEUTER():
    """NEUTER: force the retry budget to 1 → the failure is NOT retried →
    the error surfaces with zero results (the retry is what saves it)."""
    import lib.paper.arxiv as ax
    orig_n = ax._ARXIV_SEARCH_RETRIES
    ax._ARXIV_SEARCH_RETRIES = 1
    restore, calls = _patch_search([
        _envelope('request_failed', error='HTTP 429'),
        _envelope('hits', [_ONE_PAPER]),
    ])
    try:
        results, error = ax.search_arxiv_explained('q', max_results=5)
        assert results == [], 'NEUTER FAILED: with retries=1 a failure should give no results'
        assert 'HTTP 429' in error, 'NEUTER FAILED: the failure reason must surface'
        assert len(calls) == 1, 'with retries=1 there should be exactly 1 call'
    finally:
        restore()
        ax._ARXIV_SEARCH_RETRIES = orig_n
    _ok('NEUTER: with retry budget 1 the failure surfaces (retry is what saves it)')


def test_all_failures_exhaust_to_error_not_exception():
    import lib.paper.arxiv as ax
    restore, calls = _patch_search([
        _envelope('request_failed', error='HTTP 429'),
        _envelope('request_failed', error='HTTP 429'),
        _envelope('request_failed', error='HTTP 429'),
    ])
    try:
        results, error = ax.search_arxiv_explained('q', max_results=5)
        assert results == [], 'all-failed should exhaust to no results (no exception)'
        assert 'HTTP 429' in error, 'the reason must survive exhaustion'
        assert len(calls) == ax._ARXIV_SEARCH_RETRIES
        # …and the back-compat wrapper still returns a bare list, no raise.
        restore()  # re-patch for the wrapper pass
        restore, calls = _patch_search([_envelope('request_failed', error='HTTP 500')] * 3)
        assert ax.search_arxiv('q', max_results=5) == []
    finally:
        restore()
    _ok('all attempts failed → ( [], reason ) after the budget; wrapper returns bare []')


def test_exception_from_shared_helper_propagates():
    """THE 2026-07-28 incident shape: a stale server process raised
    AttributeError out of the tofu_search seam on EVERY call. Swallowing it
    into [] is what turned a loud outage into "no papers found" — the
    exception MUST propagate so the route can surface it."""
    import lib.paper.arxiv as ax
    from tofu_search.search.vertical import arxiv as ts_arxiv
    orig = ts_arxiv.search_by_query

    def _boom(*a, **kw):
        raise AttributeError(
            "module 'tofu_search.search.vertical.arxiv' has no attribute "
            "'search_by_query'")
    ts_arxiv.search_by_query = _boom
    try:
        raised = False
        try:
            ax.search_arxiv_explained('q', max_results=5)
        except AttributeError:
            raised = True
        assert raised, 'an exception out of the shared helper MUST propagate, not become []'
    finally:
        ts_arxiv.search_by_query = orig
    _ok('exception out of the shared helper propagates (never a silent empty)')


def main():
    print()
    print(_color('═══ search_arxiv Retry Tests ═══', '36'))
    print()
    tests = [
        test_transient_failure_then_success_is_retried,
        test_no_retry_would_drop_the_result_NEUTER,
        test_all_failures_exhaust_to_error_not_exception,
        test_exception_from_shared_helper_propagates,
    ]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
    print()
    print(_color(f'═══ ALL {len(tests)} TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
