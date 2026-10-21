"""Requested HTML fragments retain their structural evidence and source bounds."""

from types import SimpleNamespace

import pytest

from tofu_search import fetch_page_content
from tofu_search.fetch import core, html_extract, utils
from tofu_search.fetch.html_extract import extract_html_text

pytestmark = pytest.mark.unit
URL = 'https://docs.example.test/reference'
INTRO = 'Unrelated introduction describes installation and deployment of the service.'
ENDING = 'Unrelated teardown describes stopping the service and removing its configuration.'
DETAIL = 'The controller waits until every pending operation has completed before returning.'


def document(body):
    return f'<html><body><main><h1>Reference</h1><p>{INTRO}</p>{body}<h2>Teardown</h2><p>{ENDING}</p></main></body></html>'


def extract(body, fragment):
    return extract_html_text(document(body), 0, URL + '#' + fragment)


def test_heading_scope_includes_subsections_code_tables_and_exact_links():
    code = 'if pending:\n    wait_all()'
    text = extract('<h2 id="lifecycle">Lifecycle</h2>'
        f'<p>{DETAIL}</p><h3>Example</h3><pre>{code}</pre>'
        '<table><tr><th>Limit</th><td>20 operations</td></tr></table>'
        '<p>Read the <a href="/rule?q=a%2Fb#notes">rule notes</a> before shutdown.</p>',
        'lifecycle')
    assert DETAIL in text and code in text
    assert '20 operations' in text and '/rule?q=a%2Fb#notes' in text
    assert INTRO not in text and ENDING not in text


def test_heading_wrapper_and_nested_identifier_keep_following_body():
    text = extract('<div class="heading"><h2><span id="workers">Workers</span></h2></div>'
        f'<p>{DETAIL}</p><div class="heading"><h2>Other topic</h2></div>'
        '<p>Different lifecycle with unrelated shutdown rules.</p>', 'workers')
    assert DETAIL in text and 'Workers' in text
    assert 'Different lifecycle' not in text and INTRO not in text


def test_named_anchor_before_heading_selects_that_heading():
    text = extract(f'<a name="legacy"></a><h2>Legacy workers</h2><p>{DETAIL}</p>', 'legacy')
    assert DETAIL in text and 'Legacy workers' in text
    assert INTRO not in text and ENDING not in text


def test_definition_fragment_preserves_term_aliases_and_their_definition():
    text = extract('<dl><dt>UnrelatedError</dt><dd>An earlier exception.</dd>'
        '<dt id="ClosedError">ClosedError</dt><dt>ClosedAlias</dt>'
        f'<dd><p>{DETAIL}</p><p>Added in version 4.2.</p></dd>'
        '<dt>LaterError</dt><dd>A different exception with different conditions.</dd></dl>',
        'ClosedError')
    assert 'ClosedError' in text and 'ClosedAlias' in text
    assert DETAIL in text and 'Added in version 4.2.' in text
    assert 'UnrelatedError' not in text and 'LaterError' not in text
    assert INTRO not in text


def test_summary_anchor_keeps_documentation_outside_signature_section():
    text = extract('<details><summary><section id="method.reserve">'
        '<h4>reserve(additional)</h4></section></summary>'
        f'<div class="docblock"><p>{DETAIL}</p><pre>reserve(20)</pre></div></details>'
        '<details><summary>remove()</summary><p>A different operation.</p></details>',
        'method.reserve')
    assert DETAIL in text and 'reserve(20)' in text
    assert 'A different operation' not in text and INTRO not in text


@pytest.mark.parametrize('identifier,fragment', [
    ('生命周期', '%E7%94%9F%E5%91%BD%E5%91%A8%E6%9C%9F'),
    ('%73', '%2573'),
    ('x.y[0]', 'x.y%5B0%5D'),
])
def test_fragment_decodes_once_and_matches_identifiers_literally(identifier, fragment):
    text = extract(f'<section id="{identifier}"><h2>Lifecycle</h2><p>{DETAIL}</p></section>', fragment)
    assert DETAIL in text and INTRO not in text and ENDING not in text


