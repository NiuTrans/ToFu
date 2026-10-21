"""Rendering decisions use received main content, not navigation or head length."""

from types import SimpleNamespace

import pytest

from tofu_search.fetch import core, utils
from tofu_search.fetch.html_extract import extract_html_text

pytestmark = pytest.mark.unit

FACT = 'The archive opens at nine each morning.'
LONG_FACT = 'The eastern observatory opened its public archive on 17 May 1986. ' * 4
NAVIGATION = '<nav>' + 'Site navigation and promotional links. ' * 30 + '</nav>'


def document(main='', *, root='__next', head='', notice=''):
    return ('<html><head>' + head + '</head><body><div id="' + root + '">'
            + NAVIGATION + '<main>' + main + '</main>' + notice + '</div></body></html>')


@pytest.mark.parametrize('root', ['root', 'app', '__next', '__nuxt', 'vue-app'])
def test_empty_main_is_detected_after_a_large_head_even_with_long_navigation(root):
    html = document(root=root, head='<style>/*' + 'x' * 60000 + '*/</style>')
    assert utils._looks_like_spa_shell(html, 'Site navigation and headings. ' * 30)


@pytest.mark.parametrize('body', ['<p>' + FACT + '</p>', '<p>' + LONG_FACT + '</p>'])
def test_real_short_or_long_primary_content_does_not_need_rendering(body):
    html = document(body, head='<style>/*' + 'x' * 60000 + '*/</style>',
                    notice='<noscript>Enable JavaScript to use interactive controls.</noscript>')
    assert not utils._looks_like_spa_shell(html, extract_html_text(html, 0))


def test_script_text_that_mentions_a_root_is_not_an_application_mount():
    html = ('<html><body><p>' + FACT + '</p><script>const example = '
            "'<div id=\"root\"></div>';</script></body></html>")
    assert not utils._looks_like_spa_shell(html, FACT)


def test_inert_template_main_cannot_mask_an_empty_application_main():
    html = document().replace('<body>', '<body><template><main>' + LONG_FACT + '</main></template>')
    assert utils._looks_like_spa_shell(html, LONG_FACT)


def test_plain_short_document_with_large_stylesheet_is_readable():
    html = document('<p>' + FACT + '</p>', root='ordinary', head='<style>' + 'x' * 8000 + '</style>')
    assert not utils._looks_like_spa_shell(html, FACT)


def test_received_completed_stream_is_not_classified_from_its_old_empty_main():
    html = document('<!--$?--><template id="B:0"></template><!--/$-->')
    html = html.replace('</body>', '<div hidden id="S:0"><p>' + FACT + '</p></div>'
                        '<script>$RC("B:0","S:0")</script></body>')
    assert not utils._looks_like_spa_shell(html, extract_html_text(html, 0))


@pytest.mark.parametrize('recovery', ['playwright', 'browser', None])
def test_empty_primary_content_uses_existing_recovery_and_is_not_cached_as_success(monkeypatch, recovery):
    calls = []
    html = document(head='<style>' + 'x' * 10000 + '</style>')

    def request(*_args, **_kwargs):
        calls.append('http')
        return SimpleNamespace(headers={'Content-Type': 'text/html'}, encoding='utf-8'), html.encode()

    def render(*_args, **_kwargs):
        calls.append('playwright')
        return LONG_FACT if recovery == 'playwright' else None

    def browser(*_args, **_kwargs):
        calls.append('browser')
        return LONG_FACT if recovery == 'browser' else None

    monkeypatch.setattr(core, '_do_request', request)
    monkeypatch.setattr(core, '_fetch_cache', utils._FetchCache())
    monkeypatch.setattr(core, '_try_playwright_fallback', render)
    monkeypatch.setattr(core, '_try_browser_fetch', browser)
    diag = {}
    text = core.fetch_page_content('https://example.test/render', max_chars=20000, diag=diag)
    assert text == (LONG_FACT if recovery else None)
    assert calls == ['http', 'playwright'] + ([] if recovery == 'playwright' else ['browser'])
    if recovery is None:
        assert diag['reason'] == 'spa_shell'
        assert core._fetch_cache.get('https://example.test/render') is None
