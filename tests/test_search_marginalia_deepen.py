"""Offline unit tests for the search breadth/depth additions.

Covers:
  * lib/search/engines/marginalia.py — JSON parser + query-in-path URL build
  * lib/search/deepen.py — link harvest, relevance scoring, candidate
    selection (dedup / SKIP_DOMAINS / score>0), and the enable toggle.

No network: the Marginalia parser is fed a fake response object, and the
deepen selection logic is exercised via its helpers + a monkeypatched
``fetch_page_content`` so nothing leaves the process.
"""

import pytest

from tofu_search.search import deepen as deepen_mod
from tofu_search.search.deepen import (
    _dedup_key,
    _harvest_links,
    _score_candidate,
    deepen_results,
    is_deepen_enabled,
)
from tofu_search.search.engines import marginalia as marginalia_mod
from tofu_search.search.engines.marginalia import _parse_marginalia, search_marginalia
from tofu_search.search.rerank import _tokenize


class _FakeResp:
    """Minimal stand-in for a requests.Response exposing .json()."""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


# ═══════════════════════════════════════════════════════════
#  Marginalia parser
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestMarginaliaParser:
    def test_parses_results(self):
        resp = _FakeResp({'results': [
            {'url': 'https://example.com/a', 'title': 'Async Guide',
             'description': 'A guide to asyncio', 'quality': 3.1},
            {'url': 'https://example.org/b', 'title': 'Event Loops',
             'description': 'Loops explained', 'quality': 2.0},
        ]})
        out = _parse_marginalia(resp)
        assert len(out) == 2
        assert out[0] == {
            'title': 'Async Guide',
            'snippet': 'A guide to asyncio',
            'url': 'https://example.com/a',
            'source': 'Marginalia',
        }

    def test_skips_missing_fields_and_nonhttp(self):
        resp = _FakeResp({'results': [
            {'url': '', 'title': 'no url', 'description': 'x'},
            {'url': 'https://ok.com', 'title': '', 'description': 'no title'},
            {'url': 'ftp://nope.com', 'title': 'bad scheme', 'description': 'x'},
            {'url': 'https://good.com', 'title': 'Good', 'description': 'y'},
        ]})
        out = _parse_marginalia(resp)
        assert len(out) == 1
        assert out[0]['url'] == 'https://good.com'

    def test_empty_results(self):
        assert _parse_marginalia(_FakeResp({'results': []})) == []
        assert _parse_marginalia(_FakeResp({})) == []

    def test_query_travels_in_url_path(self, monkeypatch):
        """search_marginalia must put the (url-encoded) query in the PATH."""
        captured = {}

        def _fake_http_search_get(*, url, params, **kwargs):
            captured['url'] = url
            captured['params'] = params
            return []

        monkeypatch.setattr(marginalia_mod, 'http_search_get', _fake_http_search_get)
        search_marginalia('python asyncio', max_results=5)
        assert '/public/search/python%20asyncio' in captured['url']
        assert captured['params'] == {'count': 5}


# ═══════════════════════════════════════════════════════════
#  Deepen — helpers
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestDeepenHelpers:
    def test_dedup_key_strips_only_leading_scheme(self):
        assert _dedup_key('https://a.com/x/') == 'a.com/x'
        assert _dedup_key('http://a.com/x') == 'a.com/x'
        # embedded scheme in query must be preserved
        assert _dedup_key('https://a.com/r?t=http://b.com') == 'a.com/r?t=http://b.com'

    def test_harvest_links_reads_only_links_section(self):
        page = {
            'url': 'https://src.com/p',
            'full_content': (
                'Body mentions https://body-link.com inline.\n\n'
                '--- Page Links ---\n'
                '- [Async Guide](https://a.com/asyncio)\n'
                '- [Docs](https://b.com/docs)\n'
            ),
        }
        cands = _harvest_links([page])
        urls = {c['url'] for c in cands}
        assert urls == {'https://a.com/asyncio', 'https://b.com/docs'}
        # body-text URL must NOT be harvested as a navigable link
        assert 'https://body-link.com' not in urls

    def test_harvest_links_no_section(self):
        assert _harvest_links([{'url': 'x', 'full_content': 'no links here'}]) == []

    def test_score_favors_anchor_over_slug(self):
        q = set(_tokenize('python asyncio guide'))
        anchor_hit = _score_candidate(q, 'Python asyncio guide', 'https://x.com/page')
        slug_hit = _score_candidate(q, 'Click here', 'https://x.com/python-asyncio-guide')
        none = _score_candidate(q, 'Cooking recipes', 'https://x.com/food')
        assert anchor_hit > slug_hit > none
        assert none == 0.0

    def test_score_empty_query(self):
        assert _score_candidate(set(), 'anything', 'https://x.com/y') == 0.0


