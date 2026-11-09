"""Mobile download metadata and credential-free connection addresses.

The checked-in android/published-release.json names a verified artifact, not
Gradle's unreleased version. Health reads this local authority only; it never
contacts a release service or adds a background refresh. Deployments can
explicitly pair an alternate download URL with its published version.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote, urlsplit

MOBILE_CLIENT_APK_ASSET = 'tofu-android.apk'
PUBLISHED_RELEASE_PATH = Path(__file__).resolve().parents[2] / 'android/published-release.json'
_SECRET_NAMES = frozenset({
    'token', 'accesstoken', 'refreshtoken', 'idtoken', 'apikey', 'key',
    'secret', 'clientsecret', 'password', 'passwd', 'pwd', 'pass', 'authorization',
    'auth', 'bearer', 'credential', 'credentials', 'session', 'sessionid',
    'sessiontoken', 'cookie', 'cookies', 'codeserversession', 'code', 'oauthcode',
    'ticket', 'signature', 'sig', 'ssotoken', 'authtoken',
    'jwt', 'samlresponse', 'csrf', 'authcode', 'authorizationcode', 'accesskey', 'privatekey',
})
_FIELDS = re.compile(r'[?&#;]')
# Browser special schemes accept mixed/redundant/slashless authorities and
# percent-encode spaces in userinfo. Inspect before every decoding step.
_EMBEDDED_USERINFO = re.compile(r'(?:(?:https?|wss?|ftp):/*|/{2,})[^/?#]*@', re.IGNORECASE)


def _contains_credentials(value: str) -> bool:
    # Decode the whole component so nested return URLs keep their own ?/#
    # boundaries. parse_qsl alone treats https://host/?token as a benign key.
    for _ in range(5):
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            return True
        if _EMBEDDED_USERINFO.search(value.replace('\\', '/')):
            return True
        for field in _FIELDS.split(value):
            assignments = field.split('=')
            names = assignments[:-1] if len(assignments) > 1 else assignments
            for name in names:
                normalized = re.sub(r'[^a-z0-9]', '', name.casefold())
                if normalized in _SECRET_NAMES or normalized.endswith(('token', 'password', 'passwd', 'secret', 'credential', 'credentials', 'signature', 'apikey', 'session', 'sessionid', 'cookie', 'cookies')):
                    return True
        decoded = unquote(value)
        if decoded == value:
            return False
        value = decoded
    return True  # deeper encoding is ambiguous; bounded fail-closed parsing


def credential_free_http_url(value: object, *, max_length: int = 2048) -> str:
    """Validate a user-persistable address; errors never include raw input.

    Preserve non-secret query and SPA fragment parameters. URL credentials
    and nested credential-bearing bootstrap/return links are rejected, not
    silently stored or exported in QR codes.
    """
    if not isinstance(value, str):
        return ''
    address = value.strip()
    if not address or len(address) > max_length or any(ord(c) < 32 for c in address):
        return ''
    try:
        parts = urlsplit(address)
        if parts.scheme not in ('http', 'https') or not parts.hostname:
            return ''
        if parts.username is not None or parts.password is not None or '\\' in parts.netloc:
            return ''
        _ = parts.port  # force validation of invalid authorities/ports
        if _contains_credentials(parts.query) or _contains_credentials(parts.fragment):
            return ''
    except ValueError:
        return ''
    return address


@lru_cache(maxsize=1)
def published_android_release() -> dict:
    """Read and validate the local publication receipt once per process."""
    try:
        value = json.loads(PUBLISHED_RELEASE_PATH.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict) or value.get('schemaVersion') != 1:
        return {}
    version = value.get('versionName')
    if not isinstance(version, str) or not re.fullmatch(r'\d+(?:\.\d+){1,3}', version):
        return {}
    tag = value.get('tag')
    if (value.get('repository') != 'rangehow/tofu-android'
            or tag not in ('v' + version, 'android-v' + version)):
        return {}
    url = credential_free_http_url(value.get('url'))
    expected = f'https://github.com/rangehow/tofu-android/releases/download/{tag}/{MOBILE_CLIENT_APK_ASSET}'
    if (url != expected or value.get('filename') != MOBILE_CLIENT_APK_ASSET
            or type(value.get('versionCode')) is not int or value['versionCode'] <= 0
            or type(value.get('sizeBytes')) is not int or value['sizeBytes'] <= 0
            or not re.fullmatch(r'[a-f0-9]{64}', str(value.get('sha256', '')))):
        return {}
    return dict(value, url=url)


_RELEASE = published_android_release()
DEFAULT_MOBILE_CLIENT_URL = str(_RELEASE.get('url', ''))
MOBILE_CLIENT_VERSION = str(_RELEASE.get('versionName', ''))


def mobile_download_metadata(*, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return safe download addresses and an artifact-matched Android badge."""
    if environ is None:
        # Explicit literal reads keep this owner's configuration discoverable
        # by the repository inventory, without losing absent-vs-empty URL.
        env = {
            'TOFU_MOBILE_CLIENT_VERSION': os.environ.get('TOFU_MOBILE_CLIENT_VERSION', ''),
            'TOFU_IOS_CLIENT_URL': os.environ.get('TOFU_IOS_CLIENT_URL', ''),
        }
        if 'TOFU_MOBILE_CLIENT_URL' in os.environ:
            env['TOFU_MOBILE_CLIENT_URL'] = os.environ['TOFU_MOBILE_CLIENT_URL']
    else:
        env = environ
    overridden = 'TOFU_MOBILE_CLIENT_URL' in env
    url = credential_free_http_url(env.get('TOFU_MOBILE_CLIENT_URL') if overridden else DEFAULT_MOBILE_CLIENT_URL)
    if url and overridden:
        version = env.get('TOFU_MOBILE_CLIENT_VERSION', '').strip()
        if not re.fullmatch(r'\d+(?:\.\d+){1,3}', version):
            version = ''
    else:
        version = MOBILE_CLIENT_VERSION if url else ''
    return {
        'mobile_client_url': url,
        'mobile_client_version': version,
        'ios_client_url': credential_free_http_url(env.get('TOFU_IOS_CLIENT_URL', '')),
    }
