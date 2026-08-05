"""Regression: streaming pre-exec ``web_search`` delegates to the authoritative
``handlers.search._web_search_one`` — so it is byte-identical to the serial
handler and never drifts into a stale re-implementation.

Background
----------
``StreamingToolAccumulator._execute_one('web_search', …)`` pre-executes the
search while the model is still streaming and injects the result into
``task['_tool_result_cache']`` as AUTHORITATIVE — the serial pipeline then
finds the cache hit and SKIPS re-execution. Historically this branch
RE-IMPLEMENTED the search path inline (its own ``resolve_vertical`` +
``perform_web_search`` + a per-query ``ThreadPoolExecutor(max_workers=1)`` for
the vertical plan). That inline copy diverged from the serial single source of
truth ``_web_search_one`` in three ways:

  1. it LEAKED the vertical thread-pool (never called ``shutdown``);
  2. it lacked ``_web_search_one``'s try/except safety net around
     ``perform_web_search`` (an exception escaped / produced a raw error
     string instead of a graceful ``search_diag`` block);
  3. the vertical-result timeout differed.

The fix routes both single and batch modes through ``_web_search_one`` +
``_format_search_display_for_results`` (mirroring the ``fetch_url`` fix). These
tests pin that delegation and the metadata-carrier parity
(``display_results`` / ``engine_breakdown`` / ``search_diag`` /
``vertical`` / the ``{'batch': [...]}`` carrier); the last test is a
double-neuter proving the assertion is load-bearing.
"""
from __future__ import annotations

import importlib
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _tofu_search_mod():
    """The ``tofu_search.search`` MODULE — never the package-level function.

    ``tofu_search/__init__.py`` defines a public *function* named ``search``
    that shadows the ``search`` subpackage attribute once the package init
    has run. ``patch('tofu_search.search.format_...')`` resolves its target
    by ``getattr`` and, on Python < 3.12, lands on that FUNCTION (mock only
    gained submodule-aware fallback later) → ``AttributeError: <function
    search ...> does not have the attribute ...`` on the 3.10 CI leg.
    ``importlib.import_module`` goes through ``sys.modules`` and is immune
    to attribute shadowing on every version.
    """
    return importlib.import_module('tofu_search.search')


def _make_task(tid='stream-search-test'):
    return {
        'id': tid,
        'aborted': False,
        'lastUserQuery': 'origin of tofu',
        '_tool_result_cache': {},
    }


def _results(*urls):
    """A minimal perform_web_search-style results list (plain list of dicts).

    Includes the keys the real ``format_search_for_tool_response`` reads
    (``source``) so tests can exercise the real formatter, and
    ``full_content`` so display derivation sets fetched/fetchedChars.
    """
    return [{'title': f'T {u}', 'url': u, 'snippet': 's',
             'source': 'Web', 'full_content': 'x' * 50}
            for u in urls]


