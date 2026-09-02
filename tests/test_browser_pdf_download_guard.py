"""Guard: the browser-bridge provider must REFUSE binary/PDF URLs.

Background — real bug: when Browser Bridge mode is on, a server-side fetch
failure on a PDF (403/429/timeout/oversize) fell through to the browser
fallback, which opens the URL in a real Chrome tab. Navigating a tab to a PDF
makes Chrome's download manager save the file to the USER's machine (the
mysterious ``sfbookvNr.pdf`` downloads) AND returns no scrapable text, so the
server couldn't parse it anyway. PDFs/binaries must never use the extension's
tab-navigation text path. They use direct server HTTP or the separate bounded
browser-response → server-staging transfer.

These tests pin ``_ChatuiBrowserProvider.fetch_url`` / ``fetch_html``: for a
binary URL they must short-circuit to ``None`` WITHOUT ever calling the
underlying extension transport (``fetch_url_via_browser`` /
``send_browser_command``). The NC: remove the guard and the transport IS
reached for a PDF → these tests fail.
"""

import pytest

import lib.search_bridge as sb


@pytest.mark.unit
class TestBrowserUnrenderablePredicate:
    def test_pdf_and_binaries_are_unrenderable(self):
        for u in [
            'https://example.com/papers/sfbookv7r.pdf',
            'https://example.com/a.PDF',
            'https://example.com/a.pdf/',            # trailing slash tolerated
            'https://example.com/archive.zip',
            'https://cdn.example.com/video.mp4',
            'https://example.com/report.docx',
            'https://example.com/img.png',
            'https://example.com/font.woff2',
        ]:
            assert sb._is_browser_unrenderable(u), u

    def test_html_and_svg_and_text_are_renderable(self):
        for u in [
            'https://example.com/article',
            'https://example.com/page.html',
            'https://example.com/diagram.svg',       # SVG is text — allowed
            'https://example.com/data.json',
            'https://example.com/',
        ]:
            assert not sb._is_browser_unrenderable(u), u


@pytest.mark.unit
class TestProviderRefusesBinaryUrls:
    def test_fetch_url_pdf_short_circuits_without_transport(self, monkeypatch):
        called = {'transport': False}

        def _boom(*a, **k):
            called['transport'] = True
            raise AssertionError('extension transport must NOT be called for a PDF')

        import lib.browser.fetch as browser_fetch
        monkeypatch.setattr(browser_fetch, 'fetch_url_via_browser', _boom)

        prov = sb._ChatuiBrowserProvider()
        out = prov.fetch_url('https://example.com/papers/sfbookv7r.pdf')
        assert out is None
        assert called['transport'] is False

    def test_fetch_html_pdf_short_circuits_without_transport(self, monkeypatch):
        called = {'transport': False}

        def _boom(*a, **k):
            called['transport'] = True
            raise AssertionError('send_browser_command must NOT be called for a PDF')

        import lib.browser.queue as browser_queue
        monkeypatch.setattr(browser_queue, 'send_browser_command', _boom)
        monkeypatch.setattr(
            browser_queue,
            'is_extension_connected',
            lambda *a, **k: True,
        )

        prov = sb._ChatuiBrowserProvider()
        out = prov.fetch_html('https://example.com/papers/sfbookv7r.pdf')
        assert out is None
        assert called['transport'] is False

    def test_fetch_url_html_still_reaches_transport(self, monkeypatch):
        """Regression guard: a normal HTML URL must still go through."""
        seen = {}

        def _fake(url, **k):
            seen['url'] = url
            seen['client_id'] = k.get('client_id')
            seen['owner_user_id'] = k.get('owner_user_id')
            return 'extracted article text'

        import lib.browser.fetch as browser_fetch
        import lib.browser.queue as browser_queue
        monkeypatch.setattr(browser_fetch, 'fetch_url_via_browser', _fake)
        monkeypatch.setattr(
            browser_queue, 'is_extension_connected',
            lambda client_id, *, owner_user_id: (
                client_id == 'client-html' and owner_user_id == '41'),
        )

        prov = sb._ChatuiBrowserProvider(
            user_id='41', client_id='client-html', bound=True)
        out = prov.fetch_url('https://example.com/article')
        assert out == 'extracted article text'
        assert seen['url'] == 'https://example.com/article'
        assert seen['client_id'] == 'client-html'
        assert seen['owner_user_id'] == '41'
