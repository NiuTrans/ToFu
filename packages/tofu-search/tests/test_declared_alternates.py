"""A received document that yields no content can name its own text representation.

When the HTML path cannot produce content (application shell, empty extract,
bot/JS wall), the page may still declare ``<link rel="alternate"
type="text/markdown" href=...>`` or an HTTP ``Link: <...>; rel="alternate"``.
The pipeline admits the first same-origin text alternate once, through the
anonymous gate, page budget and document transport, before Playwright or the
host browser. Its response is never searched for further alternates, and a
readable page never requests it.

All offline: every network, Playwright and browser hop is monkeypatched.
"""

from types import SimpleNamespace

import pytest
import requests

from tofu_search import configure
from tofu_search.fetch import core, utils

pytestmark = pytest.mark.unit

ORIGIN = 'https://docs.example'
PAGE = ORIGIN + '/release-notes/new-released'
ALTERNATE = PAGE + '.md'
MARKDOWN = ('# New Released\n\n**2026-08-26**\n\n'
            'GLM-5.3-Flash adds native visual understanding for interface review.')
DECLARATION = '<link rel="alternate" type="text/markdown" href="/release-notes/new-released.md">'


def shell(head='', body=''):
    """An application root with an empty primary region: rendering is required."""
    return ('<!doctype html><html lang="en"><head><title>New Released</title>' + head
            + '</head><body><div id="__next"><main></main>' + body + '</div></body></html>')


def response(body, content_type='text/html; charset=utf-8', *, url=PAGE, link=None):
    headers = {'Content-Type': content_type}
    if link:
        headers['Link'] = link
    return SimpleNamespace(headers=headers, encoding='utf-8', url=url), body.encode('utf-8')


@pytest.fixture
def site(monkeypatch):
    """Serve ``site.pages[url]``; record requests, gate admissions and fallbacks."""
    state = SimpleNamespace(pages={}, requests=[], admitted=[], fallbacks=[])

    def request(url, timeout, **kwargs):
        state.requests.append(url)
        page = state.pages[url]
        if isinstance(page, Exception):
            raise page
        return page

    def admit(url):
        state.admitted.append(url)
        return True

    def render(url, max_chars, timeout):
        state.fallbacks.append('playwright')
        return None

    def browser(url, max_chars, reason='unknown'):
        state.fallbacks.append('browser:' + reason)
        return None

    monkeypatch.setattr(core, '_get_reader', lambda url: None)
    monkeypatch.setattr(core, 'get_auth_source_provider', lambda: None)
    monkeypatch.setattr(core, '_should_fetch', admit)
    monkeypatch.setattr(core, '_is_known_spa', lambda url: False)
    monkeypatch.setattr(core, '_fetch_cache', utils._FetchCache())
    monkeypatch.setattr(core, '_do_request', request)
    monkeypatch.setattr(core, '_try_playwright_fallback', render)
    monkeypatch.setattr(core, '_try_browser_fetch', browser)
    return state


def test_shell_returns_markdown_resolved_against_the_final_url_and_caches_it(site):
    requested = ORIGIN + '/latest'  # redirects into /release-notes/
    site.pages[requested] = response(shell(
        '<link rel="alternate" type="text/markdown" href="new-released.md">'))
    site.pages[ALTERNATE] = response(MARKDOWN, 'text/markdown; charset=utf-8', url=ALTERNATE)
    diag = {'reason': 'stale', 'strategy': 'stale'}

    assert core.fetch_page_content(requested, max_chars=50000, diag=diag) == MARKDOWN
    assert site.requests == [requested, ALTERNATE]
    assert site.admitted == [requested, ALTERNATE], 'the alternate passes the same anonymous gate'
    assert site.fallbacks == [], 'the declared text precedes Playwright and the browser'
    assert diag == {'strategy': 'declared_alternate', 'alternate_url': ALTERNATE}
    assert core._fetch_cache.get(requested) == MARKDOWN
    assert core.fetch_page_content(requested, max_chars=50000) == MARKDOWN
    assert site.requests == [requested, ALTERNATE], 'the original URL is served from cache'


