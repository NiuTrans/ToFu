"""Phone connection links preserve routing, never export login credentials."""
from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, quote, urlsplit

import pytest
from quart import Quart, g

from lib.api_keys import local_admin_context
from lib.api_v4 import attach_v4_envelope, copy_blueprint_to_v4
from routes.api_v1.mobile import api_v1_mobile_bp

pytestmark = pytest.mark.api


def mobile_app(*, authenticated: bool = True) -> Quart:
    app = Quart(__name__)
    app.config.update(TESTING=True, ACTIVE_API_MAJOR=4, API_V4_ENABLED=True)
    if authenticated:
        @app.before_request
        def authenticate_fixture():
            g.auth_ctx = local_admin_context()
    app.register_blueprint(copy_blueprint_to_v4(api_v1_mobile_bp, name='mobile_v4'))
    attach_v4_envelope(app)
    return app


@pytest.mark.parametrize('address', [
    'https://user:fixture-password@host.test/proxy/15000/',
    'https://host.test/?token=fixture-token',
    'https://host.test/#access_token=fixture-token',
    'https://host.test/#/chat?client_secret=fixture-token',
    'https://host.test/#access_token=fixture-token?theme=dark',
    'https://host.test/#access_token=fixture-token&next=https://host.test/welcome?lang=en',
    'https://host.test/?next=%2F%2Fuser%3Afixture-password%40host.test%2F',
    'https://host.test/?return=https%3A%2F%2Fhost.test%2F%3Ftoken%3Dfixture-token',
    'https://host.test/?%2574oken=fixture-token',
    'https://host.test/?X-Api-Key=fixture-token',
    'https://host.test/?tofu_session=fixture-token',
    'https://host.test/?next=https%3A%2F%2Fhost.test%2F%3FX-Api-Key%3Dfixture-token',
    'https://host.test/?X-Amz-Credential=fixture-token&X-Amz-Signature=fixture-token',
    'https://host.test/?next=https%3A%2F%2Fhost.test%2F%3FX-Goog-Credential%3Dfixture-token%26X-Goog-Signature%3Dfixture-token',
    'https://host.test:invalid/',
])
def test_connection_qr_rejects_credentials_without_echoing_them(address):
    async def exercise():
        response = await mobile_app().test_client().get(
            '/api/v4/mobile/connect/link', query_string={'url': address})
        assert response.status_code == 400
        assert 'fixture-token' not in await response.get_data(as_text=True)
        assert 'fixture-password' not in await response.get_data(as_text=True)
    asyncio.run(exercise())


@pytest.mark.parametrize('nested', [
    r'https:\\user:fixture-password@other.test/',
    r'https:/\user:fixture-password@other.test/',
    r'\\user:fixture-password@other.test/',
    'https:///user:fixture-password@other.test/',
    'https:user:fixture-password@other.test/',
    'https://us\ter:fixture-password@other.test/',
    'https://user name:fixture-password@other.test/',
    'https://user%2Fname:fixture-password@other.test/',
    'access_token=fixture-token',
])
def test_connection_qr_rejects_nested_browser_authorities_and_assignments(nested):
    address = 'https://host.test/?workspace=one&return=' + quote(nested, safe='')

    async def exercise():
        response = await mobile_app().test_client().get(
            '/api/v4/mobile/connect/link', query_string={'url': address})
        assert response.status_code == 400
        assert 'fixture-password' not in await response.get_data(as_text=True)
        assert 'fixture-token' not in await response.get_data(as_text=True)
    asyncio.run(exercise())


def test_v4_connection_link_preserves_port_prefix_and_benign_parameters():
    address = 'https://host.test:8443/team/proxy/15005/?theme=dark#/chat'
    async def exercise():
        response = await mobile_app().test_client().get(
            '/api/v4/mobile/connect/link', query_string={'url': address, 'name': 'Phone'})
        assert response.status_code == 200
        body = await response.get_json()
        assert 'meta' in body
        data = body['data']
        assert data['url'] == address
        assert parse_qs(urlsplit(data['deepLink']).query) == {'url': [address], 'name': ['Phone']}
        assert data['qr'].startswith('data:image/png;base64,')
    asyncio.run(exercise())


def test_local_download_link_does_not_export_the_presented_credential(monkeypatch, tmp_path):
    monkeypatch.setenv('TOFU_ANDROID_APK_PATH', str(tmp_path / 'missing.apk'))
    async def exercise():
        client = mobile_app().test_client()
        for endpoint in ('link', ''):
            response = await client.get('/api/v4/mobile/android' + ('/' + endpoint if endpoint else ''),
                                        headers={'Authorization': 'Bearer fixture-do-not-export'})
            assert response.status_code == 200
            assert 'fixture-do-not-export' not in await response.get_data(as_text=True)
        response = await client.get('/api/v4/mobile/android/link')
        assert (await response.get_json())['data']['url'].endswith('/api/v4/mobile/android/apk')
    asyncio.run(exercise())


def test_phone_setup_stays_authenticated():
    async def exercise():
        client = mobile_app(authenticated=False).test_client()
        for endpoint in ('connect', 'connect/link', 'android/link'):
            response = await client.get('/api/v4/mobile/' + endpoint)
            assert response.status_code == 401
    asyncio.run(exercise())


def test_local_download_link_retains_the_explicit_same_host_proxy_root():
    root = 'https://host.test:8443/team/api/service/proxy/15005/'

    async def exercise():
        response = await mobile_app().test_client().get(
            '/api/v4/mobile/android/link', query_string={'base': root},
            headers={'Host': 'host.test:8443', 'Authorization': 'Bearer fixture-do-not-export'})
        assert response.status_code == 200
        data = (await response.get_json())['data']
        assert data['url'] == root + 'api/v4/mobile/android/apk'
        assert data['tokenEmbedded'] is False
        assert 'fixture-do-not-export' not in await response.get_data(as_text=True)
    asyncio.run(exercise())


@pytest.mark.parametrize('root', [
    'https://other.test/proxy/15005/',
    'https://user:fixture-password@host.test/proxy/15005/',
    'https://host.test/proxy/15005/?token=fixture-token',
    'https://host.test/proxy/15005/#fragment',
    'https://host.test/proxy/15005/?',
    'https://host.test/proxy/15005/#',
])
def test_local_download_link_rejects_a_foreign_or_credential_bearing_root(root):
    async def exercise():
        response = await mobile_app().test_client().get(
            '/api/v4/mobile/android/link', query_string={'base': root},
            headers={'Host': 'host.test'})
        assert response.status_code == 400
        assert 'fixture-token' not in await response.get_data(as_text=True)
        assert 'fixture-password' not in await response.get_data(as_text=True)
    asyncio.run(exercise())
