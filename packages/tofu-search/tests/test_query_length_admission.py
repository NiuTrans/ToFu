"""Offline contracts: an over-long query is refused before any engine runs.

Every engine rejected ~3.5K-character queries (HTTP 414/400, transport
failure) and three such rejections opened each engine's circuit breaker for
every other caller. See docs/free-search.md, "Query length admission".
"""

from unittest.mock import Mock

import pytest

from tofu_search import providers
from tofu_search.search import orchestrator as orch
from tofu_search.search.format import format_search_for_tool_response

pytestmark = pytest.mark.unit

_ENGINE_NAMES = ('search_ddg_html', 'search_ddg_api', 'search_brave', 'search_bing',
                 'search_searxng', 'search_marginalia', 'search_via_browser',
                 'search_xhs', 'search_gateway_source', 'fetch_page_content')
_PARAGRAPH = ('【唯一来源】论文 ACL LaTeX 原稿有效正文与附录（Table 1、Fig.3）。'
              '禁止联网检索，禁止新增实验、日期、单位或论文未给出的数字。全程中文。') * 60


@pytest.fixture
def engines(monkeypatch):
    site = Mock(spec=providers.SiteSearchProvider)
    site.bind.return_value = site
    site.list_sources.return_value = []
    providers.register_site_search_provider(site)
    mocks = {name: Mock(return_value=[]) for name in _ENGINE_NAMES}
    for name, mock in mocks.items():
        monkeypatch.setattr(orch, name, mock)
    return site, mocks


def search(query):
    return orch.perform_web_search(query, fetch_pages=False, filter_pages=False,
                                   rerank=False, deepen=False)


@pytest.mark.parametrize('query', ['a' * (orch.MAX_QUERY_CHARS + 1), _PARAGRAPH],
                         ids=['one-over', 'instruction-paragraph'])
def test_overlong_query_is_refused_before_any_engine(engines, query):
    site, mocks = engines
    result = search(query)
    assert result == []
    diag = result._search_diag
    assert diag['reason'] == 'query_too_long'
    assert f'{len(query)} characters' in diag['reason_detail']
    assert f'at most {orch.MAX_QUERY_CHARS}' in diag['reason_detail']
    assert diag['engine_errors'] == {}
    for fn in mocks.values():
        fn.assert_not_called()
    site.search.assert_not_called()


def test_query_at_the_limit_still_reaches_engines(engines):
    _site, mocks = engines
    query = ' ' + 'a' * orch.MAX_QUERY_CHARS + '\n'
    result = search(query)
    assert (result._search_diag or {}).get('reason') != 'query_too_long'
    mocks['search_bing'].assert_called_once()
    assert mocks['search_bing'].call_args.args[0] == query


def test_formatter_states_length_limit_and_keyword_guidance(engines):
    query = 'a' * (orch.MAX_QUERY_CHARS + 1)
    text = format_search_for_tool_response(search(query), query=query)
    assert f'{len(query)} characters' in text
    assert f'at most {orch.MAX_QUERY_CHARS}' in text
    assert 'keywords' in text
    assert 'Try rephrasing' not in text