@pytest.mark.parametrize(('page', 'reason', 'fallbacks'), [
    pytest.param(shell(DECLARATION), 'spa_shell', ['playwright', 'browser:spa_shell'],
                 id='spa_shell'),
    pytest.param('<html><head>' + DECLARATION + '</head><body><p>Moved.</p></body></html>',
                 'empty_extract', [], id='empty_extract'),
    pytest.param('<html><head>' + DECLARATION + '<script src="/app.js"></script></head>'
                 '<body><div id="root"></div></body></html>',
                 None, ['playwright', 'browser:login_wall'], id='bot_wall_html'),
    pytest.param('<html><head>' + DECLARATION + '</head><body><p>Please enable JavaScript '
                 'to view this documentation site.</p></body></html>',
                 'bot_wall', ['playwright', 'browser:login_wall'], id='bot_wall_text'),
])
def test_every_unreadable_html_branch_admits_the_alternate_first(site, page, reason, fallbacks):
    undeclared = ORIGIN + '/undeclared'
    site.pages[undeclared] = response(page.replace(DECLARATION, ''), url=undeclared)
    witness = {}
    assert core.fetch_page_content(undeclared, max_chars=50000, diag=witness) is None
    assert (witness.get('reason'), site.fallbacks) == (reason, fallbacks), 'branch witness'

    site.fallbacks.clear()
    site.pages[PAGE] = response(page)
    site.pages[ALTERNATE] = response(MARKDOWN, 'text/markdown', url=ALTERNATE)
    assert core.fetch_page_content(PAGE, max_chars=50000) == MARKDOWN
    assert site.requests == [undeclared, PAGE, ALTERNATE]
    assert site.fallbacks == []


@pytest.mark.parametrize(('head', 'body', 'link', 'target', 'served'), [
    pytest.param('<link rel="alternate" type="text/markdown" '
                 'href="https://docs.example/release-notes/new-released.md">',
                 '', None, ALTERNATE, 'text/markdown', id='absolute_same_origin'),
    pytest.param('<base href="/archive/"><link rel="alternate" type="text/plain" href="notes.txt">',
                 '', None, ORIGIN + '/archive/notes.txt', 'text/plain', id='document_base'),
    pytest.param('<link rel="Alternate" type="Text/X-Markdown; charset=utf-8" '
                 'href=" /release-notes/new-released.md ">',
                 '', None, ALTERNATE, 'text/x-markdown', id='case_and_parameters'),
    pytest.param('<link rel="alternate" hreflang="EN" type="text/markdown" '
                 'href="/release-notes/new-released.md">',
                 '', None, ALTERNATE, 'text/markdown', id='same_language'),
    pytest.param('', DECLARATION, None, ALTERNATE, 'text/markdown', id='streamed_body_metadata'),
    pytest.param('', '', '</llms.txt>; rel="llms-txt", '
                 '</release-notes/new-released.md>; rel="alternate"; type="text/markdown"',
                 ALTERNATE, 'text/markdown', id='link_header'),
    pytest.param('<link rel="alternate" type="application/rss+xml" href="/feed.xml">'
                 '<link rel="alternate" type="text/markdown" href="https://other.example/x.md">',
                 '', '</release-notes/new-released.md>; rel=alternate; type=text/markdown',
                 ALTERNATE, 'text/markdown', id='first_eligible_declaration'),
])
def test_declared_text_alternate_forms(site, head, body, link, target, served):
    site.pages[PAGE] = response(shell(head, body), link=link)
    site.pages[target] = response(MARKDOWN, served, url=target)
    diag = {}

    assert core.fetch_page_content(PAGE, max_chars=50000, diag=diag) == MARKDOWN
    assert site.requests == [PAGE, target]
    assert diag['alternate_url'] == target


