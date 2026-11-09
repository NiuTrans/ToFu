"""search.skip_domains blocks a URL at the fetch gate AND in search listings.

A blocked result must never reach the model through a search listing: the
listing carries the page's URL, title, snippet and fetched excerpts, which is
how hidden benchmark tests leaked through an otherwise-guarded fetch gate.
Fetch-quality skips (the built-in NO_TEXT_DOMAINS and fetch_skip_domains) are
not bans: they refuse only the fetch gate and one-hop deepening.
"""
import pytest

from tofu_search import SearchConfig, perform_web_search
from tofu_search.fetch.utils import _should_fetch
from tofu_search.search import orchestrator

pytestmark = pytest.mark.unit


def test_fetch_gate_refuses_url_prefix_and_allows_other_paths(monkeypatch):
    cfg = SearchConfig(skip_domains={'github.com/harbor-framework'})
    monkeypatch.setattr('tofu_search.fetch.utils.get_config', lambda: cfg)
    diag = {}
    assert _should_fetch(
        'https://github.com/harbor-framework/terminal-bench/blob/main/README.md', diag) is False
    assert diag['reason'] == 'skip_domain'
    assert 'harbor-framework' in diag['detail']
    assert _should_fetch('https://github.com/python/cpython/blob/main/README.rst') is True


def test_host_rule_boundary_at_the_fetch_gate(monkeypatch):
    cfg = SearchConfig(skip_domains={'x.com'})
    monkeypatch.setattr('tofu_search.fetch.utils.get_config', lambda: cfg)
    assert _should_fetch('https://www.dropbox.com/s/abc/file.pdf') is True
    assert _should_fetch('https://x.com/user/status/1') is False


def test_built_in_no_text_hosts_are_a_fetch_skip_not_a_ban(monkeypatch):
    cfg = SearchConfig()
    monkeypatch.setattr('tofu_search.fetch.utils.get_config', lambda: cfg)
    assert cfg.skip_domains == set()
    diag = {}
    assert _should_fetch('https://www.youtube.com/watch?v=dQw4w9WgXcQ', diag) is False
    assert diag['reason'] == 'fetch_skip'
    assert 'not a source ban' in diag['detail']


def test_configured_fetch_skip_refuses_and_a_ban_takes_precedence(monkeypatch):
    cfg = SearchConfig(skip_domains={'spam.example'},
                       fetch_skip_domains={'junk.example', 'spam.example'})
    monkeypatch.setattr('tofu_search.fetch.utils.get_config', lambda: cfg)
    diag = {}
    assert _should_fetch('https://junk.example/a', diag) is False
    assert diag['reason'] == 'fetch_skip'
    assert _should_fetch('https://spam.example/a', diag) is False
    assert diag['reason'] == 'skip_domain'


def _search(monkeypatch, rows, skip_domains):
    monkeypatch.setattr(orchestrator, 'search_ddg_html', lambda *args: rows)
    monkeypatch.setattr(orchestrator, 'as_completed', lambda futures, **kwargs: iter(futures))
    return perform_web_search(
        'pipeline parallel pytorch', engines=['DDG-HTML'],
        fetch_pages=False, filter_pages=False, rerank=False, deepen=False,
        config=SearchConfig(skip_domains=skip_domains))


def test_search_listing_drops_blocked_urls_and_keeps_others(monkeypatch):
    rows = [
        {'url': 'https://github.com/harbor-framework/terminal-bench/blob/main/archive/x/README.md',
         'title': 'hidden test file', 'snippet': 'def test_pipeline_parallel(world_size): ...'},
        {'url': 'https://docs.pytorch.org/docs/main/distributed.pipelining.html',
         'title': 'Pipeline Parallelism', 'snippet': 'pipeline parallel training in pytorch'},
    ]
    result = _search(monkeypatch, rows, {'github.com/harbor-framework'})
    urls = [r['url'] for r in result]
    assert urls == ['https://docs.pytorch.org/docs/main/distributed.pipelining.html']
    rendered = str(result)
    assert 'harbor-framework' not in rendered
    assert 'hidden test file' not in rendered


