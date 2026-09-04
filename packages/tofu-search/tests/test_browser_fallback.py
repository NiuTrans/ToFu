"""Offline regression tests for the host-browser search fallback seams.

These lock in the contract that lets an embedding host (e.g. chatui) stay thin:
the host supplies only a transport (`BrowserProvider.fetch_html` → raw HTML),
and tofu-search owns the SERP parsing (`parse_ddg_html_text`). If a future
refactor breaks any of this, browser-search silently dies in the host with
nothing to catch it — hence these tests.

Covers:
  1. ``parse_ddg_html_text`` — uddg-unwrap, ad-link skip, max_results cap,
     custom source.
  2. ``search_via_browser`` — prefers ``fetch_html`` (parses HTML in-library),
     falls back to the provider's ``search()`` only when fetch_html → None.
  3. The logging-deferral invariant: importing ``tofu_search`` when the root
     logger already has a handler must NOT attach a ``tofu_search`` package
     handler (the heart of the log-routing fix).

All offline: no network, no real browser.
"""

import subprocess
import sys

import pytest

from tofu_search.providers import (
    BrowserProvider,
    get_browser_provider,
    register_browser_provider,
)
from tofu_search.search.browser_fallback import search_via_browser
from tofu_search.search.engines.ddg import parse_ddg_html_text

# A DDG lite SERP fragment exercising every parser branch:
#   - result 1: uddg-wrapped redirect URL (must be unwrapped) + snippet
#   - result 2: a sponsored /y.js?ad_ link (must be skipped)
#   - result 3: a plain http URL, no snippet
_DDG_HTML = """<html><body>
<div class="result results_links">
  <a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage&amp;rut=abc">Example Title</a>
  <a class="result__snippet">A useful snippet here</a>
</div>
<div class="result results_links">
  <a class="result__a" href="//duckduckgo.com/y.js?ad_provider=foo">Sponsored Ad</a>
</div>
<div class="result results_links">
  <a class="result__a" href="https://foo.org/article">Foo Article</a>
</div>
</body></html>"""


@pytest.fixture(autouse=True)
def _restore_provider():
    """Restore whatever provider was registered before each test.

    The shared conftest fixture resets the global SearchConfig but NOT the
    provider registry (a separate process-global), so a test that registers a
    provider would otherwise leak it into the next test.
    """
    saved = get_browser_provider()
    try:
        yield
    finally:
        register_browser_provider(saved)


# ══════════════════════════════════════════════════════════
#  1. parse_ddg_html_text
# ══════════════════════════════════════════════════════════

def test_parse_unwraps_uddg_and_keeps_snippet():
    results = parse_ddg_html_text(_DDG_HTML)
    assert results[0]['url'] == 'https://example.com/page'
    assert results[0]['title'] == 'Example Title'
    assert results[0]['snippet'] == 'A useful snippet here'


def test_parse_skips_ad_links():
    results = parse_ddg_html_text(_DDG_HTML)
    urls = [r['url'] for r in results]
    # The /y.js?ad_ sponsored link must not appear; the two real results do.
    assert urls == ['https://example.com/page', 'https://foo.org/article']


def test_parse_respects_max_results():
    results = parse_ddg_html_text(_DDG_HTML, max_results=1)
    assert len(results) == 1
    assert results[0]['url'] == 'https://example.com/page'


def test_parse_stamps_custom_source():
    results = parse_ddg_html_text(_DDG_HTML, source='DuckDuckGo (via browser)')
    assert results
    assert all(r['source'] == 'DuckDuckGo (via browser)' for r in results)


def test_parse_empty_html_returns_empty():
    assert parse_ddg_html_text('') == []
    assert parse_ddg_html_text('<html><body>no results</body></html>') == []


# ══════════════════════════════════════════════════════════
#  2. search_via_browser — fetch_html-first, search() fallback
# ══════════════════════════════════════════════════════════

def test_no_provider_returns_empty():
    register_browser_provider(None)
    assert search_via_browser('anything') == []


def test_disconnected_provider_returns_empty():
    class Disconnected(BrowserProvider):
        def is_connected(self):
            return False

        def fetch_html(self, url, *, timeout=20):  # pragma: no cover - must not run
            raise AssertionError('fetch_html called on disconnected provider')

    register_browser_provider(Disconnected())
    assert search_via_browser('q') == []