@pytest.mark.parametrize(('head', 'link'), [
    pytest.param('<link rel="alternate" type="text/markdown" '
                 'href="https://other.example/new-released.md">', None, id='cross_origin'),
    pytest.param('<link rel="alternate" type="text/markdown" '
                 'href="//other.example/new-released.md">', None, id='protocol_relative'),
    pytest.param('<link rel="alternate" type="text/markdown" '
                 'href="http://docs.example/new-released.md">', None, id='other_scheme'),
    pytest.param('<link rel="alternate" type="text/markdown" '
                 'href="https://docs.example:8443/new-released.md">', None, id='other_port'),
    pytest.param('<link rel="alternate" type="text/markdown" '
                 'href="https://docs.example:99999/new-released.md">', None, id='invalid_port'),
    pytest.param('<base href="https://other.example/">'
                 '<link rel="alternate" type="text/markdown" href="new-released.md">',
                 None, id='cross_origin_base'),
    pytest.param('<link rel="alternate" type="application/xml" href="/sitemap.xml">',
                 None, id='not_text'),
    pytest.param('<link rel="alternate" hreflang="fr" type="text/markdown" '
                 'href="/fr/new-released.md">', None, id='translation'),
    pytest.param('<template>' + DECLARATION + '</template>', None, id='inert_template'),
    pytest.param('', '</llms.txt>; rel="llms-txt"', id='site_index_header'),
    pytest.param('', '<https://other.example/new-released.md>; rel="alternate"; '
                 'type="text/markdown"', id='cross_origin_header'),
    pytest.param('', '</release-notes/new-released.md>; rel="alternate"; '
                 'type="text/markdown"; anchor="/other"', id='header_about_another_resource'),
])
def test_ineligible_declarations_are_never_requested(site, head, link):
    site.pages[PAGE] = response(shell(head), link=link)
    diag = {}

    assert core.fetch_page_content(PAGE, max_chars=50000, diag=diag) is None
    assert site.requests == [PAGE]
    assert site.fallbacks == ['playwright', 'browser:spa_shell']
    assert diag['reason'] == 'spa_shell'


@pytest.mark.parametrize('alternate', [
    pytest.param(core._HttpError(404, ALTERNATE), id='http_404'),
    pytest.param(requests.Timeout('slow alternate'), id='timeout'),
    pytest.param(response('<html><body><p>Not found</p></body></html>', url=ALTERNATE),
                 id='html_answer'),
    pytest.param(response('# Moved', 'text/markdown', url=ALTERNATE), id='too_short'),
    pytest.param(response('Checking your browser before accessing docs.example. '
                          'This process is automatic.', 'text/plain', url=ALTERNATE),
                 id='bot_text'),
    pytest.param(response(MARKDOWN, 'application/octet-stream', url=ALTERNATE), id='not_text'),
])
def test_failed_alternate_leaves_fallbacks_and_diagnostics_unchanged(site, alternate):
    undeclared = ORIGIN + '/undeclared'
    site.pages[undeclared] = response(shell(), url=undeclared)
    expected = {}
    assert core.fetch_page_content(undeclared, max_chars=50000, diag=expected) is None
    expected_fallbacks = list(site.fallbacks)
    site.fallbacks.clear()

    site.pages[PAGE] = response(shell(DECLARATION))
    site.pages[ALTERNATE] = alternate
    diag = {}
    assert core.fetch_page_content(PAGE, max_chars=50000, diag=diag) is None
    assert site.requests == [undeclared, PAGE, ALTERNATE]
    assert site.fallbacks == expected_fallbacks == ['playwright', 'browser:spa_shell']
    assert diag == expected
    assert core._fetch_cache.get(PAGE) is None


