"""Similar excerpts are presentation groups, not proof that source pages match."""

import threading
from copy import deepcopy

import pytest

from tofu_search import SearchConfig, perform_web_search
from tofu_search.search import orchestrator
from tofu_search.search.format import format_search_for_tool_response

pytestmark = pytest.mark.unit


def listing(url, snippet, title='Quasar calibration'):
    return {'url': url, 'title': title, 'snippet': snippet, 'source': 'Bing'}


def controlled(monkeypatch, rows, *, fetch=False, count=2, **kwargs):
    monkeypatch.setattr(orchestrator, 'search_bing', lambda *a, **kw: rows)
    return perform_web_search('quasar calibration', max_results=count, engines=['Bing'],
        fetch_pages=fetch, filter_pages=False, deepen=False,
        config=SearchConfig(search_deadline_secs=5, prefetch_gate_enabled=False), **kwargs)


def grouped_rows():
    return [
        listing('https://first.example/report', 'Quasar calibration completed in 1976 with manual verification.'),
        listing('https://origin.example/report', 'Quasar calibration completed in 1977 without manual verification.'),
        listing('https://garden.example/article', 'Tomatoes grow in the orchard.', 'Gardening'),
    ]


def test_similar_snippet_remains_discoverable_without_changing_representative(monkeypatch):
    rows = grouped_rows()
    before = deepcopy(rows)
    out = controlled(monkeypatch, rows)
    assert [r['url'] for r in out] == [rows[0]['url']]
    alternate = out[0]['similar_snippets'][0]
    assert (alternate['url'], alternate['snippet']) == (rows[1]['url'], rows[1]['snippet'])
    assert out[0]['sources'] == ['Bing'] and out[0]['engine_count'] == 1
    assert not out[0].get('duplicate_urls')
    assert rows == before  # Provider-owned cached dictionaries remain untouched.
    rendered = format_search_for_tool_response(out, query='quasar calibration')
    assert rows[1]['url'] in rendered and rows[1]['snippet'] in rendered
    assert 'not verified duplicates' in rendered


def test_provider_cannot_inject_unobserved_related_urls(monkeypatch):
    row = grouped_rows()[0]
    row['similar_snippets'] = [listing('https://unobserved.example/secret', 'fabricated')]
    row['similar_snippets_omitted'] = 999
    before = deepcopy(row)
    out = controlled(monkeypatch, [row])
    assert not out[0].get('similar_snippets')
    assert not out[0].get('similar_snippets_omitted')
    assert 'unobserved.example' not in format_search_for_tool_response(out)
    assert row == before


def test_related_identity_budget_is_shared_across_visible_groups(monkeypatch):
    rows = []
    for group, text in [('measure', 'measured sensor wavelength optics laboratory nanometer'),
                        ('build', 'compiled library dependency versions wheels deployment')]:
        for i in range(5):
            rows.append(listing(f'https://{group}{i}.example/a', f'Quasar calibration {text}.'))
    out = controlled(monkeypatch, rows, count=2)
    assert len(out) == 2
    assert sum(len(r.get('similar_snippets', [])) for r in out) == 2
    assert all(len(r['similar_snippets']) == 1 for r in out)
    assert all(r['similar_snippets_omitted'] == 3 for r in out)
    shown = [r['similar_snippets'][0]['url'] for r in out]
    assert len(set(shown)) == 2


def test_site_excluded_results_never_become_similar_candidates(monkeypatch):
    rows = grouped_rows()[:2]
    monkeypatch.setattr(orchestrator, 'search_bing', lambda *a, **kw: rows)
    out = perform_web_search('site:first.example quasar calibration', max_results=1,
        engines=['Bing'], fetch_pages=False, filter_pages=False, deepen=False,
        config=SearchConfig())
    assert len(out) == 1 and not out[0].get('similar_snippets')
    assert rows[1]['url'] not in format_search_for_tool_response(out)


def test_fetched_alternative_survives_failed_representative(monkeypatch):
    rows = grouped_rows()
    body = 'Quasar calibration measured a wavelength of 17 nanometers using the observatory sensor. ' * 12
    calls = []
    def fetch(url, **kwargs):
        calls.append(url)
        return body if url == rows[1]['url'] else None
    monkeypatch.setattr(orchestrator, 'fetch_page_content', fetch)
    out = controlled(monkeypatch, rows, fetch=True)
    assert calls.count(rows[1]['url']) == 1
    assert out[0]['url'] == rows[1]['url'] and out[0]['full_content'] == body


@pytest.mark.parametrize('mirror', [False, True])
def test_fetched_bodies_not_snippet_similarity_determine_copy_status(monkeypatch, mirror):
    rows = grouped_rows()
    first = 'Quasar calibration measured a wavelength of 17 nanometers using the observatory sensor. ' * 12
    second = first if mirror else 'Quasar calibration requires rotating the actuator before applying a new voltage. ' * 12
    bodies = {rows[0]['url']: first, rows[1]['url']: second}
    monkeypatch.setattr(orchestrator, 'fetch_page_content', lambda url, **kw: bodies.get(url))
    out = controlled(monkeypatch, rows, fetch=True)
    assert {r['full_content'] for r in out} == set(bodies.values())
    if mirror:
        assert len(out) == 1 and out[0]['duplicate_urls'] == [rows[1]['url']]
        assert not out[0].get('similar_snippets')
    else:
        assert len(out) == 2 and not any(r.get('duplicate_urls') for r in out)