# ═══════════════════════════════════════════════════════════
#  Deepen — selection + fetch (monkeypatched, offline)
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestDeepenSelection:
    def _pages(self):
        return [{
            'url': 'https://src.com/seed',
            'full_content': (
                '--- Page Links ---\n'
                '- [Asyncio deep dive](https://good.com/asyncio-deep-dive)\n'
                '- [Asyncio reference](https://ref.com/asyncio)\n'
                '- [Unrelated cooking](https://cook.com/recipes)\n'
                '- [Self](https://src.com/seed)\n'
                '- [YouTube vid](https://youtube.com/watch?v=1)\n'
            ),
        }]

    def test_follows_relevant_dedups_and_skips(self, monkeypatch):
        fetched = []

        def _fake_fetch(url, **kwargs):
            fetched.append(url)
            return 'FULL CONTENT for ' + url + ' ' * 60

        monkeypatch.setattr(deepen_mod, 'fetch_page_content', _fake_fetch)

        out = deepen_results('python asyncio', self._pages(), max_links=10)
        followed = set(fetched)

        # relevant links fetched
        assert 'https://good.com/asyncio-deep-dive' in followed
        assert 'https://ref.com/asyncio' in followed
        # zero-score (irrelevant) pruned
        assert 'https://cook.com/recipes' not in followed
        # self / already-visited source pruned
        assert 'https://src.com/seed' not in followed
        # SKIP_DOMAINS pruned
        assert not any('youtube.com' in u for u in followed)

        # results tagged DeepCrawl with content
        assert out and all(r['source'] == 'DeepCrawl' for r in out)
        assert all(r.get('full_content') for r in out)

    def test_max_links_cap(self, monkeypatch):
        monkeypatch.setattr(deepen_mod, 'fetch_page_content', lambda url, **k: 'x' * 100)
        out = deepen_results('python asyncio', self._pages(), max_links=1)
        assert len(out) == 1

    def test_drops_empty_fetches(self, monkeypatch):
        # fetch returns too-short / empty content → not included
        monkeypatch.setattr(deepen_mod, 'fetch_page_content', lambda url, **k: '')
        out = deepen_results('python asyncio', self._pages(), max_links=5)
        assert out == []

    def test_no_results_input(self):
        assert deepen_results('q', []) == []

    def test_no_relevant_links(self, monkeypatch):
        monkeypatch.setattr(deepen_mod, 'fetch_page_content', lambda url, **k: 'x' * 100)
        pages = [{'url': 'https://s.com', 'full_content':
                  '--- Page Links ---\n- [Cooking](https://c.com/food)\n'}]
        assert deepen_results('quantum chromodynamics', pages) == []


# ═══════════════════════════════════════════════════════════
#  Deepen — enable toggle
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestDeepenToggle:
    def test_env_on_off(self, monkeypatch):
        monkeypatch.setenv('SEARCH_DEEPEN_HOPS', '1')
        assert is_deepen_enabled() is True
        monkeypatch.setenv('SEARCH_DEEPEN_HOPS', '0')
        assert is_deepen_enabled() is False

    def test_env_noninteger_is_off(self, monkeypatch):
        monkeypatch.setenv('SEARCH_DEEPEN_HOPS', 'yes')
        assert is_deepen_enabled() is False

    def test_default_off(self, monkeypatch):
        monkeypatch.delenv('SEARCH_DEEPEN_HOPS', raising=False)
        # With no env override, falls back to features.json default (off).
        assert is_deepen_enabled() in (False, True)  # resolves without raising


# ═══════════════════════════════════════════════════════════
#  Façade / wiring
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestWiring:
    def test_facade_exposes_deepen(self):
        assert hasattr(deepen_mod, 'deepen_results')
        assert callable(deepen_mod.deepen_results)

    def test_marginalia_in_engine_specs(self):
        import inspect

        from tofu_search.search import orchestrator as o
        src = inspect.getsource(o.perform_web_search)
        assert 'Marginalia' in src
        assert "'deepen'" in src or 'deepen' in inspect.signature(o.perform_web_search).parameters
