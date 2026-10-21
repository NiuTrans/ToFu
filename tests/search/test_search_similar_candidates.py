"""Host previews and canonical evidence retain bounded package discovery groups."""

import json

import pytest

from tofu_search import SearchConfig, format_results, perform_web_search
from tofu_search.search import orchestrator
from tofu_search.search.orchestrator import SearchResultList

from lib.tasks_pkg.handlers.search import _presentation

pytestmark = pytest.mark.unit


def test_host_keeps_related_urls_visible_and_original_evidence_lossless(monkeypatch):
    monkeypatch.setattr(_presentation, 'SEARCH_PREVIEW_CONTENT_CHARS', 30)
    candidate_url = 'https://origin.example/资料?q=1977%2F1976&lang=zh'
    rows = SearchResultList([{
        'url': 'https://first.example/report', 'title': 'Quasar report',
        'snippet': 'quasar calibration', 'source': 'Bing',
        'full_content': 'quasar calibration observation ' * 100,
        'similar_snippets': [{'url': candidate_url, 'title': 'Alternate report',
                              'snippet': 'A different observation from the search listing.'}],
        'similar_snippets_omitted': 2,
    }])
    observation = _presentation.search_observation('quasar calibration', rows)
    text = _presentation.format_search_observations([observation], format_results)

    assert candidate_url in text
    assert 'not verified duplicates or independent corroboration' in text
    assert '2 further similar listings omitted' in text
    assert rows[0]['full_content'] not in text
    canonical = json.loads(text.canonical.parts[0].text)
    assert canonical['observations'][0]['results'] == list(rows)
    assert text.canonical.source_coverage == 'unknown'


def test_host_exposes_ranked_uncertainty_without_losing_received_candidates(monkeypatch):
    query = 'Nimbus WAL 读者 写者 同时进行 官方文档'
    rows = [{'url': f'https://index{i}.example/a', 'title': f'Nimbus overview {i}',
             'snippet': 'Nimbus general introduction and navigation.', 'source': 'Bing'}
            for i in range(3)]
    target = 'https://manual.example/资料?q=a%2Fb'
    rows.append({'url': target, 'title': 'Nimbus WAL', 'snippet': 'Nimbus WAL details.', 'source': 'Bing'})
    monkeypatch.setattr(orchestrator, 'search_bing', lambda *a, **kw: rows)
    result = perform_web_search(query, engines=['Bing'], fetch_pages=False,
        filter_pages=False, deepen=False, config=SearchConfig(search_deadline_secs=5))
    text = _presentation.format_search_observations(
        [_presentation.search_observation(query, result)], format_results)

    assert target in text and 'not evidence of relevance or absence' in text
    captured = json.loads(text.canonical.parts[0].text)['observations'][0]
    assert not captured['results']
    assert captured['unverified_candidates'] == result._unverified_candidates
    assert {row['url'] for row in captured['unverified_candidates']} == {row['url'] for row in rows}