@pytest.mark.unit
class TestStreamingWebSearchDelegation:

    def test_single_query_routes_through_web_search_one(self):
        from lib.tasks_pkg.streaming_tool_executor import (
            StreamingToolAccumulator, _ContentWithDisplayResults)
        acc = StreamingToolAccumulator(_make_task(), project_path='/tmp')

        breakdown = {'bing': ['https://a.com']}
        fake_ret = (_results('https://a.com'), None, breakdown, None)
        with patch('lib.tasks_pkg.handlers.search._web_search_one',
                   return_value=fake_ret) as m, \
             patch.object(_tofu_search_mod(), 'format_search_for_tool_response',
                          return_value='FORMATTED-LLM-TEXT') as fmt:
            out = acc._execute_one('web_search', {'query': 'tofu', 'vertical': 'auto'})

        # Delegation happened with the serial arg contract:
        #   _web_search_one(query, user_question, freshness, vertical=…)
        assert m.call_count == 1
        args, kwargs = m.call_args
        assert args[0] == 'tofu'
        assert args[1] == 'origin of tofu'       # lastUserQuery
        assert kwargs.get('vertical') == 'auto'
        # Metadata carriers preserved on the cached string subclass.
        assert isinstance(out, _ContentWithDisplayResults)
        assert str(out) == 'FORMATTED-LLM-TEXT'
        assert out.engine_breakdown == breakdown
        assert len(out.display_results) == 1
        assert out.display_results[0]['url'] == 'https://a.com'
        # full_content is stripped but fetched/fetchedChars derived from it.
        assert 'full_content' not in out.display_results[0]
        assert out.display_results[0]['fetched'] is True
        assert fmt.called

    def test_single_query_zero_results_carries_search_diag(self):
        from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
        acc = StreamingToolAccumulator(_make_task(), project_path='/tmp')
        diag = {'reason': 'no_matches', 'engine_ok': ['bing']}
        fake_ret = ([], diag, None, None)
        with patch('lib.tasks_pkg.handlers.search._web_search_one', return_value=fake_ret), \
             patch.object(_tofu_search_mod(), 'format_search_for_tool_response',
                          return_value='no results text'):
            out = acc._execute_one('web_search', {'query': 'zzz'})
        assert out.display_results == []
        assert out.search_diag == diag

    def test_single_query_vertical_payload_attached(self):
        from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
        acc = StreamingToolAccumulator(_make_task(), project_path='/tmp')
        vresult = {'domain': 'academic',
                   'sources': [{'type': 'arxiv', 'source': 'arXiv'}],
                   'items': [{'title': 'Paper', 'url': 'u'}],
                   'content': '# Paper header'}
        fake_ret = (_results('https://a.com'), None, None, vresult)
        with patch('lib.tasks_pkg.handlers.search._web_search_one', return_value=fake_ret), \
             patch.object(_tofu_search_mod(), 'format_search_for_tool_response',
                          return_value='body'):
            out = acc._execute_one('web_search', {'query': 'mamba', 'vertical': 'academic'})
        # Single-query vertical → singular dict carrier (NOT {'batch': …}).
        assert isinstance(out.vertical, dict)
        assert out.vertical.get('domain') == 'academic'
        assert 'batch' not in out.vertical
        # Vertical header prepended to the LLM text.
        assert 'Vertical Search' in str(out)

    def test_batch_routes_through_web_search_one_with_batch_carrier(self):
        from lib.tasks_pkg.streaming_tool_executor import (
            StreamingToolAccumulator, _ContentWithDisplayResults)
        acc = StreamingToolAccumulator(_make_task(), project_path='/tmp')

        vresult = {'domain': 'code', 'sources': [{'type': 'github', 'source': 'GitHub'}],
                   'items': [{'title': 'repo', 'url': 'g'}], 'content': '# repo'}

        def fake(q, user_question, freshness, vertical='auto'):
            assert user_question == 'origin of tofu'
            if q == 'q2':
                return (_results('https://b.com'), None, None, vresult)
            return (_results('https://a.com'), None, None, None)

        with patch('lib.tasks_pkg.handlers.search._web_search_one', side_effect=fake) as m, \
             patch.object(_tofu_search_mod(), 'format_search_for_tool_response',
                          return_value='FMT'):
            out = acc._execute_one('web_search', {'queries': ['q1', 'q2']})

        assert m.call_count == 2
        assert isinstance(out, _ContentWithDisplayResults)
        # Per-query header + `_q` tagging.
        assert '=== Search: q1 ===' in str(out)
        assert {dr['_q'] for dr in out.display_results} == {'q1', 'q2'}
        # Batch verticals land in the {'batch': [...]} carrier consumed at
        # tool_dispatch.py:1063; each payload carries its source query.
        assert isinstance(out.vertical, dict) and 'batch' in out.vertical
        assert len(out.vertical['batch']) == 1
        assert out.vertical['batch'][0]['query'] == 'q2'

    def test_double_neuter_delegation_is_load_bearing(self):
        """NEUTER: force _web_search_one to raise. The delegated path (via the
        serial _web_search_one) is the ONLY path — so the mock IS the search.
        Here we prove the streaming branch actually CALLS it (not some leftover
        inline perform_web_search): if _web_search_one is what's invoked, a
        RuntimeError from it must propagate out of _execute_one."""
        from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
        acc = StreamingToolAccumulator(_make_task(), project_path='/tmp')
        with patch('lib.tasks_pkg.handlers.search._web_search_one',
                   side_effect=RuntimeError('boom-from-web-search-one')) as m:
            with pytest.raises(RuntimeError, match='boom-from-web-search-one'):
                acc._execute_one('web_search', {'query': 'x'})
        assert m.call_count == 1