def test_alternate_response_never_leads_to_another_alternate(site):
    second = ORIGIN + '/second.md'
    site.pages[PAGE] = response(shell(DECLARATION))
    site.pages[ALTERNATE] = response(
        shell('<link rel="alternate" type="text/markdown" href="/second.md">'), url=ALTERNATE,
        link='</second.md>; rel="alternate"; type="text/markdown"')
    site.pages[second] = response(MARKDOWN, 'text/markdown', url=second)

    assert core.fetch_page_content(PAGE, max_chars=50000) is None
    assert site.requests == [PAGE, ALTERNATE]
    assert site.fallbacks == ['playwright', 'browser:spa_shell']


def test_readable_page_keeps_its_html_result_without_requesting_the_alternate(site):
    fact = 'GLM-5.3-Flash adds native visual understanding for interface review.'
    site.pages[PAGE] = response(
        '<html lang="en"><head><title>Release notes</title>' + DECLARATION + '</head><body>'
        '<main><h1>Release notes</h1><p>' + (fact + ' ') * 5 + '</p></main></body></html>')
    diag = {}

    text = core.fetch_page_content(PAGE, max_chars=50000, diag=diag)
    assert fact in text
    assert site.requests == [PAGE]
    assert site.fallbacks == []
    assert 'strategy' not in diag


def test_refused_alternate_is_not_requested(site, monkeypatch):
    def admit(url):
        site.admitted.append(url)
        return url != ALTERNATE

    monkeypatch.setattr(core, '_should_fetch', admit)
    site.pages[PAGE] = response(shell(DECLARATION))

    assert core.fetch_page_content(PAGE, max_chars=50000) is None
    assert site.admitted == [PAGE, ALTERNATE]
    assert site.requests == [PAGE]
    assert site.fallbacks == ['playwright', 'browser:spa_shell']


def test_expired_page_budget_admits_no_alternate(site, monkeypatch):
    expired = []
    serve = core._do_request

    def request(url, timeout, **kwargs):
        answer = serve(url, timeout, **kwargs)
        expired.append(url)
        return answer

    monkeypatch.setattr(core, 'page_budget_expired', lambda: bool(expired))
    monkeypatch.setattr(core, '_do_request', request)
    site.pages[PAGE] = response(shell(DECLARATION))
    diag = {}

    assert core.fetch_page_content(PAGE, max_chars=50000, deadline_secs=5, diag=diag) is None
    assert site.requests == [PAGE]
    assert site.fallbacks == [], 'the deadline-aware fallbacks are skipped as before'
    assert diag['reason'] == 'spa_shell'


def test_alternate_reuses_the_document_transport(site, monkeypatch):
    configure(allow_insecure_ssl_fallback=True)
    transports = []

    def request(url, timeout, **kwargs):
        transports.append((url, kwargs))
        if url == PAGE and kwargs.get('verify', True):
            raise requests.exceptions.SSLError('certificate verify failed')
        return site.pages[url]

    monkeypatch.setattr(core, '_do_request', request)
    site.pages[PAGE] = response(shell(DECLARATION))
    site.pages[ALTERNATE] = response(MARKDOWN, 'text/markdown', url=ALTERNATE)

    assert core.fetch_page_content(PAGE, max_chars=50000) == MARKDOWN
    assert transports == [(PAGE, {'verify': True}), (PAGE, {'verify': False}),
                          (ALTERNATE, {'verify': False})]


def test_alternate_follows_preview_and_direct_source_bounds(site):
    site.pages[PAGE] = response(shell(DECLARATION))
    site.pages[ALTERNATE] = response(MARKDOWN, 'text/markdown', url=ALTERNATE)

    assert core.fetch_page_content(PAGE, max_chars=20) == MARKDOWN[:20] + '\n[…truncated]'
    assert core._fetch_cache.get(PAGE) == MARKDOWN
    assert core.fetch_page_content(PAGE, max_chars=0) == MARKDOWN
    assert site.requests == [PAGE, ALTERNATE, PAGE, ALTERNATE], 'direct reads bypass the preview cache'