def test_search_listing_drops_answer_hosts_and_wildcard_repositories(monkeypatch):
    rows = [
        {'url': 'https://marginlab.ai/explorers/terminal-bench/x', 'title': 'explorer',
         'snippet': 'pipeline parallel tests'},
        {'url': 'https://github.com/lynl7130/terminal-bench/blob/main/x',
         'title': 'fork', 'snippet': 'pipeline parallel fork'},
        {'url': 'https://github.com/pytorch/pytorch', 'title': 'pytorch',
         'snippet': 'pipeline parallel pytorch framework'},
    ]
    result = _search(monkeypatch, rows, {'marginlab.ai', 'github.com/*/terminal-bench'})
    assert [r['url'] for r in result] == ['https://github.com/pytorch/pytorch']


def test_search_listing_keeps_fetch_skipped_hosts(monkeypatch):
    """Only a ban removes a listing row; the default config bans nothing."""
    rows = [
        {'url': 'https://www.youtube.com/watch?v=pp', 'title': 'pipeline parallel talk',
         'snippet': 'pipeline parallel pytorch video'},
        {'url': 'https://docs.pytorch.org/pipelining', 'title': 'pipeline parallel docs',
         'snippet': 'pipeline parallel documentation'},
    ]
    result = _search(monkeypatch, rows, SearchConfig().skip_domains)
    assert {r['url'] for r in result} == {row['url'] for row in rows}


def test_blocked_rows_do_not_reserve_the_result_budget(monkeypatch):
    rows = [
        {'url': f'https://github.com/harbor-framework/terminal-bench/x{i}',
         'title': 'pipeline parallel', 'snippet': 'pipeline parallel'} for i in range(3)
    ] + [{'url': 'https://docs.pytorch.org/pipelining', 'title': 'pipeline parallel docs',
          'snippet': 'pipeline parallel documentation'}]
    result = _search(monkeypatch, rows, {'github.com/harbor-framework'})
    assert [r['url'] for r in result] == ['https://docs.pytorch.org/pipelining']


def test_deepen_link_selection_uses_the_same_url_rule(monkeypatch):
    """A blocked outbound link is refused by the real deepen_results pipeline."""
    from tofu_search.config import SearchConfig
    from tofu_search.search import deepen
    fetched = []

    def fake_fetch(url, **kwargs):
        fetched.append(url)
        return 'deep content'

    monkeypatch.setattr(deepen, 'fetch_page_content', fake_fetch)
    monkeypatch.setattr(deepen, 'get_config',
                        lambda: SearchConfig(skip_domains={'github.com/harbor-framework'}))
    results = [{'url': 'https://docs.example/page', 'relevance': 'lexical_match',
                'title': 'pipeline parallel', 'snippet': 'pipeline parallel',
                'full_content': 'pipeline parallel text\n--- Page Links ---\n'
                                '- [pipeline parallel hidden](https://github.com/harbor-framework/x)\n'
                                '- [pipeline parallel public](https://docs.example/other)\n'}]
    deepen.deepen_results('pipeline parallel pytorch', results, max_links=5)
    assert 'https://github.com/harbor-framework/x' not in fetched
    assert fetched == ['https://docs.example/other']


def test_deepen_skips_fetch_skipped_links(monkeypatch):
    """Built-in and configured fetch-quality skips are not deepened into."""
    from tofu_search.search import deepen
    fetched = []

    def fake_fetch(url, **kwargs):
        fetched.append(url)
        return 'deep content'

    monkeypatch.setattr(deepen, 'fetch_page_content', fake_fetch)
    monkeypatch.setattr(deepen, 'get_config',
                        lambda: SearchConfig(fetch_skip_domains={'junk.example'}))
    results = [{'url': 'https://docs.example/page', 'relevance': 'lexical_match',
                'title': 'pipeline parallel', 'snippet': 'pipeline parallel',
                'full_content': 'pipeline parallel text\n--- Page Links ---\n'
                                '- [pipeline parallel talk](https://www.youtube.com/watch?v=pp)\n'
                                '- [pipeline parallel junk](https://junk.example/pp)\n'
                                '- [pipeline parallel public](https://docs.example/other)\n'}]
    deepen.deepen_results('pipeline parallel pytorch', results, max_links=5)
    assert fetched == ['https://docs.example/other']
