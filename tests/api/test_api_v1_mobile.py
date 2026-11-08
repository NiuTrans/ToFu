"""HTTP wiring contract for the local Android APK distribution surface."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from urllib.parse import parse_qsl, urlsplit

import pytest

pytestmark = pytest.mark.api


def _app(*, authenticated: bool = True):
    """The mobile routes as production mounts them at the v4 release major.

    Only the ``/api/v4`` projection is mounted, and every ``/api/v1`` path
    meets the 426 upgrade fence before authentication.
    """
    from lib.api_keys import local_admin_context
    from lib.api_v4 import (
        attach_v4_envelope,
        configure_api_version_policy,
        copy_blueprint_to_v4,
        legacy_api_upgrade_before_request,
    )
    from lib.generated_api_v4_constants import API_MAJOR
    from quart import Quart, g
    from routes.api_v1 import mobile as route

    app = Quart(__name__)
    app.config['ACTIVE_API_MAJOR'] = API_MAJOR
    configure_api_version_policy(app)
    app.before_request(legacy_api_upgrade_before_request)

    if authenticated:
        @app.before_request
        async def _auth():
            g.auth_ctx = local_admin_context()

    app.register_blueprint(attach_v4_envelope(copy_blueprint_to_v4(
        route.api_v1_mobile_bp, name='api_v1_mobile_v4')))
    return app


def _point_at(monkeypatch: pytest.MonkeyPatch, path) -> None:
    monkeypatch.setenv('TOFU_ANDROID_APK_PATH', str(path))


def _data(body: dict) -> dict:
    return body['data'] if 'data' in body else body


_PAGE_HARNESS = r"""
const vm = require('vm');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const page = new URL(input.pageUrl);
const elements = {};
const requests = [];
vm.runInNewContext(input.script, {
  location: {origin: page.origin, pathname: page.pathname, href: page.href},
  document: {getElementById: (id) => (elements[id] = elements[id] || {})},
  fetch: async (url) => {
    requests.push(String(url));
    return {ok: true, status: 200, json: async () => input.response};
  },
});
setTimeout(() => process.stdout.write(JSON.stringify({requests, elements})), 0);
"""


def _run_page_script(html: str, page_url: str, response: dict) -> dict:
    """Run a scan page's inline script as the browser at ``page_url`` would.

    ``fetch`` answers every request with ``response``; the result lists the
    requested URLs and the properties the script set on each element by id.
    """
    node = shutil.which('node')
    if node is None:
        pytest.skip('needs node to execute the page script')
    script = html.split('<script>', 1)[1].split('</script>', 1)[0]
    completed = subprocess.run(
        [node, '-e', _PAGE_HARNESS],
        input=json.dumps(
            {'script': script, 'pageUrl': page_url, 'response': response}),
        text=True, capture_output=True, check=False)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_apk_route_404_when_no_local_build(
        monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _point_at(monkeypatch, tmp_path / 'missing.apk')

    async def _run():
        response = await _app().test_client().get(
            '/api/v4/mobile/android/apk')
        return response.status_code, await response.get_json()

    status, body = asyncio.run(_run())
    assert status == 404
    assert 'assembleRelease' in str(body)


def test_apk_route_serves_bytes_with_apk_mime(
        monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    apk = tmp_path / 'app-release.apk'
    apk.write_bytes(b'PK\x03\x04fake-apk-bytes')
    _point_at(monkeypatch, apk)

    async def _run():
        response = await _app().test_client().get(
            '/api/v4/mobile/android/apk')
        return (response.status_code, response.headers, await response.data)

    status, headers, data = asyncio.run(_run())
    assert status == 200
    assert headers['Content-Type'].startswith(
        'application/vnd.android.package-archive')
    assert 'tofu-android.apk' in headers['Content-Disposition']
    assert data == b'PK\x03\x04fake-apk-bytes'


_RANGED_APK = bytes(range(256)) * 6


def _get_apk_range(monkeypatch, tmp_path, range_header: str):
    apk = tmp_path / 'app-release.apk'
    apk.write_bytes(_RANGED_APK)
    _point_at(monkeypatch, apk)

    async def _run():
        response = await _app().test_client().get(
            '/api/v4/mobile/android/apk', headers={'Range': range_header})
        return response.status_code, response.headers, await response.data

    return asyncio.run(_run())


@pytest.mark.parametrize(('range_header', 'first', 'last'), [
    ('bytes=-10', 1526, 1535),
    ('bytes=-1', 1535, 1535),
    ('bytes=-1536', 0, 1535),
    # Longer than the file: RFC 9110 §14.1.2 uses the entire representation.
    ('bytes=-5000', 0, 1535),
])
def test_apk_route_serves_a_suffix_range(
        range_header: str, first: int, last: int,
        monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """``bytes=-N`` is the last N bytes (a resumed download's tail check)."""
    status, headers, data = _get_apk_range(monkeypatch, tmp_path, range_header)

    assert status == 206
    assert data == _RANGED_APK[first:last + 1]
    assert headers['Content-Range'] == f'bytes {first}-{last}/{len(_RANGED_APK)}'


def test_apk_route_rejects_a_zero_length_suffix_range(
        monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """RFC 9110 §14.1.2: a suffix range is satisfiable only when non-zero;
    §15.5.17: the 416 names the representation's current length."""
    status, headers, _data = _get_apk_range(monkeypatch, tmp_path, 'bytes=-0')

    assert status == 416
    assert headers['Content-Range'] == f'bytes */{len(_RANGED_APK)}'


def test_link_route_embeds_presented_token_and_qr(
        monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    apk = tmp_path / 'app-release.apk'
    apk.write_bytes(b'PK\x03\x04fake')
    _point_at(monkeypatch, apk)

    async def _run():
        response = await _app().test_client().get(
            '/api/v4/mobile/android/link',
            headers={
                'Authorization': 'Bearer tofu_admin_testtoken',
                'Host': '192.168.1.20:8080',
            })
        return response.status_code, await response.get_json()

    status, body = asyncio.run(_run())
    assert status == 200
    data = _data(body)
    assert data['available'] is True
    assert data['tokenEmbedded'] is True
    assert data['url'] == (
        'http://192.168.1.20:8080/api/v4/mobile/android/apk'
        '?token=tofu_admin_testtoken')
    assert data['qr'].startswith('data:image/png;base64,')
    assert data['sizeBytes'] == len(b'PK\x03\x04fake')
    assert isinstance(data['modifiedMs'], int)


def test_link_route_without_token_reports_not_embedded(
        monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _point_at(monkeypatch, tmp_path / 'missing.apk')

    async def _run():
        response = await _app().test_client().get(
            '/api/v4/mobile/android/link',
            headers={'Host': '10.0.0.5:8080'})
        return response.status_code, await response.get_json()

    status, body = asyncio.run(_run())
    assert status == 200
    data = _data(body)
    assert data['available'] is False
    assert data['tokenEmbedded'] is False
    assert data['url'] == 'http://10.0.0.5:8080/api/v4/mobile/android/apk'
    assert 'sizeBytes' not in data


def test_link_url_downloads_the_apk_from_this_server(
        monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """The handed-out URL names a mounted route, not the retired v1 one."""
    apk = tmp_path / 'app-release.apk'
    apk.write_bytes(b'PK\x03\x04fake-apk-bytes')
    _point_at(monkeypatch, apk)

    async def _run():
        client = _app().test_client()
        link = await client.get(
            '/api/v4/mobile/android/link',
            headers={'Authorization': 'Bearer tofu_admin_testtoken',
                     'Host': '192.168.1.20:8080'})
        url = urlsplit(_data(await link.get_json())['url'])
        download = await client.get(
            url.path, query_string=dict(parse_qsl(url.query)),
            headers={'Host': url.netloc})
        return download.status_code, await download.data

    status, data = asyncio.run(_run())
    assert status == 200
    assert data == b'PK\x03\x04fake-apk-bytes'


def test_link_keeps_the_proxy_prefix_of_the_callers_base(
        monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Behind code-server the request arrives without ``/proxy/15000``; the
    caller's own root (``base``) restores it, scheme included."""
    _point_at(monkeypatch, tmp_path / 'missing.apk')

    async def _run():
        response = await _app().test_client().get(
            '/api/v4/mobile/android/link',
            query_string={'base': 'https://ide.example/proxy/15000/'},
            headers={'Authorization': 'Bearer tofu_admin_testtoken',
                     'Host': 'ide.example'})
        return response.status_code, await response.get_json()

    status, body = asyncio.run(_run())
    assert status == 200
    assert _data(body)['url'] == (
        'https://ide.example/proxy/15000/api/v4/mobile/android/apk'
        '?token=tofu_admin_testtoken')


@pytest.mark.parametrize('base', [
    'https://evil.example/proxy/15000/',
    'https://ide.example/proxy/15000/?next=/',
    'ftp://ide.example/proxy/15000/',
    'http://[::1',
])
def test_link_rejects_a_base_that_is_not_this_servers_address(
        base: str, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """The URL carries the caller's credential, so it may only point back at
    the Host this request reached."""
    _point_at(monkeypatch, tmp_path / 'missing.apk')

    async def _run():
        response = await _app().test_client().get(
            '/api/v4/mobile/android/link',
            query_string={'base': base},
            headers={'Authorization': 'Bearer tofu_admin_testtoken',
                     'Host': 'ide.example'})
        return response.status_code, await response.get_data(as_text=True)

    status, text = asyncio.run(_run())
    assert status == 400
    assert 'tofu_admin_testtoken' not in text


def test_scan_page_reports_the_local_build(
        monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    apk = tmp_path / 'app-release.apk'
    apk.write_bytes(b'PK\x03\x04fake')
    _point_at(monkeypatch, apk)

    async def _run():
        response = await _app().test_client().get(
            '/api/v4/mobile/android',
            headers={'Host': '192.168.1.20:8080'})
        return (response.status_code,
                response.headers.get('Content-Type', ''),
                await response.get_data(as_text=True))

    status, content_type, html = asyncio.run(_run())
    assert status == 200
    assert content_type.startswith('text/html')
    assert 'APK ready' in html


def test_scan_page_asks_for_its_link_under_its_own_root(
        monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Behind ``/proxy/15000/`` only the browser knows the prefix: the page
    requests the v4 link with its own root and renders what comes back."""
    _point_at(monkeypatch, tmp_path / 'missing.apk')

    async def _run():
        response = await _app().test_client().get('/api/v4/mobile/android')
        return await response.get_data(as_text=True)

    apk_url = ('https://ide.example/proxy/15000/api/v4/mobile/android/apk'
               '?token=tofu_admin_testtoken')
    result = _run_page_script(
        asyncio.run(_run()),
        'https://ide.example/proxy/15000/api/v4/mobile/android',
        {'data': {'available': False, 'url': apk_url, 'tokenEmbedded': True,
                  'qr': 'data:image/png;base64,QR'}})

    assert result['requests'] == [
        'https://ide.example/proxy/15000/api/v4/mobile/android/link'
        '?base=https%3A%2F%2Fide.example%2Fproxy%2F15000%2F']
    links = [element for element in result['elements'].values()
             if element.get('href') == apk_url]
    assert links and links[0].get('textContent') == apk_url
    assert any(element.get('src') == 'data:image/png;base64,QR'
               for element in result['elements'].values())


def test_routes_require_authentication(
        monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _point_at(monkeypatch, tmp_path / 'missing.apk')
    app = _app(authenticated=False)

    async def _run():
        client = app.test_client()
        return [(await client.get(path)).status_code for path in (
            '/api/v4/mobile/android',
            '/api/v4/mobile/android/apk',
            '/api/v4/mobile/android/link',
            '/api/v4/mobile/connect',
            '/api/v4/mobile/connect/link',
        )]

    assert asyncio.run(_run()) == [401, 401, 401, 401, 401]


def test_connect_link_builds_deep_link_and_qr() -> None:
    async def _run():
        response = await _app().test_client().get(
            '/api/v4/mobile/connect/link',
            query_string={
                'url': 'https://host.test/proxy/15000/',
                'name': 'Shanghai sandbox',
            })
        return response.status_code, await response.get_json()

    status, body = asyncio.run(_run())
    assert status == 200
    data = _data(body)
    assert data['url'] == 'https://host.test/proxy/15000/'
    assert data['name'] == 'Shanghai sandbox'
    assert data['deepLink'].startswith('tofu://connect?url=')
    # The address and name are percent-encoded into the deep link.
    assert 'https%3A%2F%2Fhost.test%2Fproxy%2F15000%2F' in data['deepLink']
    assert 'name=Shanghai%20sandbox' in data['deepLink']
    assert data['qr'].startswith('data:image/png;base64,')


def test_connect_link_defaults_name_to_host() -> None:
    async def _run():
        response = await _app().test_client().get(
            '/api/v4/mobile/connect/link',
            query_string={
                'url': 'https://a1b2-vscode-dc1.example.com/proxy/15000/',
            })
        return await response.get_json()

    body = asyncio.run(_run())
    assert _data(body)['name'] == 'a1b2-vscode-dc1.example.com'


@pytest.mark.parametrize('url', ['ftp://host.test', 'http://[::1'])
def test_connect_link_rejects_non_http_url(url: str) -> None:
    async def _run():
        response = await _app().test_client().get(
            '/api/v4/mobile/connect/link', query_string={'url': url})
        return response.status_code

    assert asyncio.run(_run()) == 400


def test_connect_page_asks_for_its_link_under_its_own_root() -> None:
    """The connect page fetches the v4 link beside itself (proxy prefix
    kept) for the root it computed, and shows the QR that comes back."""
    async def _run():
        response = await _app().test_client().get('/api/v4/mobile/connect')
        return (
            response.status_code,
            response.headers.get('Content-Type', ''),
            await response.get_data(as_text=True),
        )

    status, content_type, html = asyncio.run(_run())
    assert status == 200
    assert content_type.startswith('text/html')
    root = 'https://ide.example/proxy/15000/'
    result = _run_page_script(html, root + 'api/v4/mobile/connect', {'data': {
        'url': root, 'name': 'ide.example', 'deepLink': 'tofu://connect?url=x',
        'qr': 'data:image/png;base64,QR'}})

    assert result['requests'] == [
        root + 'api/v4/mobile/connect/link'
        '?url=https%3A%2F%2Fide.example%2Fproxy%2F15000%2F']
    assert any(element.get('src') == 'data:image/png;base64,QR'
               for element in result['elements'].values())
