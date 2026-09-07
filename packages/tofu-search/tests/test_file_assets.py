"""File-asset handling in the fetch pipeline.

Covers the two competencies that belong to tofu-search (so the host app
doesn't re-implement fetching):

  * Text-based file assets (SVG, JSON, source code, …) are returned as their
    raw source by ``fetch_page_content`` — NOT rejected by ``_should_fetch``
    and NOT run through HTML extraction.
  * ``fetch_url_bytes`` downloads BINARY assets, enforcing the same scheme /
    SSRF / size policy as the text pipeline.
  * ``looks_like_text_asset`` classifies a URL by extension.
"""

import pytest

import tofu_search.fetch.core as core
import tofu_search.fetch.utils as utils
from tofu_search import configure, fetch_page_content, fetch_url_bytes, looks_like_text_asset


class _FakeResp:
    def __init__(self, ct, encoding='utf-8'):
        self.headers = {'Content-Type': ct}
        self.encoding = encoding


# ── _should_fetch: binary media rejected, .svg allowed ──

def test_should_fetch_allows_svg(monkeypatch):
    configure(block_private_addresses=False)
    assert utils._should_fetch('https://cdn.example.com/icon.svg') is True


@pytest.mark.parametrize('ext', ['.png', '.jpg', '.gif', '.mp4', '.zip', '.exe'])
def test_should_fetch_blocks_binary_media(monkeypatch, ext):
    configure(block_private_addresses=False)
    assert utils._should_fetch(f'https://cdn.example.com/file{ext}') is False


# ── _is_text_asset_ct content-type classification ──

@pytest.mark.parametrize('ct', [
    'image/svg+xml', 'application/json', 'application/xml',
    'text/css', 'text/markdown', 'application/javascript',
    'application/x-yaml', 'text/x-python',
])
def test_text_asset_ct_true(ct):
    assert utils._is_text_asset_ct(ct) is True


@pytest.mark.parametrize('ct', [
    'text/html', 'text/html; charset=utf-8', 'text/plain',
    'application/pdf', 'image/png', 'application/octet-stream',
])
def test_text_asset_ct_false(ct):
    assert utils._is_text_asset_ct(ct) is False


# ── fetch_page_content: text asset returned verbatim ──

def test_fetch_page_content_returns_svg_source(monkeypatch):
    configure(block_private_addresses=False)
    svg = '<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0h24v24H0z"/></svg>' * 3
    monkeypatch.setattr(utils, '_host_is_safe', lambda h: True)
    monkeypatch.setattr(core, '_do_request',
                        lambda url, timeout, verify=True, legacy_ssl=False, deadline_ts=None:
                        (_FakeResp('image/svg+xml'), svg.encode('utf-8')))
    out = fetch_page_content('https://cdn.example.com/icon.svg')
    assert out is not None
    assert out.startswith('<svg')
    assert 'path' in out


def test_fetch_page_content_returns_json_source(monkeypatch):
    configure(block_private_addresses=False)
    body = '{"name": "tofu", "nested": {"a": [1, 2, 3]}}'
    monkeypatch.setattr(utils, '_host_is_safe', lambda h: True)
    monkeypatch.setattr(core, '_do_request',
                        lambda url, timeout, verify=True, legacy_ssl=False, deadline_ts=None:
                        (_FakeResp('application/json'), body.encode('utf-8')))
    out = fetch_page_content('https://api.example.com/data.json')
    assert out == body


# ── fetch_url_bytes ──

def test_fetch_url_bytes_returns_bytes_and_ct(monkeypatch):
    configure(block_private_addresses=False)
    raw = b'\x89PNG\r\n\x1a\n' + b'\x00' * 100
    monkeypatch.setattr(utils, '_host_is_safe', lambda h: True)
    monkeypatch.setattr(core, '_do_request',
                        lambda url, timeout, verify=True, legacy_ssl=False, deadline_ts=None:
                        (_FakeResp('image/png'), raw))
    got = fetch_url_bytes('https://cdn.example.com/logo.png')
    assert got is not None
    body, ct = got
    assert body == raw
    assert ct == 'image/png'


def test_fetch_url_bytes_rejects_non_http():
    assert fetch_url_bytes('file:///etc/passwd') is None
    assert fetch_url_bytes('ftp://example.com/x.zip') is None


def test_fetch_url_bytes_ssrf_guard(monkeypatch):
    configure(block_private_addresses=True)
    # Don't even reach _do_request — guard rejects internal host first.
    monkeypatch.setattr(core, '_do_request',
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError('should not fetch')))
    assert fetch_url_bytes('http://169.254.169.254/x') is None
    assert fetch_url_bytes('http://127.0.0.1/x.png') is None


def test_fetch_url_bytes_size_cap(monkeypatch):
    configure(block_private_addresses=False)
    monkeypatch.setattr(utils, '_host_is_safe', lambda h: True)
    monkeypatch.setattr(core, '_do_request',
                        lambda url, timeout, verify=True, legacy_ssl=False, deadline_ts=None:
                        (_FakeResp('application/zip'), b'X' * 5000))
    assert fetch_url_bytes('https://cdn.example.com/big.zip', max_bytes=1000) is None


def test_fetch_url_bytes_download_failure_returns_none(monkeypatch):
    configure(block_private_addresses=False)
    monkeypatch.setattr(utils, '_host_is_safe', lambda h: True)

    def boom(*a, **k):
        raise core._HttpError(404, 'https://cdn.example.com/missing.png')

    monkeypatch.setattr(core, '_do_request', boom)
    assert fetch_url_bytes('https://cdn.example.com/missing.png') is None


# ── looks_like_text_asset ──

@pytest.mark.parametrize('url,expected', [
    ('https://x/a.svg', True),
    ('https://x/a.json', True),
    ('https://x/script.py', True),
    ('https://x/style.css', True),
    ('https://x/a.png', False),
    ('https://x/a.zip', False),
    ('https://x/page', False),
    ('https://x/', False),
    ('https://x/article.html', False),
])
def test_looks_like_text_asset(url, expected):
    assert looks_like_text_asset(url) is expected