@pytest.mark.parametrize('body,fragment', [
    (f'<h2 id="workers">Workers</h2><p>{DETAIL}</p>', 'missing'),
    (f'<h2 id="workers">Workers</h2><p>{DETAIL}</p>', 'WORKERS'),
    (f'<h2 id="workers">Workers</h2><p>{DETAIL}</p>', '%FF'),
    (f'<h2 id="workers">Workers</h2><p>{DETAIL}</p>', ':~:text=controller'),
    (f'<h2 id="same">First</h2><p>{DETAIL}</p><h2 id="same">Second</h2>', 'same'),
    (f'<section hidden id="hidden">Private hidden text</section><p>{DETAIL}</p>', 'hidden'),
    (f'<nav id="navigation">Site navigation</nav><p>{DETAIL}</p>', 'navigation'),
])
def test_unresolved_or_ineligible_fragment_preserves_whole_page(body, fragment):
    assert extract(body, fragment) == extract_html_text(document(body), 0, URL)


def test_scope_metadata_does_not_claim_a_previous_fragment_for_a_new_page():
    scope = {'custom': 'caller value', 'fragment': 'previous'}
    html = document(f'<section id="workers"><h2>Workers</h2><p>{DETAIL}</p></section>')
    extract_html_text(html, 0, URL + '#workers', scope=scope)
    assert scope['custom'] == 'caller value' and scope['fragment'] == 'workers'
    extract_html_text(html, 0, URL + '#missing', scope=scope)
    assert scope == {'custom': 'caller value'}


@pytest.mark.parametrize('selection', ['<html><body></body></html>', 'error'])
def test_failed_scope_extraction_retains_full_document(monkeypatch, selection):
    html = document(f'<h2 id="workers">Workers</h2><p>{DETAIL}</p>')
    expected = extract_html_text(html, 0, URL)

    def select(*_args):
        if selection == 'error':
            raise ValueError('Unusable document structure')
        return selection

    monkeypatch.setattr(html_extract, 'select_fragment_html', select)
    scope = {}
    assert extract_html_text(html, 0, URL + '#workers', scope=scope) == expected
    assert not scope


def test_public_fetch_accepts_short_scope_without_browser_and_keeps_cache_identity(monkeypatch):
    html = document('<section id="timeout"><h2>Timeout</h2>'
                    '<p>Requests wait at most 20 seconds.</p></section>')
    html = html.replace('</body>', '<script>' + '/* application bundle */' * 150
                        + '</script></body>')
    requests = []

    def get(url, *_args, **_kwargs):
        requests.append(url)
        return SimpleNamespace(headers={'Content-Type': 'text/html'}, encoding='utf-8'), html.encode()

    def no_browser(*_args, **_kwargs):
        pytest.fail('A readable fragment does not need browser recovery')

    monkeypatch.setattr(core, '_do_request', get)
    monkeypatch.setattr(core, '_fetch_cache', utils._FetchCache())
    monkeypatch.setattr(core, '_try_browser_fetch', no_browser)
    monkeypatch.setattr(core, '_try_playwright_fallback', no_browser)
    focused = fetch_page_content(URL + '#timeout', max_chars=20000)
    assert 'Requests wait at most 20 seconds.' in focused
    assert INTRO not in focused and ENDING not in focused
    assert fetch_page_content(URL + '#timeout', max_chars=20000) == focused
    assert fetch_page_content(URL + '#timeout', max_chars=20) == focused[:20] + '\n[…truncated]'
    full = fetch_page_content(URL, max_chars=20000)
    assert INTRO in full and ENDING in full
    assert requests == [URL + '#timeout', URL]


@pytest.mark.parametrize('identifier', ['app', 'pending'])
def test_matching_fragment_does_not_turn_loading_shell_into_source_evidence(monkeypatch, identifier):
    html = ('<html><body><div id="' + identifier + '">Loading...</div>'
            '<script>' + '/* client application */' * 150 + '</script></body></html>')
    rendered = 'Rendered controller instructions describe how to configure and stop pending work.'
    recovery = []

    def render(url, max_chars, timeout):
        recovery.append(url)
        return rendered

    monkeypatch.setattr(core, '_do_request', lambda *_a, **_kw: (
        SimpleNamespace(headers={'Content-Type': 'text/html'}, encoding='utf-8'), html.encode()))
    monkeypatch.setattr(core, '_fetch_cache', utils._FetchCache())
    monkeypatch.setattr(core, '_try_playwright_fallback', render)
    assert fetch_page_content(URL + '#' + identifier) == rendered
    assert recovery == [URL + '#' + identifier]