def test_zero_body_budget_preserves_related_urls_without_stealing_primary_evidence():
    row = listing('https://primary.example/a', 'PRIMARY_SOURCE_PAYLOAD')
    row['similar_snippets'] = [listing('https://related.example/文档?q=a%2Fb', 'ALTERNATE_SOURCE_PAYLOAD')]
    rendered = format_search_for_tool_response([row], max_total_content_chars=len(row['snippet']))
    assert row['snippet'] in rendered
    assert 'ALTERNATE_SOURCE_PAYLOAD' not in rendered
    assert row['similar_snippets'][0]['url'] in rendered
    empty = format_search_for_tool_response([row], max_total_content_chars=0)
    assert row['snippet'] not in empty and row['similar_snippets'][0]['url'] in empty


def test_no_usable_result_still_exposes_similar_discovery_urls(monkeypatch):
    rows = [listing(f'https://garden{i}.example/a', 'Grow tomatoes in sunny orchards.', 'Gardening') for i in range(3)]
    out = controlled(monkeypatch, rows, count=2)
    assert not out
    rendered = format_search_for_tool_response(out, max_total_content_chars=0)
    assert rows[1]['url'] in rendered and rows[2]['url'] in rendered
    assert 'not verified duplicates' in rendered


def test_unverified_sidecar_cannot_retain_provider_injected_related_urls(monkeypatch):
    rows = [listing(f'https://irrelevant{i}.example/a', f'unique{i}alpha unique{i}beta unique{i}gamma', f'entry{i}')
            for i in range(4)]
    rows[-1]['similar_snippets'] = [listing('https://never-observed.example/a', 'fake')]
    out = controlled(monkeypatch, rows, count=6)
    assert len(out._unverified_candidates) == 4
    assert not any(r.get('similar_snippets') for r in out._unverified_candidates)


def test_rejected_body_and_its_copies_do_not_return_as_related_listings(monkeypatch):
    rows = grouped_rows()[:2]
    rows.append({**rows[1], 'url': 'https://copy.example/report'})
    rows.extend(listing(f'https://filler{i}.example/a', f'unique{i}garden vegetables orchard', f'Garden {i}')
                for i in range(4))
    rejected_body = 'Quasar calibration: this document is not useful for the requested evidence. ' * 12
    monkeypatch.setattr(orchestrator, 'search_bing', lambda *a, **kw: rows)
    monkeypatch.setattr(orchestrator, 'fetch_page_content',
        lambda url, **kw: rejected_body if url in {rows[1]['url'], rows[2]['url']} else None)
    monkeypatch.setattr(orchestrator, 'filter_web_contents_batch',
        lambda pages, **kw: {url: orchestrator.IRRELEVANT_SENTINEL for url, _ in pages})
    out = perform_web_search('quasar calibration', max_results=6, engines=['Bing'],
        deepen=False, config=SearchConfig(filter_enabled=True, llm_api_key='test-only',
            prefetch_gate_enabled=False, search_deadline_secs=5))
    assert [r['url'] for r in out] == [rows[0]['url']]
    assert not out[0].get('similar_snippets')
    rendered = format_search_for_tool_response(out)
    assert rows[1]['url'] not in rendered and rows[2]['url'] not in rendered


def test_race_stop_keeps_already_completed_alternative_without_waiting_for_loser(monkeypatch):
    rows = grouped_rows()[:2] + [
        listing('https://other.example/a', 'Quasar calibration software build versions packaging compiled library.'),
        listing('https://slow.example/a', 'Quasar calibration ocean currents salinity marine coastal vessel.'),
    ]
    release, retired = threading.Event(), threading.Event()
    body = 'Quasar calibration wavelength is exactly 17 nanometers, measured with the sensor. ' * 12
    def fetch(url, **kwargs):
        if url == rows[3]['url']:
            try:
                assert release.wait(3), 'Test did not release its slow fetch'
                return 'Quasar calibration delayed result. ' * 12
            finally:
                retired.set()
        return body if url == rows[1]['url'] else 'Quasar calibration background notes and general overview. ' * 12
    real_completed = orchestrator.as_completed
    def completed(futures, **kwargs):
        pending = list(futures)
        iterator = iter(real_completed(pending, **kwargs))
        if len(pending) != 4:
            yield from iterator
            return
        # All three fast bodies have settled. Deliver representatives first so
        # Race-to-N stops while one completed alternative remains unconsumed.
        fast = [next(iterator) for _ in range(3)]
        by_url = {future.result()[0]['url']: future for future in fast}
        for row in (rows[0], rows[2], rows[1]):
            yield by_url[row['url']]
        yield from iterator
    monkeypatch.setattr(orchestrator, 'as_completed', completed)
    monkeypatch.setattr(orchestrator, 'search_bing', lambda *a, **kw: rows)
    monkeypatch.setattr(orchestrator, 'fetch_page_content', fetch)
    try:
        out = perform_web_search('quasar calibration wavelength', max_results=2,
            engines=['Bing'], filter_pages=False, deepen=False,
            config=SearchConfig(prefetch_gate_enabled=False, search_deadline_secs=2))
        assert not retired.is_set()
        assert any(r['url'] == rows[1]['url'] and r.get('full_content') == body for r in out)
    finally:
        release.set()
        assert retired.wait(1)
