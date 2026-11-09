"""Published mobile metadata never advertises an unbuilt source revision."""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def _owner():
    return importlib.import_module('lib.mobile_distribution')


def test_default_metadata_is_pinned_to_the_verified_published_artifact():
    release = _owner().mobile_download_metadata(environ={})
    manifest = json.loads((ROOT / 'android/published-release.json').read_text())
    assert release['mobile_client_version'] == manifest['versionName']
    assert release['mobile_client_url'] == manifest['url']
    assert '/releases/download/' in release['mobile_client_url']
    assert '/latest/' not in release['mobile_client_url']
    assert manifest['versionCode'] > 0
    assert len(manifest['sha256']) == 64
    assert manifest['sizeBytes'] > 0


def test_custom_asset_does_not_borrow_the_default_version_badge():
    release = _owner().mobile_download_metadata(environ={
        'TOFU_MOBILE_CLIENT_URL': 'https://downloads.example.test/private.apk',
    })
    assert release['mobile_client_version'] == ''
    assert release['mobile_client_url'].endswith('/private.apk')


def test_custom_asset_and_version_are_an_explicit_pair():
    release = _owner().mobile_download_metadata(environ={
        'TOFU_MOBILE_CLIENT_URL': 'https://downloads.example.test/private.apk',
        'TOFU_MOBILE_CLIENT_VERSION': '1.2.3',
        'TOFU_IOS_CLIENT_URL': 'https://testflight.apple.com/join/example',
    })
    assert release['mobile_client_version'] == '1.2.3'
    assert release['ios_client_url'].startswith('https://testflight.apple.com/')


def test_explicit_empty_url_disables_android_download():
    release = _owner().mobile_download_metadata(environ={
        'TOFU_MOBILE_CLIENT_URL': '', 'TOFU_MOBILE_CLIENT_VERSION': '1.2.3',
    })
    assert release['mobile_client_url'] == ''
    assert release['mobile_client_version'] == ''


@pytest.mark.parametrize('url', [
    'javascript:alert(1)', 'data:text/html,unsafe',
    'https://user:password@downloads.example.test/app.apk',
    'https://downloads.example.test/app.apk?token=secret',
])
def test_download_metadata_rejects_unsafe_links(url):
    release = _owner().mobile_download_metadata(environ={
        'TOFU_MOBILE_CLIENT_URL': url, 'TOFU_IOS_CLIENT_URL': url,
    })
    assert release['mobile_client_url'] == ''
    assert release['ios_client_url'] == ''


@pytest.mark.parametrize('replacement', [
    {'versionName': '99.0.0'}, {'repository': 'attacker/tofu-android'},
    {'tag': 'v99.0.0'}, {'url': 'http://github.com/rangehow/tofu-android/releases/download/v0.1.15/tofu-android.apk'},
])
def test_invalid_publication_identity_fails_closed(monkeypatch, tmp_path, replacement):
    owner = _owner()
    receipt = json.loads((ROOT / 'android/published-release.json').read_text())
    receipt.update(replacement)
    path = tmp_path / 'receipt.json'
    path.write_text(json.dumps(receipt))
    monkeypatch.setattr(owner, 'PUBLISHED_RELEASE_PATH', path)
    owner.published_android_release.cache_clear()
    try:
        assert owner.published_android_release() == {}
    finally:
        owner.published_android_release.cache_clear()


def test_version_only_override_cannot_relabel_the_default_apk():
    release = _owner().mobile_download_metadata(environ={
        'TOFU_MOBILE_CLIENT_VERSION': '99.0.0',
    })
    manifest = json.loads((ROOT / 'android/published-release.json').read_text())
    assert release['mobile_client_version'] == manifest['versionName']
