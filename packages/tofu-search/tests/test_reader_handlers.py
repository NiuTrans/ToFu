"""Tests for the site-reader tier (tofu_search.fetch.readers) and its wiring
into fetch_page_content.

All offline: the Twitter syndication endpoint is monkeypatched via
``readers.http_get`` and the anonymous HTTP path via ``core._do_request`` — no
network. Covers:
  * syndication token generation (exact vectors from Vercel's JS algorithm),
  * URL → reader routing (status URL matches, non-status x.com does not),
  * tweet-result JSON parsing (valid / reply / quote / tombstone / empty),
  * a reader HIT short-circuits fetch_page_content,
  * a skip-domain status URL is READ (bypasses the block) while a non-status
    x.com URL stays blocked,
  * an auth-source-connected skip-domain bypasses the block (ordering fix).
"""

import pytest

import tofu_search.config as _config
from tofu_search.fetch import core, readers


class FakeResp:
    def __init__(self, status_code=200, json_data=None, raise_json=False):
        self.status_code = status_code
        self._json = json_data
        self._raise_json = raise_json

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        if self._raise_json:
            raise ValueError('bad json')
        return self._json


# ── Fixtures ──────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clear_fetch_cache():
    """The fetch cache is a module singleton — clear it around each test so a
    cached reader result from one test can't satisfy the next."""
    from tofu_search.fetch.utils import _fetch_cache
    _fetch_cache._data.clear()
    yield
    _fetch_cache._data.clear()


# ── Token generation (exact vectors from yt-dlp PR #12107) ──

@pytest.mark.parametrize('tweet_id,expected', [
    ('1874097816571961839', '4jjngwkifa'),
    ('1674700676612386816', '42586mwa3uv'),
    ('1877747914073620506', '4jv4aahw36n'),
    ('1876710769913450647', '4jruzjz5lux'),
    ('1346554693649113090', '39ibqxei7mo'),
])
def test_syndication_token_vectors(tweet_id, expected):
    assert readers._syndication_token(tweet_id) == expected


def test_syndication_token_has_no_zeros_or_dots():
    tok = readers._syndication_token('1683920951807971329')
    assert '0' not in tok and '.' not in tok and tok


# ── URL → tweet id routing ──

@pytest.mark.parametrize('url,expected', [
    ('https://x.com/edent/status/719484841172054016', '719484841172054016'),
    ('https://twitter.com/jack/status/20', '20'),
    ('https://www.x.com/foo/status/12345', '12345'),
    ('https://mobile.twitter.com/foo/status/98765', '98765'),
    ('https://x.com/i/web/status/555', '555'),
    ('https://x.com/edent/status/719484841172054016?s=20&t=abc', '719484841172054016'),
    ('https://x.com/edent/status/123/photo/1', '123'),
])
def test_extract_tweet_id_matches(url, expected):
    assert readers.extract_tweet_id(url) == expected


@pytest.mark.parametrize('url', [
    'https://x.com/',
    'https://x.com/elonmusk',
    'https://x.com/search?q=python',
    'https://x.com/home',
    'https://example.com/foo/status/123',   # right shape, wrong host
    'https://youtube.com/watch?v=abc',
    '',
])
def test_extract_tweet_id_non_status_returns_none(url):
    assert readers.extract_tweet_id(url) is None


def test_get_reader_routes_status_url_to_twitter():
    reader = readers.get_reader('https://x.com/edent/status/719484841172054016')
    assert isinstance(reader, readers.TwitterReader)


def test_get_reader_non_status_returns_none():
    assert readers.get_reader('https://x.com/elonmusk') is None
    assert readers.get_reader('https://example.com/article') is None


# ── tweet-result JSON parsing ──

def test_parse_valid_tweet():
    data = {
        '__typename': 'Tweet',
        'text': 'Hello world',
        'created_at': '2016-04-11T11:18:48.000Z',
        'user': {'name': 'Terence Eden', 'screen_name': 'edent'},
    }
    out = readers.parse_tweet_result(data)
    assert 'Hello world' in out
    assert 'Terence Eden (@edent)' in out
    assert '2016-04-11' in out


def test_parse_tweet_screen_name_only():
    data = {'text': 'hi', 'user': {'screen_name': 'jack'}}
    out = readers.parse_tweet_result(data)
    assert '@jack' in out and 'hi' in out


def test_parse_reply_inlines_parent():
    data = {
        'text': 'my reply', 'user': {'screen_name': 'me'},
        'parent': {'text': 'original post', 'user': {'screen_name': 'them'}},
    }
    out = readers.parse_tweet_result(data)
    assert 'my reply' in out
    assert 'In reply to' in out
    assert 'original post' in out


def test_parse_quote_inlines_quoted():
    data = {
        'text': 'check this', 'user': {'screen_name': 'me'},
        'quoted_tweet': {'text': 'quoted thing', 'user': {'screen_name': 'src'}},
    }
    out = readers.parse_tweet_result(data)
    assert 'check this' in out
    assert 'Quoting' in out
    assert 'quoted thing' in out


def test_parse_tombstone_returns_none():
    data = {'__typename': 'TweetTombstone',
            'tombstone': {'text': {'text': 'This Post was deleted'}}}
    assert readers.parse_tweet_result(data) is None


