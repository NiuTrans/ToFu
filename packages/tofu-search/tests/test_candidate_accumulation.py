"""A malformed engine row cannot reserve another engine's valid evidence URL."""
import pytest

from tofu_search import SearchConfig, perform_web_search
from tofu_search.search import orchestrator

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('field, invalid, malformed_first', [
    ('engine_count', 'not-an-integer', True),
    ('rrf_score', 'not-a-number', True),
    ('sources', 'not-a-list', True),
    ('engine_count', 'not-an-integer', False),
])
def test_failed_contribution_cannot_poison_an_independent_source(
        monkeypatch, field, invalid, malformed_first):
    row = {'url': 'https://observatory.example/quasar',
           'title': 'Quasar calibration sensor',
           'snippet': 'Quasar calibration sensor operating instructions.'}
    failed_source = 'DDG-HTML' if malformed_first else 'Bing'
    valid_source = 'Bing' if malformed_first else 'DDG-HTML'
    malformed = dict(row, source=failed_source, **{field: invalid})
    valid = dict(row, source=valid_source)
    first, second = (malformed, valid) if malformed_first else (valid, malformed)
    monkeypatch.setattr(orchestrator, 'search_ddg_html', lambda *args: [first])
    monkeypatch.setattr(orchestrator, 'search_bing', lambda *args: [second])
    # Iterate submitted receipts in engine order; result() still joins each
    # genuine worker. A scheduler race must not decide which source is bad first.
    monkeypatch.setattr(orchestrator, 'as_completed', lambda futures, **kwargs: iter(futures))
    result = perform_web_search(
        'quasar calibration sensor', engines=['DDG-HTML', 'Bing'],
        fetch_pages=False, filter_pages=False, rerank=False, deepen=False,
        config=SearchConfig())
    assert len(result) == 1
    assert result[0]['url'] == row['url']
    assert result[0]['sources'] == [valid_source]
    assert result[0]['engine_count'] == 1
    from tofu_search.search.rerank import RRF_RANK_OFFSET
    assert result[0]['rrf_score'] == pytest.approx(1 / (RRF_RANK_OFFSET + 1))
    assert set(result._search_diag['engine_errors']) == {failed_source}
    assert valid == dict(row, source=valid_source)  # Provider-owned dictionaries stay intact.
