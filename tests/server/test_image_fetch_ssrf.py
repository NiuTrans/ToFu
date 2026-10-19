"""SSRF + complete source tests for routes.upload._safe_image_fetch (B10).

Covers:
  * Scheme allow-list (http/https only).
  * IP allow-list: loopback / RFC1918 / link-local / multicast / reserved
    are all rejected by default.
  * TOFU_IMAGE_FETCH_ALLOW_HOSTS escapes the IP check for trusted hosts.
  * Sources beyond the former byte cap are retained in owned files.
  * Complete history encoding closes files on success and failure.
  * The wrapper raises _ImageFetchError on policy violations (caught by
    callers, treated as a soft skip in _resolve_history_images).

Run:  pytest tests/server/test_image_fetch_ssrf.py -v
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


# Imports go inside fixtures/tests because routes.upload pulls in Flask at
# import time — the flask_client fixture in conftest.py handles that for
# us, but for the helper-only tests we import lazily.


def _import_helpers():
    from routes.upload import _ImageFetchError, _is_blocked_ip, _safe_image_fetch
    return _ImageFetchError, _is_blocked_ip, _safe_image_fetch


# ═══════════════════════════════════════════════════════════
#  IP classification
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestBlockedIPs:
    """_is_blocked_ip returns True for any address that must not be
    reachable through a user-supplied URL."""

    @pytest.mark.parametrize('ip', [
        '127.0.0.1', '127.42.42.42',                # loopback
        '10.0.0.1', '172.16.0.1', '192.168.1.1',    # RFC1918
        '169.254.169.254',                          # link-local (cloud metadata)
        '224.0.0.1',                                # multicast
        '0.0.0.0',                                  # unspecified
        '::1', 'fe80::1', 'fc00::1',                # IPv6 loopback / link-local / ULA
        'not-an-ip',                                # unparseable → deny
    ])
    def test_blocked(self, ip):
        _err, _is_blocked, _fetch = _import_helpers()
        assert _is_blocked(ip) is True

    @pytest.mark.parametrize('ip', [
        '8.8.8.8', '1.1.1.1', '93.184.216.34',      # public IPv4
        '2606:4700:4700::1111',                     # public IPv6 (Cloudflare)
    ])
    def test_allowed(self, ip):
        _err, _is_blocked, _fetch = _import_helpers()
        assert _is_blocked(ip) is False


# ═══════════════════════════════════════════════════════════
#  _safe_image_fetch policy checks (no real network)
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestSchemeAndHostGuards:

    def test_rejects_non_http_scheme(self):
        Err, _is_blocked, fetch = _import_helpers()
        with pytest.raises(Err, match='scheme'):
            fetch('file:///etc/passwd')
        with pytest.raises(Err, match='scheme'):
            fetch('ftp://example.com/x.png')

    def test_rejects_missing_hostname(self):
        Err, _is_blocked, fetch = _import_helpers()
        with pytest.raises(Err):
            fetch('http:///x.png')

    def test_rejects_loopback_hostname(self, monkeypatch):
        Err, _is_blocked, fetch = _import_helpers()
        monkeypatch.delenv('TOFU_IMAGE_FETCH_ALLOW_HOSTS', raising=False)
        # localhost resolves to 127.0.0.1 — gets caught by the IP filter.
        with pytest.raises(Err, match='blocked IP'):
            fetch('http://localhost/cat.png')

    def test_rejects_ip_literal_loopback(self, monkeypatch):
        Err, _is_blocked, fetch = _import_helpers()
        monkeypatch.delenv('TOFU_IMAGE_FETCH_ALLOW_HOSTS', raising=False)
        with pytest.raises(Err, match='blocked IP'):
            fetch('http://127.0.0.1/img')

    def test_rejects_ip_literal_rfc1918(self, monkeypatch):
        Err, _is_blocked, fetch = _import_helpers()
        monkeypatch.delenv('TOFU_IMAGE_FETCH_ALLOW_HOSTS', raising=False)
        with pytest.raises(Err, match='blocked IP'):
            fetch('http://10.0.0.5:8080/x.png')

    def test_rejects_cloud_metadata(self, monkeypatch):
        Err, _is_blocked, fetch = _import_helpers()
        monkeypatch.delenv('TOFU_IMAGE_FETCH_ALLOW_HOSTS', raising=False)
        with pytest.raises(Err, match='blocked IP'):
            fetch('http://169.254.169.254/latest/meta-data/')

    def test_allow_list_bypasses_ip_check(self, monkeypatch):
        """When a hostname is in TOFU_IMAGE_FETCH_ALLOW_HOSTS, we skip the
        IP filter entirely — we still attempt the HTTP fetch (mocked here)."""
        Err, _is_blocked, fetch = _import_helpers()
        monkeypatch.setenv('TOFU_IMAGE_FETCH_ALLOW_HOSTS', 'internal.example.com')

        source = __import__('io').BytesIO(b'PNGDATA000')
        with patch('lib.safe_fetch.fetch_public_file',
                   return_value=(source, 'image/png',
                                 'http://internal.example.com/cat.png')) as guarded:
            body, ct = fetch('http://internal.example.com/cat.png')
        with body:
            assert body.read() == b'PNGDATA000'
        assert ct == 'image/png'
        assert guarded.call_args.kwargs['allow_hosts_env'] == \
            'TOFU_IMAGE_FETCH_ALLOW_HOSTS'


# ═══════════════════════════════════════════════════════════
#  Size cap
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestCompleteSource:

    @pytest.mark.parametrize('declared', [True, False])
    def test_large_source_is_complete_and_owned(self, monkeypatch, declared):
        import hashlib
        Err, _is_blocked, fetch = _import_helpers()
        monkeypatch.setenv('TOFU_IMAGE_FETCH_ALLOW_HOSTS', 'cdn.example.com')
        block = bytes(range(256)) * 256
        count = 177
        expected = hashlib.sha256(block * count).digest()
        with patch('lib.safe_fetch._session_for') as session_for:
            session = session_for.return_value
            session.max_redirects = 30
            response = session.get.return_value
            response.headers = {'Content-Type': 'image/png'}
            if declared:
                response.headers['Content-Length'] = str(len(block) * count)
            response.iter_content = lambda chunk_size: (block for _ in range(count))
            response.url = 'https://cdn.example.com/large.png'
            source, content_type = fetch(response.url)
            assert response.close.call_count == 1
            assert session.max_redirects == 30
            with source:
                assert source.fileno() >= 0
                assert hashlib.sha256(source.read()).digest() == expected
            assert source.closed
            assert content_type == 'image/png'
        print(f'remote_image_demand: {len(block) * count} bytes, declared={declared}')

    def test_history_encodes_complete_source_and_closes_it(self, monkeypatch):
        import base64
        import tempfile
        import routes.upload as upload
        raw = bytes(range(256)) * 44000 + b'end'
        source = tempfile.TemporaryFile(mode='w+b')
        source.write(raw)
        monkeypatch.setattr(upload, '_safe_image_fetch', lambda url: (source, 'image/webp'))
        result = upload._resolve_history_images([{
            'prompt': 'edit', 'text': '', 'image_url': 'https://example.com/image.webp'}])
        assert base64.b64decode(result[0]['image_b64'], validate=True) == raw
        assert result[0]['mime_type'] == 'image/webp'
        assert source.closed

    def test_history_encoding_failure_closes_source(self, monkeypatch):
        import tempfile
        import routes.upload as upload
        source = tempfile.TemporaryFile(mode='w+b')
        monkeypatch.setattr(upload, '_safe_image_fetch', lambda url: (source, 'image/png'))
        def fail(source):
            raise OSError('injected source read failure')
        monkeypatch.setattr(upload, '_encode_image_source', fail)
        assert upload._resolve_history_images([{'image_url': 'https://example.com/x'}]) == []
        assert source.closed


# ═══════════════════════════════════════════════════════════
#  _resolve_history_images integration: rejects ssrf URLs softly
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestResolveHistoryImagesSSRF:
    """When a history entry points at a blocked URL, _resolve_history_images
    must drop that entry (returning the rest), NOT crash and NOT fetch."""

    def test_blocked_url_dropped(self, monkeypatch):
        from routes.upload import _resolve_history_images
        monkeypatch.delenv('TOFU_IMAGE_FETCH_ALLOW_HOSTS', raising=False)

        with patch('requests.get') as mock_get:
            result = _resolve_history_images([
                {'prompt': 'p', 'text': '',
                 'image_url': 'http://169.254.169.254/latest/meta-data/'},
            ])
        # Blocked → empty list, and we never hit the network.
        assert result == []
        mock_get.assert_not_called()

    def test_pre_resolved_b64_passthrough(self, monkeypatch):
        """Entries that already have image_b64 don't trigger any fetch —
        even if the SSRF guard would otherwise reject them."""
        from routes.upload import _resolve_history_images
        with patch('requests.get') as mock_get:
            result = _resolve_history_images([
                {'prompt': 'p', 'text': '', 'image_b64': 'BASE64HERE'},
            ])
        assert len(result) == 1
        assert result[0]['image_b64'] == 'BASE64HERE'
        mock_get.assert_not_called()
