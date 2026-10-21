"""Received React completion HTML obeys source order and visibility boundaries."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tofu_search.fetch import core, html_extract, html_streaming, utils
from tofu_search.fetch.html_extract import extract_html_text

pytestmark = pytest.mark.unit

URL = 'https://example.test/guide'
INTRO = 'Read this introduction before inspecting the completed release information.'
FACT = 'The observatory opened its eastern telescope to visitors on 17 May 1986.'
ENDING = 'The public archive retains the original observations and visitor records.'
LOADING = 'Loading the obsolete fallback description, please wait for the release.'


def boundary(number='0', prefix='', fallback=LOADING):
    return f'<!--$?--><template id="{prefix}B:{number}"></template><p>{fallback}</p><!--/$-->'


def segment(body, number='0', prefix=''):
    return f'<div hidden id="{prefix}S:{number}">{body}</div>'


def call(number='0', prefix=''):
    return f'$RC({json.dumps(prefix + "B:" + number)},{json.dumps(prefix + "S:" + number)})'


def document(body=None, script=None):
    return ('<html><body><main><h1>Observatory archive</h1><p>' + INTRO + '</p>'
            + boundary() + '<p>' + ENDING + '</p></main>'
            + segment(body if body is not None else '<p>' + FACT + '</p>')
            + '<script>' + (script if script is not None else call()) + '</script>'
            + '</body></html>')


@pytest.mark.parametrize('fallback', [False, True])
def test_completion_replaces_fallback_in_source_order_in_both_extractors(monkeypatch, fallback):
    if fallback:
        monkeypatch.setattr(html_extract.trafilatura, 'extract', lambda *_a, **_k: None)
    text = extract_html_text(document(), 0, URL)
    assert FACT in text and LOADING not in text
    assert text.index(INTRO) < text.index(FACT) < text.index(ENDING)
    assert text.count(FACT) == 1


def test_completed_fragment_keeps_links_code_tables_and_hidden_child_exclusion():
    body = ('<section id="observations"><h2>Recorded observations</h2><p>' + FACT + '</p>'
            '<p>Read the <a href="../records">archive records</a> for the exact procedure.</p>'
            '<pre>if count &lt; 3:\n    await next_item()\n\n    finish()</pre>'
            '<table><tr><th>Year</th><th>Visitors</th></tr>'
            '<tr><td>1986</td><td>42</td></tr></table>'
            '<div hidden>HIDDEN SENTINEL</div>'
            '<p>Read <span hidden>PRIVATE WORDS</span>the public instructions.</p></section>')
    scope = {}
    text = extract_html_text(document(body), 0, URL + '#observations', scope=scope)
    assert FACT in text and INTRO not in text and ENDING not in text
    assert scope['fragment'] == 'observations' and INTRO in scope['document_text']
    assert 'https://example.test/records' in text
    assert 'if count < 3:\n    await next_item()\n\n    finish()' in text
    assert '| 1986 | 42 |' in text
    assert 'HIDDEN SENTINEL' not in text and 'PRIVATE WORDS' not in text
    assert 'Read the public instructions.' in text


@pytest.mark.parametrize('script', [
    '', '/* ' + call() + ' */', '// ' + call(), json.dumps(call()),
    'const example = ' + json.dumps(call()) + ';',
    'if (false) ' + call(), 'false && ' + call(),
    'function later(){' + call() + '}', 'setTimeout(()=>{' + call() + '}, 100)',
    call() + ' + 1', call() + '.then(later)', 'other();' + call(),
    '$RC("B:0", "S:0", "failed")', '$RC(boundaryId, "S:0")',
    '$RC("B:0", "S:1")', '$RC("B:0", "otherS:0")',
])
def test_unknown_or_nonexecuting_script_does_not_reveal_hidden_text(script):
    text = extract_html_text(document(script=script), 0, URL)
    assert FACT not in text and LOADING in text


@pytest.mark.parametrize('attributes', [
    'type="application/json"', 'type="module"', 'src="/external.js"',
    'type="text/plain"',
])
def test_only_inline_classic_completion_scripts_are_interpreted(attributes):
    html = document().replace('<script>', '<script ' + attributes + '>')
    text = extract_html_text(html, 0, URL)
    assert FACT not in text and LOADING in text


@pytest.mark.parametrize('change', [
    lambda s: s.replace('<!--$?-->', '<!--ordinary comment-->'),
    lambda s: s.replace('<!--/$-->', ''),
    lambda s: s.replace('<!--/$-->', '<!--/&-->'),
    lambda s: s.replace('<template id="B:0"></template>', '<div id="B:0"></div>'),
    lambda s: s.replace('</template>', 'not an empty marker</template>'),
    lambda s: s.replace('id="S:0"', 'id="S:0" aria-hidden="true"'),
    lambda s: s.replace('</main>', '</main><template id="B:0"></template>'),
    lambda s: s.replace('</main>', '</main><div hidden id="S:0">Duplicate</div>'),
    lambda s: s.replace('</script>', ';' + call() + '</script>'),
    lambda s: s.replace('<script>', '<template><script>').replace('</script>', '</script></template>'),
    lambda s: s.replace('<script>', '<noscript><script>').replace('</script>', '</script></noscript>'),
])
def test_ambiguous_malformed_or_inert_completion_keeps_hidden_content_excluded(change):
    text = extract_html_text(change(document()), 0, URL)
    assert FACT not in text


def test_completion_before_receiving_the_segment_is_not_replayed_later():
    html = document().replace('<script>' + call() + '</script>', '')
    html = html.replace('<main>', '<script>' + call() + '</script><main>')
    assert FACT not in extract_html_text(html, 0, URL)


@pytest.mark.parametrize('attribute', ['hidden', 'aria-hidden="true"', 'style="display: none"'])
def test_completion_does_not_remove_destination_visibility_boundary(attribute):
    html = document().replace(boundary(), '<div ' + attribute + '>' + boundary() + '</div>')
    assert FACT not in extract_html_text(html, 0, URL)


def test_nested_fallback_comments_do_not_remove_visible_following_content():
    html = document().replace(LOADING, boundary('1', fallback='Nested stale loading state'))
    text = extract_html_text(html, 0, URL)
    assert FACT in text and ENDING in text and 'stale loading' not in text


@pytest.mark.parametrize('inside_first', [False, True])
def test_nested_completed_boundaries_follow_declared_order(inside_first):
    html = document('<h2>Final release details</h2>' + boundary('1'))
    html = html.replace('<script>', segment('<p>' + FACT + '</p>', '1') + '<script>')
    calls = [call('1'), call()] if inside_first else [call(), call('1')]
    html = html.replace('<script>' + call(), '<script>' + ';'.join(calls))
    text = extract_html_text(html, 0, URL)
    assert FACT in text and text.count(FACT) == 1 and LOADING not in text
    assert text.index(INTRO) < text.index('Final release details') < text.index(FACT)


def test_incomplete_outer_segment_does_not_expose_completed_inner_boundary():
    html = document(boundary('1'), call('1'))
    html = html.replace('<script>', segment('<p>' + FACT + '</p>', '1') + '<script>')
    assert FACT not in extract_html_text(html, 0, URL)


def test_completed_empty_segment_removes_only_its_fallback():
    text = extract_html_text(document(''), 0, URL)
    assert INTRO in text and ENDING in text and LOADING not in text


def test_distinct_prefixed_roots_keep_their_corresponding_sources():
    html = ('<html><body><main>' + boundary(prefix='north-') + boundary(prefix='south-')
            + '</main>' + segment('<p>' + FACT + '</p>', prefix='north-')
            + segment('<p>' + ENDING + '</p>', prefix='south-')
            + '<script>' + call(prefix='south-') + ';' + call(prefix='north-')
            + '</script></body></html>')
    text = extract_html_text(html, 0, URL)
    assert FACT in text and ENDING in text and LOADING not in text
    assert text.index(FACT) < text.index(ENDING)


def test_public_fetch_recovers_received_completion_without_browser_or_extra_request(monkeypatch):
    requests = []

    def get(url, *_args, **_kwargs):
        requests.append(url)
        return SimpleNamespace(headers={'Content-Type': 'text/html'}, encoding='utf-8'), document().encode()

    def no_browser(*_args, **_kwargs):
        pytest.fail('Received completed HTML needs no browser recovery')

    monkeypatch.setattr(core, '_do_request', get)
    monkeypatch.setattr(core, '_fetch_cache', utils._FetchCache())
    monkeypatch.setattr(core, '_try_browser_fetch', no_browser)
    monkeypatch.setattr(core, '_try_playwright_fallback', no_browser)
    text = core.fetch_page_content(URL, max_chars=20000)
    assert FACT in text and LOADING not in text
    assert core.fetch_page_content(URL, max_chars=40) == text[:40] + '\n[…truncated]'
    assert requests == [URL]


def test_queued_runtime_bootstrap_restores_the_received_html():
    fixture = Path(__file__).parent / 'fixtures' / 'react-fizz-queued-completion.js'
    script = fixture.read_text().split('*/', 1)[1].strip()
    text = extract_html_text(document(script=script), 0, URL)
    assert FACT in text and LOADING not in text
    assert text.index(INTRO) < text.index(FACT) < text.index(ENDING)


@pytest.mark.parametrize('script', [
    '$RC=function(a,b){', '$RC=function(a,b){const s="unterminated;};' + call(),
    '$RC=function(a,b){const s=`template`;};' + call(),
    '$RC=function(a,b){const r=/regex/;};' + call(),
    '$RC=function(a,b){/* unterminated};' + call(),
    '$RC=function(a,b){// unterminated};' + call(),
    '$RC=function(a,b){return "x";} ' + call(),
])
def test_unsupported_bootstrap_syntax_does_not_change_the_document(script):
    html = document(script=script)
    assert html_streaming.restore_completed_streams(html) is html


def test_ordinary_html_keeps_the_fast_path_without_an_additional_parser(monkeypatch):
    def unexpected_parser():
        pytest.fail('Ordinary HTML should not be parsed by the stream normalizer')

    monkeypatch.setattr(html_streaming, '_get_bs4', unexpected_parser)
    html = '<html><body><p>' + FACT + '</p></body></html>'
    assert html_streaming.restore_completed_streams(html) is html


def test_normalization_failure_keeps_the_ordinary_extraction_path(monkeypatch):
    html = '<html><body><main><p>' + FACT + '</p></main></body></html>'
    expected = extract_html_text(html, 0, URL)

    def unavailable(_html):
        raise ValueError('Unusable stream structure')

    monkeypatch.setattr(html_extract, 'restore_completed_streams', unavailable)
    assert extract_html_text(html, 0, URL) == expected


def test_many_received_segments_preserve_document_order_and_keep_no_transport_wrappers():
    count = 200
    ids = [format(n, 'x') for n in range(count)]
    html = ('<html><body><main>' + ''.join(boundary(n) for n in ids) + '</main>'
            + ''.join(segment('<p>Recorded item ' + n + '</p>', n) for n in reversed(ids))
            + '<script>' + ';'.join(call(n) for n in reversed(ids)) + '</script></body></html>')
    restored = html_streaming.restore_completed_streams(html)
    soup = utils._get_bs4()(restored, 'html.parser')
    assert [p.get_text() for p in soup.main.find_all('p')] == ['Recorded item ' + n for n in ids]
    assert not soup.find_all(hidden=True) and not soup.find_all('template')
