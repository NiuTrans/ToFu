"""Compact host search previews retain the exact received corpus in both lanes."""
from __future__ import annotations

import importlib
import json
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from lib.tasks_pkg.handlers.search import _handlers
from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
from lib.tools.result_value import capture_result

pytestmark = pytest.mark.unit
pytest_plugins = ['tests._artifact_sidecar']


class SearchResults(list):
    pass


def corpus(kind):
    results = SearchResults([
        {'title': f'Source {i}', 'url': f'https://example.org/{i}',
         'source': 'Web', 'snippet': 'topic snippet fact ' * 6000,
         'full_content': ('background ' * 4000 + f' topic fact needle-{i} ' +
                          'background ' * 5000) if kind == 'pages' else ''}
        for i in range(10)])
    results._unverified_candidates = [
        {'url': f'https://uncertain.org/{i}', 'snippet': f'uncertain fact {i}'}
        for i in range(40)]
    results._search_diag = {'reason': 'no_usable_evidence',
                            'engine_errors': {'bing': 'network_error'},
                            'engine_warnings': {'SearXNG': ['upstream_unavailable']},
                            'engine_ok': ['google'],
                            'unverified_count': 40,
                            'unverified_candidates': results._unverified_candidates}
    results._deadline_hit = True
    if kind == 'uncertain':
        results.clear()
    return results


@pytest.mark.parametrize('batch', [False, True])
@pytest.mark.parametrize('kind', ['pages', 'snippets', 'uncertain'])
def test_serial_streaming_cache_and_settled_round_retain_full_evidence(monkeypatch, batch, kind):
    import lib.tasks_pkg.tool_dispatch._pipeline as pipeline
    from lib.tool_results.tool_result_artifacts import ToolResultArtifactRepository

    results = corpus(kind)
    vertical = {'domain': 'academic', 'sources': [], 'items': [],
                'content': '# Vertical original'}
    formatter = importlib.import_module('tofu_search.search').format_search_for_tool_response
    seen = []

    def format_spy(*args, **kwargs):
        seen.append(kwargs['max_total_content_chars'])
        return formatter(*args, **kwargs)

    def search(query, user_question, freshness, vertical='auto'):
        return results, results._search_diag, {'google': ['https://example.org/0']}, globals_vertical

    globals_vertical = vertical
    backend = SimpleNamespace(_web_search_one=search)
    monkeypatch.setattr(_handlers, '_load_search_backend', lambda: (backend, format_spy))
    monkeypatch.setattr(_handlers, 'append_event', lambda *a, **k: None)
    monkeypatch.setattr(pipeline, 'append_event', lambda *a, **k: None)
    monkeypatch.setattr(pipeline, 'run_post_hooks', lambda name, args, text, task: text)
    args = {'queries': ['topic fact', {'query': 'other fact'}]} if batch else {'query': 'topic fact'}
    task = {'id': 'compact-search', '_userId': 17, 'model': 'test-model',
            'lastUserQuery': 'topic', '_tool_result_cache': {}, 'aborted': False}
    row = {}
    _, serial, is_search = _handlers._handle_web_search(
        task, {}, 'web_search', 'call', args, 1, row, {}, '/tmp', False)
    acc = StreamingToolAccumulator(task, project_path='/tmp')
    streamed = acc._execute_one('web_search', args)
    assert is_search
    assert str(serial) == str(streamed)
    assert json.loads(capture_result(serial).text()) == json.loads(capture_result(streamed).text())
    assert sum(seen[:2 if batch else 1]) == 20_000
    assert len(str(serial)) < 32_000  # metadata/diagnostics are outside body budget
    assert 'bing' in serial and 'Vertical Search' in serial
    assert 'SearXNG=upstream_unavailable' in serial
    assert 'SearXNG=upstream_unavailable' in streamed
    if batch:
        assert '=== Search: other fact ===' in serial
    if kind == 'pages':
        assert 'PARTIAL' in serial
        assert 'needle-0' in serial  # query-focused content, not a blind head slice
    assert streamed.search_diag

    # Exercise actual prefetch cache injection, not just the execution return.
    future = Future()
    future.set_result(streamed)
    acc._futures = {'call': (future, 'web_search', args, 0)}
    assert acc.inject_into_cache(task) == 1
    cached = next(iter(task['_tool_result_cache'].values()))[0]
    assert capture_result(cached).source_omitted
    assert capture_result(cached).text() == capture_result(serial).text()

    for lane, content in [('serial', serial), ('stream', cached)]:
        settled_row = {'status': 'done'}
        model = pipeline._settle_tool_result(
            task, 'web_search', lane, args, 1, settled_row, content,
            idempotent_tools=frozenset(), cache={}, tid=task['id'], round_num=1)
        evidence = settled_row['toolResultEvidence']
        assert evidence['retentionCoverage'] == 'complete'
        assert evidence['viewCoverage'] == 'partial'
        assert evidence['artifactRef'] in str(model)
        repository = ToolResultArtifactRepository()
        recovered = repository.read_range(user_id=17, artifact_ref=evidence['artifactRef'], limit=8192)
        assert 'https://uncertain.org/39' in capture_result(content).text()
        assert 'observations' in recovered['content']
        found = repository.search(user_id=17, artifact_ref=evidence['artifactRef'],
                                  query='uncertain fact 39')
        assert found['items']
        if kind == 'pages':
            found = repository.search(user_id=17, artifact_ref=evidence['artifactRef'], query='needle-9')
            assert found['items']


def test_accepted_only_results_are_captured_without_uncertainty(monkeypatch):
    formatter = importlib.import_module('tofu_search.search').format_search_for_tool_response
    results = corpus('pages')
    results._unverified_candidates = []
    results._search_diag = None
    monkeypatch.setattr(_handlers, '_load_search_backend', lambda: (
        SimpleNamespace(_web_search_one=lambda *a, **k: (results, None, None, None)),
        formatter))
    monkeypatch.setattr(_handlers, 'append_event', lambda *a, **k: None)
    task = {'id': 'accepted-only', '_userId': 17}
    args = {'query': 'topic fact'}
    _, serial, _ = _handlers._handle_web_search(
        task, {}, 'web_search', 'call', args, 1, {}, {}, '/tmp', False)
    streamed = StreamingToolAccumulator(task, project_path='/tmp')._execute_one('web_search', args)
    original = capture_result(serial)
    assert original.source_omitted
    assert json.loads(original.text())['observations'][0]['results'] == list(results)
    assert capture_result(streamed).text() == original.text()
    assert len(serial) < 24_000
    assert 'needle-9' in original.text()


def test_canonical_capture_precedes_formatter_mutation():
    from lib.tasks_pkg.handlers.search._presentation import (
        format_search_observations, search_observation)
    result = corpus('pages')
    original = result[0]['full_content']

    def mutating_formatter(results, **kwargs):
        results[0]['full_content'] = 'replacement'
        return 'preview'

    value = format_search_observations([search_observation('topic', result)], mutating_formatter)
    assert json.loads(capture_result(value).text())['observations'][0]['results'][0]['full_content'] == original


def test_batch_failed_query_keeps_boundary_and_canonical_failure(monkeypatch):
    from lib.tasks_pkg.handlers.search._presentation import (
        failed_search_observation, format_search_observations, search_observation)
    value = format_search_observations([
        failed_search_observation('failed'), search_observation('good', [])],
        lambda *a, **k: 'no results')
    assert '=== Search: failed ===' in value
    assert 'Search failed' in value
    assert '=== Search: good ===' in value
    assert json.loads(capture_result(value).text())['observations'][0]['failure']