def test_prefers_fetch_html_and_parses_in_library():
    """A provider that only supplies raw HTML still yields parsed results,
    and the legacy search() path is never consulted."""
    calls = {'fetch_html': 0, 'search': 0}

    class HtmlProvider(BrowserProvider):
        def is_connected(self):
            return True

        def fetch_html(self, url, *, timeout=20):
            calls['fetch_html'] += 1
            assert 'duckduckgo.com/html' in url
            assert 'q=' in url  # query was URL-encoded into the SERP URL
            return _DDG_HTML

        def search(self, query, *, max_results=8):  # pragma: no cover
            calls['search'] += 1
            return [{'title': 'should not be used', 'snippet': '', 'url': 'https://x', 'source': 'x'}]

    register_browser_provider(HtmlProvider())
    out = search_via_browser('my query', max_results=8)

    assert calls['fetch_html'] == 1
    assert calls['search'] == 0, 'search() must not be called when fetch_html yields results'
    assert [r['url'] for r in out] == ['https://example.com/page', 'https://foo.org/article']
    assert all(r['source'] == 'DuckDuckGo (via browser)' for r in out)


def test_falls_back_to_search_when_fetch_html_none():
    """When fetch_html returns None (host can't supply HTML), the legacy
    host-search() path is used."""
    calls = {'fetch_html': 0, 'search': 0}

    class SearchOnlyProvider(BrowserProvider):
        def is_connected(self):
            return True

        def fetch_html(self, url, *, timeout=20):
            calls['fetch_html'] += 1
            return None

        def search(self, query, *, max_results=8):
            calls['search'] += 1
            return [{'title': 'Legacy', 'snippet': 's', 'url': 'https://legacy.example', 'source': 'host'}]

    register_browser_provider(SearchOnlyProvider())
    out = search_via_browser('q', max_results=8)

    assert calls['fetch_html'] == 1
    assert calls['search'] == 1
    assert out == [{'title': 'Legacy', 'snippet': 's', 'url': 'https://legacy.example', 'source': 'host'}]


def test_falls_back_to_search_when_fetch_html_unparseable():
    """fetch_html returns HTML but it yields zero parseable results → fall back."""
    calls = {'search': 0}

    class Provider(BrowserProvider):
        def is_connected(self):
            return True

        def fetch_html(self, url, *, timeout=20):
            return '<html><body>captcha wall, no results</body></html>'

        def search(self, query, *, max_results=8):
            calls['search'] += 1
            return [{'title': 'Fallback', 'snippet': '', 'url': 'https://fb.example', 'source': 'host'}]

    register_browser_provider(Provider())
    out = search_via_browser('q')
    assert calls['search'] == 1
    assert out[0]['url'] == 'https://fb.example'


def test_fetch_html_raising_falls_back_to_search():
    """A fetch_html that raises must not crash the pipeline — it falls back."""
    class Provider(BrowserProvider):
        def is_connected(self):
            return True

        def fetch_html(self, url, *, timeout=20):
            raise RuntimeError('extension channel died')

        def search(self, query, *, max_results=8):
            return [{'title': 'Safe', 'snippet': '', 'url': 'https://safe.example', 'source': 'host'}]

    register_browser_provider(Provider())
    out = search_via_browser('q')
    assert out[0]['url'] == 'https://safe.example'


# ══════════════════════════════════════════════════════════
#  3. Logging-deferral invariant (subprocess — import-time behaviour)
# ══════════════════════════════════════════════════════════

def _run_import_probe(preconfigure_root: bool) -> str:
    """Import tofu_search in a fresh interpreter and report the handler count.

    The package handler is attached at import time and modules are cached, so
    the only honest way to test import-time logging behaviour is a subprocess
    where we control the root-logger state BEFORE the import happens.
    """
    code = (
        "import logging\n"
        + ("logging.basicConfig(level=logging.INFO)\n" if preconfigure_root else "")
        + "import tofu_search\n"
        "pkg = logging.getLogger('tofu_search')\n"
        "print(len(pkg.handlers))\n"
    )
    out = subprocess.check_output([sys.executable, '-c', code], text=True)
    return out.strip()


def test_embedded_does_not_attach_package_handler():
    """When the host already configured the root logger, tofu_search must NOT
    attach its own handler — records propagate to the host's handlers."""
    assert _run_import_probe(preconfigure_root=True) == '0'


def test_standalone_attaches_one_package_handler():
    """With no host logging config, the standalone console handler IS attached
    so the library's diagnostics are visible out of the box."""
    assert _run_import_probe(preconfigure_root=False) == '1'
