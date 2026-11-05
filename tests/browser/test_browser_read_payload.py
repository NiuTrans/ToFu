"""Contract tests for browser page/fetch HTML-first payload extraction.

The extension now ships page HTML as the PRIMARY payload and only includes
``text`` (innerText) as a fallback when the HTML is too small for the server
to extract from. These tests pin that the two server-side consumers behave
correctly against the new payload shapes:

  * ``lib.browser.handlers._handle_read_page(mode='text')``
  * ``lib.browser.fetch.fetch_url_via_browser``

They monkeypatch the wire call (``send_browser_command``) so no real Chrome
extension is needed.
"""

import pytest

import lib.browser.fetch as bfetch
from lib.browser.handlers import _handle_read_page
from lib.browser.tool_runtime import BrowserToolRuntime

_REAL_HTML = (
    '<html><head><title>Doc</title></head><body>'
    '<article><h1>Real Heading</h1>'
    + '<p>This is a substantial paragraph of readable article content that '
      'trafilatura/BS4 should extract cleanly. </p>' * 12
    + '</article></body></html>'
)


@pytest.fixture(autouse=True)
def _file_aware_extension(monkeypatch):
    monkeypatch.setattr(
        'lib.browser.protocol.require_capabilities',
        lambda client_id, required: {'client_id': client_id},
    )


@pytest.mark.unit
class TestReadPageHtmlPrimary:
    def test_extracts_from_html_when_text_absent(self):
        """Common path: HTML present, NO innerText shipped → extract from HTML."""
        payload = {
            'html': _REAL_HTML,
            'htmlTruncated': False,
            'meta': {},
            'title': 'Doc',
            'url': 'https://example.com/a',
            # note: no 'text' key — the optimization omits it on the hot path
        }
        runtime = BrowserToolRuntime(
            owner_user_id='41', client_id='test-browser',
            sender=lambda *a, **k: (payload, None),
        )
        out = _handle_read_page(
            {'tabId': 1, 'mode': 'text'}, runtime)
        assert 'Real Heading' in out
        assert 'readable article content' in out
        assert 'rendered_dom' in out and 'Coverage:' in out

    def test_falls_back_to_text_when_html_too_small(self):
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
        runtime = BrowserToolRuntime(
            owner_user_id='41', client_id='test-browser',
            sender=lambda *a, **k: (payload, None),
        )
        out = _handle_read_page(
            {'tabId': 2, 'mode': 'text'}, runtime)
        assert 'Fallback visible text' in out
        assert 'rendered_dom' in out


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
        seen = {}

        def fake_connected(client_id, *, owner_user_id):
            return client_id == 'test-browser' and owner_user_id == '41'

        def fake_send(*args, client_id=None, owner_user_id=None, **kwargs):
            seen['route'] = (client_id, owner_user_id)
            return payload, None

        monkeypatch.setattr(bfetch, 'is_extension_connected', fake_connected)
        monkeypatch.setattr(bfetch, 'send_browser_command', fake_send)
        out = bfetch.fetch_url_via_browser(
            'https://example.com/a',
            client_id='test-browser',
            owner_user_id='41',
        )
        assert out and 'readable article content' in out
        assert seen['route'] == ('test-browser', '41')

    def test_falls_back_to_innertext_when_html_small(self, monkeypatch):
        payload = {
            'html': '<html><body></body></html>',
            'htmlTruncated': False,
            'meta': {},
            'text': 'x' * 120,  # > fetch.py's 50-char fallback gate
            'title': 'Shell',
            'url': 'https://example.com/shell',
        }
        seen = {}

        def fake_connected(client_id, *, owner_user_id):
            return client_id == 'test-browser' and owner_user_id == '41'

        def fake_send(*args, client_id=None, owner_user_id=None, **kwargs):
            seen['route'] = (client_id, owner_user_id)
            return payload, None

        monkeypatch.setattr(bfetch, 'is_extension_connected', fake_connected)
        monkeypatch.setattr(bfetch, 'send_browser_command', fake_send)
        out = bfetch.fetch_url_via_browser(
            'https://example.com/shell',
            client_id='test-browser',
            owner_user_id='41',
        )
        assert 'x' * 120 in out and 'Coverage:' in out
        assert seen['route'] == ('test-browser', '41')


@pytest.mark.unit
class TestReadPageUrlReceipt:
    """A successful text read names its accepted page on the timeline row.

    The Sources footer cites that row's URL — the page the model read —
    never the links that page contains.
    """

    def _runtime(self, payload, metadata):
        return BrowserToolRuntime(
            owner_user_id='41', client_id='test-browser',
            sender=lambda *a, **k: (payload, None), display_metadata=metadata)

    def test_successful_read_reports_the_accepted_page_url(self):
        metadata = {}
        payload = {'html': _REAL_HTML, 'htmlTruncated': False, 'meta': {},
                   'title': 'Doc', 'url': 'https://example.com/a'}
        out = _handle_read_page({'tabId': 1, 'mode': 'text'}, self._runtime(payload, metadata))
        assert 'Real Heading' in out
        assert metadata == {'url': 'https://example.com/a'}

    def test_read_without_content_reports_no_page(self):
        metadata = {}
        payload = {'html': '', 'htmlTruncated': False, 'meta': {},
                   'title': 'Empty', 'url': 'https://example.com/empty'}
        _handle_read_page({'tabId': 1, 'mode': 'text'}, self._runtime(payload, metadata))
        assert metadata == {}

    @pytest.mark.parametrize('content,reported,row_url', [
        ('Browser content: ok\nDoc\nhttps://example.com/a\nCoverage: {}\nbody',
         'https://example.com/a', 'https://example.com/a'),
        ('Error reading tab 1: tab closed', None, ''),
    ])
    def test_task_row_carries_only_a_successful_reads_url(self, content, reported, row_url):
        from unittest.mock import patch
        from lib.tasks_pkg.handlers.browser import _handle_browser_tool
        captured = []

        def execute(name, fn_args, **kwargs):
            assert name == 'browser_read_page'
            if reported:
                kwargs['display_metadata']['url'] = reported
            return content

        def settle(task, rn, row, results, **kwargs):
            captured.append(results)

        with patch('lib.browser.dispatch.execute_browser_tool', side_effect=execute), \
             patch('lib.tasks_pkg.handlers.browser._finalize_tool_round', side_effect=settle):
            result = _handle_browser_tool({'_userId': 41}, None, 'browser_read_page', 'call',
                                         {'tabId': 1}, 1, {}, {'browserClientId': 'test-browser'},
                                         '', False)
        assert result[2] is bool(reported)
        assert captured[0][0]['url'] == row_url
