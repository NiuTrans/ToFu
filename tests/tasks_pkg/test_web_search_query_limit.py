"""web_search refuses an over-long query at the tool boundary, per query.

Every engine rejects ~3.5K-character queries (HTTP 414/400, transport
failure). The refusal states the length and the limit and asks for keywords;
valid queries in the same batch still run. Decision record:
docs/modules/tools_execution.md, "web_search query length".
"""
from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

from lib.tasks_pkg.handlers.search import _handlers
from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
from lib.tools.search import WEB_SEARCH_MAX_QUERY_CHARS, build_search_tool

pytestmark = pytest.mark.unit

LONG = '论文报告：面向熟悉大模型但不了解 ToFu 的研究者讲解白盒 agent harness。' * 60


class SearchResults(list):
    pass


@pytest.fixture
def searched(monkeypatch):
    calls = []
    formatter = importlib.import_module('tofu_search.search').format_search_for_tool_response

    def one(query, user_question, freshness, vertical='auto'):
        calls.append(query)
        results = SearchResults([{
            'title': f'About {query}', 'source': 'Web', 'snippet': f'{query} snippet',
            'url': 'https://example.org/' + query.strip().replace(' ', '-')[:40]}])
        return results, None, None, None

    monkeypatch.setattr(_handlers, '_load_search_backend',
                        lambda: (SimpleNamespace(_web_search_one=one), formatter))
    events = []
    monkeypatch.setattr(_handlers, 'append_event', lambda task, event: events.append(event))
    return calls, events


def _task():
    return {'id': 'query-limit', '_userId': 17, 'lastUserQuery': 'topic', 'aborted': False}


def run_serial(args):
    row = {}
    _, content, is_search = _handlers._handle_web_search(
        _task(), {}, 'web_search', 'call', args, 1, row, {}, '/tmp', False)
    return content, is_search, row


def run_streaming(args):
    return StreamingToolAccumulator(_task(), project_path='/tmp')._execute_one('web_search', args)


def refusal_of(query):
    return f'this search query is {len(query.strip())} characters'


def test_batch_refuses_only_the_overlong_query(searched):
    calls, _events = searched
    args = {'queries': ['short alpha', {'query': LONG}, {'query': 'short beta'}]}
    content, is_search, row = run_serial(args)
    text = str(content)
    assert sorted(calls) == ['short alpha', 'short beta']
    assert is_search
    assert f'=== Search: {LONG} ===\nError: {refusal_of(LONG)}' in text
    assert f'at most {WEB_SEARCH_MAX_QUERY_CHARS}' in text
    assert 'keywords' in text
    assert 'About short alpha' in text and 'About short beta' in text
    assert (text.index('=== Search: short alpha ===') < text.index(refusal_of(LONG))
            < text.index('=== Search: short beta ==='))
    assert row['_batchQueries'] == ['short alpha', LONG, 'short beta']
    assert {result['_q'] for result in row['results']} == {'short alpha', 'short beta'}

    calls.clear()
    streamed = run_streaming(args)
    assert sorted(calls) == ['short alpha', 'short beta']
    assert str(streamed) == text


def test_batch_of_only_overlong_queries_runs_nothing(searched):
    calls, _events = searched
    second = 'x' * (WEB_SEARCH_MAX_QUERY_CHARS + 1)
    content, _is_search, row = run_serial({'queries': [LONG, second]})
    assert calls == []
    assert refusal_of(LONG) in content and refusal_of(second) in content
    assert row['results'] == [] and row['_cacheableResult'] is False
    assert str(run_streaming({'queries': [LONG, second]})) == str(content)
    assert calls == []


def test_single_overlong_query_is_refused_before_the_search_backend(monkeypatch):
    def unavailable():
        raise AssertionError('an over-long query must not activate the search backend')

    monkeypatch.setattr(_handlers, '_load_search_backend', unavailable)
    events = []
    monkeypatch.setattr(_handlers, 'append_event', lambda task, event: events.append(event))
    content, is_search, row = run_serial({'query': LONG})
    assert not is_search
    assert content.startswith('Error: ' + refusal_of(LONG))
    assert f'at most {WEB_SEARCH_MAX_QUERY_CHARS}' in content
    assert 'keywords' in content
    assert row['status'] == 'done' and row['results'] == []
    assert events[-1]['type'] == 'tool_result' and events[-1]['results'] == []

    streamed = run_streaming({'query': LONG})
    assert str(streamed) == content
    # A completed failure is settled by the serial handler, never a cached read.
    assert streamed.cacheable is False


@pytest.mark.parametrize('padding', ['', ' \n'])
def test_limit_counts_characters_without_surrounding_whitespace(searched, padding):
    calls, _events = searched
    at_limit = padding + 'a' * WEB_SEARCH_MAX_QUERY_CHARS + padding
    _content, is_search, _row = run_serial({'query': at_limit})
    assert is_search and calls == [at_limit]
    over = 'a' * (WEB_SEARCH_MAX_QUERY_CHARS + 1)
    content, is_search, _row = run_serial({'query': over})
    assert not is_search and calls == [at_limit]
    assert refusal_of(over) in content


def test_contract_admits_a_batch_holding_an_overlong_query():
    """A schema maxLength would reject the whole call before any query ran."""
    from lib.tools.contracts import (
        compile_execution_contract_documents, validate_tool_arguments_from_documents,
    )
    documents = compile_execution_contract_documents([build_search_tool()])
    args = {'queries': [{'query': 'short alpha'}, {'query': LONG}]}
    validated = validate_tool_arguments_from_documents(documents, 'web_search', args)
    assert validated['queries'][1]['query'] == LONG


def test_package_entry_refuses_exactly_beyond_the_host_limit():
    """Direct callers (production research, recipes) share the same limit.

    An explicit empty engine selection fails closed without network, so the
    package's length admission must answer first for an over-long query.
    """
    from tofu_search import perform_web_search

    def reason(query):
        result = perform_web_search(query, engines=[], fetch_pages=False,
                                    filter_pages=False, rerank=False, deepen=False)
        return result._search_diag['reason']

    assert reason('a' * (WEB_SEARCH_MAX_QUERY_CHARS + 1)) == 'query_too_long'
    assert reason('a' * WEB_SEARCH_MAX_QUERY_CHARS) == 'invalid_engines'
