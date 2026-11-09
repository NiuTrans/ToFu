"""Authenticated local APK delivery and credential-free phone connection QR.

The operator's configured local APK is exposed directly without per-request
certificate/manifest validation. Verify it with the SDK release validator
before delivery. This adapter does not certify CI publication or uploaded
bytes; local URLs retain a configured trusted ASGI root_path only.

Endpoints:
  * ``GET /api/v4/mobile/android/apk``  — the APK attachment itself.
  * ``GET /api/v4/mobile/android/link`` — credential-free absolute URL and
    QR; ``?base=`` retains the caller's same-host reverse-proxy root.
  * ``GET /api/v4/mobile/android``      — minimal scan-me page; it asks
    ``link`` under its own root, computed from ``location``.
  * ``GET /api/v4/mobile/connect/link`` — JSON: a ``tofu://connect`` deep link
    (and its QR ``data:`` URI) for THIS server's address, so the phone's
    built-in scanner adds the server without typing a UUID URL.
  * ``GET /api/v4/mobile/connect``      — scan-to-connect page for the server
    address, computed from ``location`` so it stays correct behind any
    reverse-proxy prefix.

The APK location defaults to the in-repo Gradle output and can be pointed
elsewhere with ``TOFU_ANDROID_APK_PATH``. All endpoints sit behind the
normal auth gate. Never reflect a browser's bearer credential into links/QRs.
Declarations retain the v1 adapter namespace; production mounts their v4 twins.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote, urlparse

from quart import Blueprint, Response, request

from lib.api_v4 import raw_success_response
from lib.api_response import api_bad_request, api_not_found, api_ok
from lib.file_serving import send_file_conditional
from lib.log import get_logger
from lib.mobile_distribution import credential_free_http_url
from lib.openapi import media_responses, api_meta

from .auth import require_auth

logger = get_logger(__name__)

api_v1_mobile_bp = Blueprint('api_v1_mobile', __name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_APK = (_REPO_ROOT / 'android' / 'app' / 'build' / 'outputs' /
                'apk' / 'release' / 'app-release.apk')
_APK_MIME = 'application/vnd.android.package-archive'
#: Where phones fetch the APK, relative to the server root: the mounted v4
#: projection of ``mobile_android_apk_v1`` (``/api/v1/*`` answers 426).
_APK_DOWNLOAD_PATH = 'api/v4/mobile/android/apk'

# Recover only the actual scan-page suffix: a deployment prefix may contain
# /api/. Keep the browser's prefix and scheme, which a stripping proxy hides.
_PAGE_ROOT_JS = r"""
  var match = location.pathname.match(/^(.*)\/api\/v(?:1|4)\/mobile\/(?:android|connect)\/?$/);
  if (!match) {
    document.getElementById('err').textContent = 'Could not determine this server address';
    return;
  }
  var root = location.origin + match[1] + '/';
"""


def _apk_path() -> Path:
    override = os.environ.get('TOFU_ANDROID_APK_PATH', '').strip()
    return Path(override) if override else _DEFAULT_APK


def _caller_root(base: str) -> str | None:
    """Retain the caller's same-host deployment prefix, without credentials."""
    root = base or request.url_root
    if not _is_absolute_http_url(root):
        return None
    parsed = urlparse(root)
    if (parsed.netloc.lower() != (request.host or '').lower()
            or '?' in root or '#' in root):
        return None
    return root.rstrip('/') + '/'


def _download_url(root: str) -> str:
    # Only the production-mounted route is emitted. The phone signs in itself.
    return root + _APK_DOWNLOAD_PATH


@api_v1_mobile_bp.route('/api/v1/mobile/android/apk', methods=['GET'])
@require_auth
@api_meta(
    responses=media_responses('application/vnd.android.package-archive', conditional=True),
    summary='Download the locally built Android APK', tags=['mobile'])
