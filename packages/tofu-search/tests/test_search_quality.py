"""Offline quality/efficiency contracts for the model-facing search path."""

import time

from tofu_search.config import get_config
from tofu_search.passages import select_relevant_passages
from tofu_search.search import orchestrator
from tofu_search.search.dedup import canonical_url_key, dedup_by_full_content
from tofu_search.search.format import format_search_for_tool_response
from tofu_search.search.rerank import rerank_by_bm25


def _result(url, title='topic', content=''):
    return {
        'url': url,
        'title': title,
        'snippet': f'{title} useful details',
        'source': 'test',
        'full_content': content,
    }


def test_canonical_url_drops_fragment_tracking_and_parameter_order():
    left = 'https://EXAMPLE.com:443/a%7Eb/?utm_source=x&b=2&a=1#section'
    right = 'http://example.com/a~b?a=1&b=2&fbclid=ignored'
    assert canonical_url_key(left) == canonical_url_key(right)


def test_canonical_url_preserves_meaningful_query_and_long_suffix():
    base = 'https://example.com/search?' + 'padding=' + ('x' * 300)
    assert canonical_url_key(base + '&q=alpha') != canonical_url_key(base + '&q=beta')


def test_full_content_dedup_collapses_mirrors_but_keeps_distinct_article():
    shared = ('alpha beta gamma delta epsilon ' * 200)
    results = [
        _result('https://one.example/a', content=shared),
        _result('https://mirror.example/a', content=shared + 'copyright'),
        _result('https://other.example/b', content=('quasar latency benchmark ' * 180)),
    ]
    out = dedup_by_full_content(results)
    assert len(out) == 2
    assert out[0]['duplicate_urls'] == ['https://mirror.example/a']


def test_passage_selector_finds_late_fact_under_budget():
    raw = ('menu account subscribe\n\n' * 500
           + 'Quasar latency is exactly 17 milliseconds in the benchmark.\n\n'
           + 'unrelated footer\n\n' * 300)
    selected = select_relevant_passages(raw, 'quasar latency benchmark', 1_500)
    assert '17 milliseconds' in selected
    assert len(selected) <= 1_500


def test_formatter_honours_shared_budget_and_keeps_each_source():
    results = [
        _result(f'https://source{i}.example/a', content=(f'topic fact {i} ' * 2_000))
        for i in range(3)
    ]
    rendered = format_search_for_tool_response(
        results, query='topic fact', max_total_content_chars=3_000)
    assert rendered.count('URL:') == 3
    # Page content plus omission labels/metadata: the expensive part stays at
    # the requested 3k budget rather than 3 x the original ~26k pages.
    assert len(rendered) < 5_000
    assert 'Query-Focused Excerpts' in rendered


def test_formatter_names_the_hosts_single_page_tool():
    rendered = format_search_for_tool_response(
        [_result('https://example.com/a', 'topic')],
        query='topic',
        fetch_tool_name='fetch_page',
    )
    assert 'call fetch_page("https://example.com/a")' in rendered
    assert 'fetch_url' not in rendered


def test_rerank_uses_engine_consensus_as_weak_tiebreaker():
    same = 'topic implementation details'
    results = [
        _result('https://single.example/a', same, same),
        {**_result('https://consensus.example/a', same, same),
         'engine_count': 3, 'rrf_score': 3 / 61},
        _result('https://other.example/a', 'unrelated', 'garden tomatoes'),
    ]
    out = rerank_by_bm25('topic implementation', results, top_k=2)
    assert out[0]['url'] == 'https://consensus.example/a'


def test_rerank_prevents_one_domain_monopoly():
    results = [
        _result(f'https://same.example/{i}', 'topic', 'topic topic details')
        for i in range(4)
    ] + [_result('https://independent.example/a', 'topic', 'topic details')]
    out = rerank_by_bm25('topic details', results, top_k=3)
    assert any('independent.example' in r['url'] for r in out)


def _stub_other_engines(monkeypatch):
    for name in ('search_brave', 'search_bing', 'search_ddg_api',
                 'search_searxng', 'search_marginalia'):
        monkeypatch.setattr(orchestrator, name, lambda q, n=6, freshness='': [])
    monkeypatch.setattr(orchestrator, 'xhs_search_available', lambda: False)
    monkeypatch.setattr(orchestrator, 'is_deepen_enabled', lambda: False)


