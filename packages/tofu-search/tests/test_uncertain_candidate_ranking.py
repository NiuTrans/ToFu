"""Discovery previews rank useful partial matches without upgrading evidence."""

from copy import deepcopy

import pytest

from tofu_search import SearchConfig, perform_web_search
from tofu_search.search import orchestrator
from tofu_search.search.format import format_search_for_tool_response

pytestmark = pytest.mark.unit


def pool(subject='quasar', detail='calibration sensor'):
    rows = [
        {'url': f'https://index{i}.example/a', 'title': f'{subject} overview {i}',
         'snippet': f'{subject} general introduction and navigation.', 'source': 'Bing'}
        for i in range(3)
    ]
    rows.append({'url': 'https://manual.example/资料?q=a%2Fb', 'title': f'{subject} {detail}',
                 'snippet': f'{subject} {detail} details.', 'source': 'Bing'})
    return rows


def search(monkeypatch, rows, query, *, rerank=True, fetch=False):
    monkeypatch.setattr(orchestrator, 'search_bing', lambda *a, **kw: rows)
    return perform_web_search(query, engines=['Bing'], max_results=6, rerank=rerank,
        fetch_pages=fetch, filter_pages=False, deepen=False,
        config=SearchConfig(prefetch_gate_enabled=False, search_deadline_secs=5))


@pytest.mark.parametrize('query,subject,detail', [
    ('quasar calibration sensor wavelength optics observatory temperature measurement',
     'quasar', 'calibration sensor'),
    ('Nimbus WAL 读者 写者 同时进行 官方文档', 'Nimbus', 'WAL'),
])
def test_partial_match_enters_preview_without_becoming_primary_evidence(monkeypatch, query, subject, detail):
    rows = pool(subject, detail)
    original = deepcopy(rows)
    result = search(monkeypatch, rows, query)

    assert not result
    assert result._search_diag['reason'] == 'no_usable_evidence'
    assert result._search_diag['unverified_count'] == len(rows)
    preview = result._search_diag['unverified_candidates']
    assert rows[-1]['url'] in {row['url'] for row in preview}
    assert len(preview) == 3
    assert {row['url'] for row in result._unverified_candidates} == {row['url'] for row in rows}
    assert all(row['relevance'] == 'weak_match' for row in result._unverified_candidates)
    assert rows == original
    text = format_search_for_tool_response(result, query=query, max_total_content_chars=0)
    assert rows[-1]['url'] in text
    assert 'not evidence of relevance or absence' in text


def test_uncertainty_selection_keeps_primary_results_separate(monkeypatch):
    query = 'quasar calibration sensor wavelength optics observatory temperature measurement'
    rows = pool()
    primary = {'url': 'https://complete.example/a', 'title': query,
               'snippet': 'An observatory report with measured sensor wavelengths.', 'source': 'Bing'}
    result = search(monkeypatch, rows + [primary], query)

    assert [row['url'] for row in result] == [primary['url']]
    assert result[0]['relevance'] == 'lexical_match'
    assert rows[-1]['url'] in {row['url'] for row in result._search_diag['unverified_candidates']}
    assert len(result._unverified_candidates) == len(rows)


def test_rerank_opt_out_preserves_candidate_arrival_order(monkeypatch):
    query = 'quasar calibration sensor wavelength optics observatory temperature measurement'
    rows = pool()
    result = search(monkeypatch, rows, query, rerank=False)

    assert [row['url'] for row in result._unverified_candidates] == [row['url'] for row in rows]
    assert [row['url'] for row in result._search_diag['unverified_candidates']] == [row['url'] for row in rows[:3]]


def test_partial_fetched_body_remains_uncertain_and_lossless(monkeypatch):
    query = 'quasar calibration sensor wavelength optics observatory temperature measurement'
    rows = pool()
    body = 'Quasar calibration sensor details from the instrument manual. ' * 10
    monkeypatch.setattr(orchestrator, 'fetch_page_content',
                        lambda url, **kw: body if url == rows[-1]['url'] else None)
    result = search(monkeypatch, rows, query, fetch=True)

    assert not result
    assert rows[-1]['url'] in {row['url'] for row in result._search_diag['unverified_candidates']}
    received = next(row for row in result._unverified_candidates if row['url'] == rows[-1]['url'])
    assert received['relevance'] == 'weak_match'
    assert received['full_content'] == body
