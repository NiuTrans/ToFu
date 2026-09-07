"""tests/test_browser_scrape_provider.py — _ChatuiBrowserProvider.scrape contract.

tofu-search 0.7.0's XHS engine is browser-first: it calls
``BrowserProvider.scrape(url, wait_selector=…, extractor_js=…, scrolls=…)``
to extract STRUCTURED data (search cards) from pages rendered inside the
user's real, logged-in Chrome. chatui fulfils that seam by composing the
EXISTING bridge commands — no extension change:

    create_tab (background) → wait_for_element → verify final tab URL
    → scroll_page ×N → execute_js → close_tab (always, even on failure)

Contract pinned here:

  * the exact command sequence and the params that matter (background tab,
    selector, extractor code, scroll direction);
  * ``{__error: …}`` from the page extractor → None (path failed → fallback);
  * ANY failure → None, and close_tab ALWAYS fires once a tab was opened
    (no tab leaks on the user's machine);
  * binary URLs / extension offline → None with ZERO commands issued.

All offline: lib.browser's two entry points are mocked.
"""

from unittest import mock

import pytest

pytestmark = pytest.mark.unit

CARDS = [{'title': 'note 0', 'url': 'https://www.xiaohongshu.com/explore/a'},
         {'title': 'note 1', 'url': 'https://www.xiaohongshu.com/explore/b'}]


def _make_provider():
    from lib.search_bridge import _ChatuiBrowserProvider
    # Registered providers are inert templates. tofu-search binds one request's
    # user/browser before entering worker threads; exercise that production
    # shape here so a test can never normalize a global-client fallback.
    return _ChatuiBrowserProvider(
        user_id='41', client_id='test-browser', profile='Test', bound=True)


def _fake_bridge(monkeypatch, script):
    """Install fake lib.browser entry points.

    ``script``: {command_type: result_or_(result, error)} — the fake pops
    commands in order and asserts nothing unexpected arrives.
    Returns the recorded [(type, params)] call list.
    """
    calls = []
    opened = {'id': None, 'url': ''}

    def fake_connected(client_id, *, owner_user_id):
        return client_id == 'test-browser' and owner_user_id == '41'

    def fake_send(
            cmd_type, params=None, timeout=30, client_id=None,
            owner_user_id=None):
        assert (client_id, owner_user_id) == ('test-browser', '41')
        calls.append((cmd_type, dict(params or {})))
        if cmd_type == 'list_tabs' and cmd_type not in script:
            return ([{'id': opened['id'], 'url': opened['url'],
                      'active': False}], None)
        outcome = script.get(cmd_type, (None, None))
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, tuple):
            return outcome
        if cmd_type == 'create_tab' and isinstance(outcome, dict):
            opened['id'] = outcome.get('id')
            opened['url'] = str((params or {}).get('url') or '')
        return outcome, None

    monkeypatch.setattr('lib.browser.queue.is_extension_connected', fake_connected)
    monkeypatch.setattr('lib.browser.queue.send_browser_command', fake_send)
    return calls


def test_happy_path_command_sequence(monkeypatch):
    calls = _fake_bridge(monkeypatch, {
        'create_tab': {'id': 77},
        'wait_for_element': {'found': True},
        'scroll_page': {'scrolled': True},
        'execute_js': CARDS,
        'close_tab': {'closed': [77]},
    })
    # No real sleeping during the test.
    monkeypatch.setattr('lib.search_bridge.time.sleep', lambda s: None)

    out = _make_provider().scrape(
        'https://www.xiaohongshu.com/search_result?keyword=x',
        wait_selector='section.note-item', extractor_js='(()=>[])()', scrolls=2)

    assert out == CARDS
    seq = [c[0] for c in calls]
    assert seq == ['create_tab', 'wait_for_element', 'list_tabs',
                   'scroll_page', 'scroll_page',
                   'execute_js', 'close_tab'], seq
    assert calls[0][1]['active'] is False, 'scrape must never steal the foreground tab'
    assert calls[1][1]['selector'] == 'section.note-item'
    assert calls[1][1]['tabId'] == 77
    assert calls[5][1]['code'] == '(()=>[])()'
    assert calls[6][1]['tabId'] == 77, 'the SAME background tab must be closed'