def test_orchestrator_fetches_canonical_duplicate_once_and_merges_consensus(monkeypatch):
    tracked = _result('https://example.com/article?utm_source=ddg#top', 'topic')
    tracked['source'] = 'DDG-HTML'
    clean = _result('http://example.com/article', 'topic')
    clean['source'] = 'Bing'
    monkeypatch.setattr(orchestrator, 'search_ddg_html',
                        lambda q, n=6, freshness='': [tracked])
    _stub_other_engines(monkeypatch)
    monkeypatch.setattr(orchestrator, 'search_bing',
                        lambda q, n=6, freshness='': [clean])
    fetched = []
    monkeypatch.setattr(orchestrator, 'fetch_page_content',
                        lambda url, **kw: fetched.append(url) or ('topic body ' * 80))
    cfg = get_config().copy(prefetch_gate_enabled=False, search_deadline_secs=5)

    out = orchestrator.perform_web_search(
        'topic', max_results=2, filter_pages=False, rerank=False, config=cfg)

    assert len(out) == 1
    assert len(fetched) == 1
    assert set(out[0]['sources']) == {'DDG-HTML', 'Bing'}
    assert out[0]['engine_count'] == 2


def test_fetch_candidate_budget_bounds_work(monkeypatch):
    rows = [
        _result(f'https://host{i}.example/a',
                f'topic unique{i}alpha unique{i}beta unique{i}gamma unique{i}delta')
        for i in range(30)
    ]
    monkeypatch.setattr(orchestrator, 'search_ddg_html',
                        lambda q, n=6, freshness='': rows)
    _stub_other_engines(monkeypatch)
    fetched = []
    monkeypatch.setattr(orchestrator, 'fetch_page_content',
                        lambda url, **kw: fetched.append(url) or (url + ' topic ' * 80))
    cfg = get_config().copy(prefetch_gate_enabled=False, search_deadline_secs=5)

    orchestrator.perform_web_search(
        'topic', max_results=2, filter_pages=False, config=cfg)

    assert len(set(fetched)) <= 12  # max(12, max_results * 3)


def test_race_to_n_returns_without_joining_slow_losers(monkeypatch):
    rows = [
        _result(f'https://host{i}.example/a',
                f'topic unique{i}alpha unique{i}beta unique{i}gamma unique{i}delta')
        for i in range(12)
    ]
    monkeypatch.setattr(orchestrator, 'search_ddg_html',
                        lambda q, n=6, freshness='': rows)
    _stub_other_engines(monkeypatch)

    def fetch(url, **kwargs):
        idx = int(url.split('host', 1)[1].split('.', 1)[0])
        if idx >= 4:
            time.sleep(1.0)
        return f'{url} topic unique content ' * 80

    monkeypatch.setattr(orchestrator, 'fetch_page_content', fetch)
    cfg = get_config().copy(prefetch_gate_enabled=False, search_deadline_secs=5)
    started = time.monotonic()
    out = orchestrator.perform_web_search(
        'topic', max_results=2, filter_pages=False, rerank=False, config=cfg)
    elapsed = time.monotonic() - started

    assert len(out) == 2
    assert elapsed < 0.75, f'Race-to-N waited for slow losers: {elapsed:.2f}s'


def test_low_remaining_budget_skips_uncancellable_retries(monkeypatch):
    retry_calls = []

    for name in ('search_ddg_html', 'search_brave', 'search_bing',
                 'search_ddg_api', 'search_searxng', 'search_marginalia'):
        monkeypatch.setattr(
            orchestrator, name,
            lambda q, n=6, freshness='', _name=name: retry_calls.append(_name) or [])
    monkeypatch.setattr(orchestrator, 'search_via_browser',
                        lambda *a, **kw: retry_calls.append('browser') or [])
    monkeypatch.setattr(orchestrator, 'xhs_search_available', lambda: False)
    cfg = get_config().copy(search_deadline_secs=1)

    orchestrator.perform_web_search(
        'nothing', max_results=2, filter_pages=False, config=cfg)

    # Each configured engine runs once in the parallel first wave. A second
    # DDG/Brave call or the 25s browser fallback would violate this tiny budget.
    assert retry_calls.count('search_ddg_html') == 1
    assert retry_calls.count('search_brave') == 1
    assert 'browser' not in retry_calls
