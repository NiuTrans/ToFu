"""Contract tests for the read_tab / fetch_url payload optimization (#5).

The extension now ships page HTML as the PRIMARY payload and only includes
``text`` (innerText) as a fallback when the HTML is too small for the server
to extract from. These tests pin that the two server-side consumers behave
correctly against the new payload shapes:

  * ``lib.browser.handlers._handle_read_tab``
  * ``lib.browser.fetch.fetch_url_via_browser``

They monkeypatch the wire call (``send_browser_command``) so no real Chrome
extension is needed.
"""

import pytest

import lib.browser.fetch as bfetch
import lib.browser.handlers as bhandlers

_REAL_HTML = (
    '<html><head><title>Doc</title></head><body>'
    '<article><h1>Real Heading</h1>'
    + '<p>This is a substantial paragraph of readable article content that '
      'trafilatura/BS4 should extract cleanly. </p>' * 12
    + '</article></body></html>'
)


@pytest.mark.unit
class TestReadTabHtmlPrimary:
    def test_extracts_from_html_when_text_absent(self, monkeypatch):
        """Common path: HTML present, NO innerText shipped → extract from HTML."""
        payload = {
            'html': _REAL_HTML,
            'htmlTruncated': False,
            'meta': {},
            'title': 'Doc',
            'url': 'https://example.com/a',
            # note: no 'text' key — the optimization omits it on the hot path
        }
        monkeypatch.setattr(bhandlers, 'send_browser_command',
                            lambda *a, **k: (payload, None))
        out = bhandlers._handle_read_tab({'tabId': 1})
        assert 'Real Heading' in out
        assert 'readable article content' in out
        assert 'html→extract' in out  # proves the HTML pipeline ran

    def test_falls_back_to_text_when_html_too_small(self, monkeypatch):
        """Shell page: tiny HTML + innerText fallback present → use the text."""
        payload = {
            'html': '<html><body></body></html>',  # < server's 200-char gate
            'htmlTruncated': False,
            'meta': {},
            'text': 'Fallback visible text from a JS-only shell page.',
            'textLength': 48,
            'truncated': False,
            'title': 'Shell',
            'url': 'https://example.com/shell',
        }
        monkeypatch.setattr(bhandlers, 'send_browser_command',
                            lambda *a, **k: (payload, None))
        out = bhandlers._handle_read_tab({'tabId': 2})
        assert 'Fallback visible text' in out
        assert 'innerText' in out


@pytest.mark.unit
class TestFetchUrlHtmlPrimary:
    def test_extracts_from_html_when_text_absent(self, monkeypatch):
        payload = {
            'html': _REAL_HTML,
            'htmlTruncated': False,
            'meta': {},
            'title': 'Doc',
            'url': 'https://example.com/a',
        }
        monkeypatch.setattr(bfetch, 'is_extension_connected', lambda *a, **k: True)
        monkeypatch.setattr(bfetch, '_get_active_client', lambda: None)
        monkeypatch.setattr(bfetch, 'send_browser_command',
                            lambda *a, **k: (payload, None))
        out = bfetch.fetch_url_via_browser('https://example.com/a')
        assert out and 'readable article content' in out

    def test_falls_back_to_innertext_when_html_small(self, monkeypatch):
        payload = {
            'html': '<html><body></body></html>',
            'htmlTruncated': False,
            'meta': {},
            'text': 'x' * 120,  # > fetch.py's 50-char fallback gate
            'title': 'Shell',
            'url': 'https://example.com/shell',
        }
        monkeypatch.setattr(bfetch, 'is_extension_connected', lambda *a, **k: True)
        monkeypatch.setattr(bfetch, '_get_active_client', lambda: None)
        monkeypatch.setattr(bfetch, 'send_browser_command',
                            lambda *a, **k: (payload, None))
        out = bfetch.fetch_url_via_browser('https://example.com/shell')
        assert out == 'x' * 120