def test_scrolls_zero_skips_scroll_commands(monkeypatch):
    calls = _fake_bridge(monkeypatch, {
        'create_tab': {'id': 1},
        'wait_for_element': {'found': True},
        'execute_js': CARDS,
        'close_tab': {'closed': [1]},
    })
    out = _make_provider().scrape('https://x.example/', wait_selector='a',
                                  extractor_js='[]', scrolls=0)
    assert out == CARDS
    assert [c[0] for c in calls] == ['create_tab', 'wait_for_element', 'list_tabs',
                                     'execute_js', 'close_tab']


def test_extractor_page_error_returns_none_and_closes_tab(monkeypatch):
    calls = _fake_bridge(monkeypatch, {
        'create_tab': {'id': 5},
        'wait_for_element': {'found': True},
        'execute_js': {'__error': True, 'message': 'CSP blocked eval'},
        'close_tab': {'closed': [5]},
    })
    out = _make_provider().scrape('https://x.example/', wait_selector='a',
                                  extractor_js='[]')
    assert out is None
    assert [c[0] for c in calls][-1] == 'close_tab', 'tab must not leak on page errors'


def test_transport_error_still_closes_tab(monkeypatch):
    calls = _fake_bridge(monkeypatch, {
        'create_tab': {'id': 6},
        'wait_for_element': (None, 'timeout waiting for result'),
        'execute_js': (None, 'extension disconnected'),
        'close_tab': {'closed': [6]},
    })
    out = _make_provider().scrape('https://x.example/', wait_selector='a',
                                  extractor_js='[]')
    assert out is None
    assert [c[0] for c in calls][-1] == 'close_tab'


def test_create_tab_failure_stops_immediately(monkeypatch):
    calls = _fake_bridge(monkeypatch, {
        'create_tab': (None, 'user closed the browser'),
    })
    out = _make_provider().scrape('https://x.example/', wait_selector='a',
                                  extractor_js='[]')
    assert out is None
    assert [c[0] for c in calls] == ['create_tab'], (
        'no tab → no further commands, and no close_tab either')


def test_extension_offline_returns_none_without_commands(monkeypatch):
    calls = []

    def fake_disconnected(client_id, *, owner_user_id):
        assert (client_id, owner_user_id) == ('test-browser', '41')
        return False

    monkeypatch.setattr('lib.browser.queue.is_extension_connected', fake_disconnected)
    monkeypatch.setattr('lib.browser.queue.send_browser_command',
                        lambda *a, **k: calls.append(a) or (None, None))
    out = _make_provider().scrape('https://x.example/', wait_selector='a',
                                  extractor_js='[]')
    assert out is None
    assert calls == []


def test_binary_url_never_reaches_the_browser(monkeypatch):
    calls = _fake_bridge(monkeypatch, {})
    out = _make_provider().scrape('https://x.example/paper.pdf',
                                  wait_selector='a', extractor_js='[]')
    assert out is None
    assert calls == [], 'PDFs download to the USER machine — never open tabs for them'


def test_wait_not_found_still_extracts(monkeypatch):
    """A slow page may render the data without the exact selector — try anyway."""
    calls = _fake_bridge(monkeypatch, {
        'create_tab': {'id': 9},
        'wait_for_element': {'found': False, 'error': 'not found within 5000ms'},
        'execute_js': CARDS,
        'close_tab': {'closed': [9]},
    })
    out = _make_provider().scrape('https://x.example/', wait_selector='a.missing',
                                  extractor_js='[]')
    assert out == CARDS
    assert [c[0] for c in calls][-2] == 'execute_js'