@raw_success_response
def mobile_android_apk_v1():
    path = _apk_path()
    if not path.is_file():
        return api_not_found(
            'No local APK build found. Configure private release signing and '
            'run ./gradlew assembleRelease in android/ (or set TOFU_ANDROID_APK_PATH).')
    response = send_file_conditional(
        path, mimetype=_APK_MIME,
        download_name='tofu-android.apk', as_attachment=True)
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@api_v1_mobile_bp.route('/api/v1/mobile/android/link', methods=['GET'])
@require_auth
@api_meta(
    summary='Absolute download URL + QR for the local Android APK',
    description='Credential-free local APK URL. Behind a stripping reverse proxy, '
                'pass the same-host root the phone reaches as base.',
    tags=['mobile'], request_body=False,
    parameters=[{'name': 'base', 'in': 'query', 'required': False,
                 'schema': {'type': 'string'},
                 'description': 'Absolute same-host HTTP(S) deployment root without credentials.'}],
)
def mobile_android_link_v1():
    from lib.qr import qr_png_data_uri

    root = _caller_root((request.args.get('base') or '').strip())
    if root is None:
        return api_bad_request('base must be this server\'s own absolute http(s) root, '
                               'without credentials, query or fragment')
    url = _download_url(root)
    payload = {
        'available': _apk_path().is_file(),
        'url': url,
        'tokenEmbedded': False,
        'qr': qr_png_data_uri(url),
    }
    path = _apk_path()
    if payload['available']:
        stat = path.stat()
        payload['sizeBytes'] = stat.st_size
        payload['modifiedMs'] = int(stat.st_mtime * 1000)
    return api_ok(payload)


_ANDROID_PAGE_SCRIPT = '<script>\n(function () {\n' + _PAGE_ROOT_JS + """\
  var err = document.getElementById('err');
  fetch(root + 'api/v4/mobile/android/link?base=' + encodeURIComponent(root),
        {headers: {'Accept': 'application/json'}, credentials: 'same-origin', redirect: 'error'})
    .then(function (r) {
      if (!r.ok) { throw new Error('Server refused the download link'); }
      return r.json();
    })
    .then(function (body) {
      var d = body && body.data ? body.data : body;
      var link = document.getElementById('link');
      link.href = d.url;
      link.textContent = d.url;
      if (d.qr) {
        var qr = document.getElementById('qr');
        qr.src = d.qr;
        qr.hidden = false;
      } else {
        err.textContent = 'QR unavailable — use the link below.';
      }
    })
    .catch(function () { err.textContent = 'Could not build the download link'; });
})();
</script>
"""


@api_v1_mobile_bp.route('/api/v1/mobile/android', methods=['GET'])
@require_auth
@api_meta(
    responses=media_responses('text/html'),
    summary='Scan-to-install page for the local Android APK',
          tags=['mobile'])
@raw_success_response
def mobile_android_page_v1():
    path = _apk_path()
    if path.is_file():
        stat = path.stat()
        from datetime import datetime
        built = datetime.fromtimestamp(stat.st_mtime).strftime(
            '%Y-%m-%d %H:%M')
        status = (f'APK ready — {stat.st_size / (1024 * 1024):.1f} MiB, '
                  f'built {built}')
    else:
        status = ('No local build yet — configure private release signing, then run '
                  '<code>./gradlew assembleRelease</code> in android/.')
    html = (
        '<!doctype html><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Tofu Android — local install</title>'
        '<style>body{font:15px/1.6 system-ui,sans-serif;margin:3em auto;'
        'max-width:26em;padding:0 1.2em;color:#222;text-align:center}'
        'h1{font-size:1.3rem}code{background:#f3f3f3;padding:.1em .4em;'
        'border-radius:3px;word-break:break-all}'
        'a{overflow-wrap:anywhere}'
        'p.hint{color:#666;font-size:.85rem}</style>'
        '<h1>Tofu Android — local build</h1>'
        f'<p>{status}</p>'
        '<img id="qr" width="264" height="264" alt="APK download QR" hidden>'
        '<p id="err" class="hint"></p>'
        '<p class="hint">Use a network that reaches this server and sign in independently.<br>'
        '<a id="link"></a></p>' + _ANDROID_PAGE_SCRIPT)
    response = Response(html, content_type='text/html; charset=utf-8')
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response


# ═══════════════════════════════════════════════════════════════════════
#  Scan-to-connect: this server's own address as a phone-addable QR
# ═══════════════════════════════════════════════════════════════════════

#: Deep-link scheme the Android shell registers (see AndroidManifest.xml).
_CONNECT_DEEP_LINK = 'tofu://connect'
_MAX_CONNECT_URL = 2048
_MAX_CONNECT_NAME = 64


def _is_absolute_http_url(value: str) -> bool:
    return bool(credential_free_http_url(value, max_length=_MAX_CONNECT_URL))


def _connect_deep_link(url: str, name: str) -> str:
    """``tofu://connect?url=…&name=…`` with both params percent-encoded."""
    return (
        f'{_CONNECT_DEEP_LINK}?url={quote(url, safe="")}'
        f'&name={quote(name, safe="")}'
    )