def test_parse_empty_returns_none():
    assert readers.parse_tweet_result({}) is None
    assert readers.parse_tweet_result({'foo': 'bar'}) is None
    assert readers.parse_tweet_result(None) is None


# ── TwitterReader.read via mocked http_get ──

def test_reader_read_hit(monkeypatch):
    captured = {}

    def fake_get(url, **kw):
        captured['url'] = url
        captured['params'] = kw.get('params')
        return FakeResp(200, {
            'text': 'A tweet body', 'created_at': '2020-01-01T00:00:00.000Z',
            'user': {'name': 'Jane', 'screen_name': 'jane'},
        })

    monkeypatch.setattr(readers, 'http_get', fake_get)
    out = readers.TwitterReader().read('https://x.com/jane/status/999')
    assert out is not None
    assert 'A tweet body' in out and '@jane' in out
    # Reused the syndication endpoint + correct id/token params.
    assert captured['url'] == readers._SYNDICATION_URL
    assert captured['params']['id'] == '999'
    assert captured['params']['token']


def test_reader_read_http_error_returns_none(monkeypatch):
    monkeypatch.setattr(readers, 'http_get', lambda url, **kw: FakeResp(404))
    assert readers.TwitterReader().read('https://x.com/j/status/1') is None


def test_reader_read_tombstone_returns_none(monkeypatch):
    monkeypatch.setattr(readers, 'http_get',
                        lambda url, **kw: FakeResp(200, {'__typename': 'TweetTombstone'}))
    assert readers.TwitterReader().read('https://x.com/j/status/1') is None


def test_reader_read_request_exception_returns_none(monkeypatch):
    def boom(url, **kw):
        raise ConnectionError('down')
    monkeypatch.setattr(readers, 'http_get', boom)
    assert readers.TwitterReader().read('https://x.com/j/status/1') is None


def test_reader_read_truncates(monkeypatch):
    long_text = 'x' * 5000
    monkeypatch.setattr(readers, 'http_get',
                        lambda url, **kw: FakeResp(200, {
                            'text': long_text, 'user': {'screen_name': 'a'}}))
    out = readers.TwitterReader().read('https://x.com/a/status/1', max_chars=100)
    assert out.endswith('[…truncated]')
    assert len(out) < 200


# ── Integration: skip-domain bypass for a status URL ──

def test_fetch_page_content_reads_skipped_status_url(monkeypatch):
    """x.com is in skip_domains, yet a *status* URL must be READ via the reader
    tier (which runs before the skip gate)."""
    def fake_get(url, **kw):
        return FakeResp(200, {
            'text': 'Tweet via reader', 'user': {'screen_name': 'edent'}})

    monkeypatch.setattr(readers, 'http_get', fake_get)
    # Guarantee x.com is blocked so the bypass is what we're proving.
    _config.configure(skip_domains={'x.com', 'twitter.com'})

    out = core.fetch_page_content('https://x.com/edent/status/719484841172054016')
    assert out is not None
    assert 'Tweet via reader' in out


def test_fetch_page_content_non_status_xcom_still_blocked(monkeypatch):
    """A non-status x.com URL has no reader match → the skip block still bites
    and no network request is made."""
    def fail_request(*a, **kw):
        raise AssertionError('should not fetch a blocked non-status x.com URL')

    monkeypatch.setattr(core, '_do_request', fail_request)
    monkeypatch.setattr(readers, 'http_get',
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError('reader must not fire for non-status URL')))
    _config.configure(skip_domains={'x.com', 'twitter.com'})

    assert core.fetch_page_content('https://x.com/elonmusk') is None


# ── Integration: auth-source connected domain bypasses the skip gate ──

def test_auth_source_bypasses_skip_domain(monkeypatch):
    """A skip-domain with connected auth cookies must be fetched via the auth
    path — proving the auth-source match now runs BEFORE _should_fetch."""

    class FakeAuthProvider:
        def match_source(self, url):
            return {'domain': 'x.com', 'cookies': [{'name': 'a', 'value': 'b'}]}

        def get_source(self, domain):
            return None

    calls = {}

    def fake_auth_fetch(url, source, max_chars, timeout):
        calls['url'] = url
        return 'Authenticated page body that is long enough to be real content.'

    monkeypatch.setattr(core, 'get_auth_source_provider', lambda: FakeAuthProvider())
    monkeypatch.setattr(core, '_try_authenticated_fetch', fake_auth_fetch)
    # No reader match for a bare profile URL, so this exercises the auth path.
    _config.configure(skip_domains={'x.com', 'twitter.com'})

    out = core.fetch_page_content('https://x.com/someprofile')
    assert out is not None
    assert 'Authenticated page body' in out
    assert calls['url'] == 'https://x.com/someprofile'


def test_no_auth_no_reader_skip_domain_blocked(monkeypatch):
    """Control: without a reader match OR auth connection, a skip-domain URL is
    blocked (proves the bypasses above are load-bearing, not always-open)."""
    monkeypatch.setattr(core, 'get_auth_source_provider', lambda: None)
    monkeypatch.setattr(core, '_do_request',
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError('should not fetch blocked domain')))
    _config.configure(skip_domains={'x.com', 'twitter.com'})
    assert core.fetch_page_content('https://x.com/someprofile') is None