@api_v1_mobile_bp.route('/api/v1/mobile/connect/link', methods=['GET'])
@require_auth
@api_meta(
    summary='Build a Tofu connect deep link and QR for a server address',
    description=(
        'Encodes a `tofu://connect` deep link (the address, plus an optional '
        'display name) that a native client opens as a review-only server '
        'draft. `url` defaults to this request\'s root, but callers behind a '
        'reverse-proxy prefix should pass their own absolute address so the '
        'link matches what the client actually reaches.'),
    tags=['mobile'],
    request_body=False,
    parameters=[
        {'name': 'url', 'in': 'query', 'required': False,
         'schema': {'type': 'string'},
         'description': 'Absolute http(s) server address to encode.'},
        {'name': 'name', 'in': 'query', 'required': False,
         'schema': {'type': 'string'},
         'description': 'Suggested server display name (defaults to the host).'},
    ],
)
def mobile_connect_link_v1():
    url = (request.args.get('url') or '').strip()
    if not url:
        url = request.url_root
    if not _is_absolute_http_url(url):
        return api_bad_request('url must be an absolute http(s) server address without credentials')
    name = (request.args.get('name') or '').strip()[:_MAX_CONNECT_NAME]
    if not name:
        name = urlparse(url).netloc

    deep_link = _connect_deep_link(url, name)
    from lib.qr import qr_png_data_uri

    return api_ok(url=url, name=name, deepLink=deep_link,
                  qr=qr_png_data_uri(deep_link))


# Static shell; the real URL is resolved client-side so the page is correct
# under any reverse-proxy prefix (code-server `/proxy/<port>/`, cloud IDE, …).
# The address only ever needs percent-encoding, so a small JS replace suffices
# and the page needs no server-side templating.
_CONNECT_PAGE_HTML = r"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connect a phone to this Tofu</title>
<style>
  body{font:15px/1.6 system-ui,sans-serif;margin:3em auto;max-width:28em;
       padding:0 1.2em;color:#222;text-align:center}
  h1{font-size:1.3rem}
  img{width:264px;height:264px;image-rendering:pixelated;
      background:#fff;border:1px solid #eee;border-radius:8px}
  code{background:#f3f3f3;padding:.1em .4em;border-radius:3px;
       word-break:break-all;font-size:.82rem}
  p.hint{color:#666;font-size:.85rem}
</style>
<h1>Connect a phone</h1>
<p>Open <strong>Tofu</strong> on your phone and scan this code, or tap
   <strong>Scan QR</strong> in the Android app. Review the address before saving.</p>
<img id="qr" alt="Tofu connect QR">
<p id="err" class="hint"></p>
<p class="hint">Address: <code id="u">…</code></p>
<p class="hint">Link: <code id="d">…</code></p>
<script>
(function () {
""" + _PAGE_ROOT_JS + """\
  var q = root + 'api/v4/mobile/connect/link?url=' + encodeURIComponent(root);
  fetch(q, {headers: {'Accept': 'application/json'}, credentials: 'same-origin', redirect: 'error'})
    .then(function (r) {
      if (!r.ok) { throw new Error('Server refused the connection link'); }
      return r.json();
    })
    .then(function (body) {
      var d = body && body.data ? body.data : body;
      if (!d || !d.qr) { throw new Error('QR unavailable on this server'); }
      document.getElementById('qr').src = d.qr;
      document.getElementById('u').textContent = d.url;
      document.getElementById('d').textContent = d.deepLink;
    })
    .catch(function (e) {
      document.getElementById('err').textContent =
        'Could not build the QR: ' + e;
    });
})();
</script>
"""


@api_v1_mobile_bp.route('/api/v1/mobile/connect', methods=['GET'])
@require_auth
@api_meta(
    responses=media_responses('text/html'),

    summary='Scan-to-connect page for this Tofu server',
    description=(
        'Renders a QR encoding a `tofu://connect` link for this server\'s own '
        'address, so a phone opens a review draft with one scan. The address is '
        'resolved from `location` in the browser, so the page stays correct '
        'behind any reverse-proxy prefix.'),
    tags=['mobile'],
    request_body=False,
)
@raw_success_response
def mobile_connect_page_v1():
    response = Response(_CONNECT_PAGE_HTML, content_type='text/html; charset=utf-8')
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response
