"""Serve the repo-built Android APK over the LAN.

The release APK produced by ``./gradlew assembleRelease`` (same signing
config as the GitHub-published artifact) is exposed directly by this server,
so a phone on the same network installs the exact build the operator just
compiled — no GitHub round-trip and no remote/local divergence.

Endpoints:
  * ``GET /api/v1/mobile/android/apk``  — the APK attachment itself.
  * ``GET /api/v1/mobile/android/link`` — JSON: absolute URL (with the
    caller's credential embedded via the ``?token=`` first-link transport
    when one was presented) + a QR ``data:`` URI of that URL.
  * ``GET /api/v1/mobile/android``      — minimal scan-me page rendering
    the QR in the operator's browser.
  * ``GET /api/v1/mobile/connect/link`` — JSON: a ``tofu://connect`` deep link
    (and its QR ``data:`` URI) for THIS server's address, so the phone's
    built-in scanner adds the server without typing a UUID URL.
  * ``GET /api/v1/mobile/connect``      — scan-to-connect page for the server
    address, computed from ``location`` so it stays correct behind any
    reverse-proxy prefix.

The APK location defaults to the in-repo Gradle output and can be pointed
elsewhere with ``TOFU_ANDROID_APK_PATH``. All endpoints sit behind the
normal auth gate; the phone authenticates through the query-token redirect
the first link carries.
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
from lib.openapi import media_responses, api_meta

from .auth import require_auth

logger = get_logger(__name__)

api_v1_mobile_bp = Blueprint('api_v1_mobile', __name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_APK = (_REPO_ROOT / 'android' / 'app' / 'build' / 'outputs' /
                'apk' / 'release' / 'app-release.apk')
_APK_MIME = 'application/vnd.android.package-archive'
_APK_URL_PATH = '/api/v1/mobile/android/apk'


def _apk_path() -> Path:
    override = os.environ.get('TOFU_ANDROID_APK_PATH', '').strip()
    return Path(override) if override else _DEFAULT_APK


def _presented_token() -> str:
    """Raw credential the current request arrived with ('' when none).

    The session cookie stores the raw token, so a normal browser session
    yields its credential here without any keystore lookup.
    """
    from .auth import _extract_bearer_or_cookie
    return _extract_bearer_or_cookie()


def _download_url() -> str:
    url = f'{request.scheme}://{request.host}{_APK_URL_PATH}'
    token = _presented_token()
    if token:
        url = f'{url}?token={token}'
    return url


@api_v1_mobile_bp.route(_APK_URL_PATH, methods=['GET'])
@require_auth
@api_meta(
    responses=media_responses('application/vnd.android.package-archive', conditional=True),
    summary='Download the locally built Android APK', tags=['mobile'])
@raw_success_response
def mobile_android_apk_v1():
    path = _apk_path()
    if not path.is_file():
        return api_not_found(
            'No local APK build found. Run ./gradlew assembleRelease in '
            'android/ first (or set TOFU_ANDROID_APK_PATH).')
    response = send_file_conditional(
        path, mimetype=_APK_MIME,
        download_name='tofu-android.apk', as_attachment=True)
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@api_v1_mobile_bp.route('/api/v1/mobile/android/link', methods=['GET'])
@require_auth
@api_meta(summary='Absolute download URL + QR for the local Android APK',
          tags=['mobile'])
def mobile_android_link_v1():
    from lib.qr import qr_png_data_uri

    url = _download_url()
    payload = {
        'available': _apk_path().is_file(),
        'url': url,
        'tokenEmbedded': bool(_presented_token()),
        'qr': qr_png_data_uri(url),
    }
    path = _apk_path()
    if payload['available']:
        stat = path.stat()
        payload['sizeBytes'] = stat.st_size
        payload['modifiedMs'] = int(stat.st_mtime * 1000)
    return api_ok(payload)


@api_v1_mobile_bp.route('/api/v1/mobile/android', methods=['GET'])
@require_auth
@api_meta(
    responses=media_responses('text/html'),
    summary='Scan-to-install page for the local Android APK',
          tags=['mobile'])
@raw_success_response
def mobile_android_page_v1():
    from html import escape

    from lib.qr import qr_png_data_uri

    url = _download_url()
    qr = qr_png_data_uri(url)
    path = _apk_path()
    if path.is_file():
        stat = path.stat()
        from datetime import datetime
        built = datetime.fromtimestamp(stat.st_mtime).strftime(
            '%Y-%m-%d %H:%M')
        status = (f'APK ready — {stat.st_size / (1024 * 1024):.1f} MiB, '
                  f'built {built}')
    else:
        status = ('No local build yet — run '
                  '<code>./gradlew assembleRelease</code> in android/.')
    img = (f'<img src="{qr}" width="264" height="264" alt="APK download QR">'
           if qr else
           '<p><em>QR unavailable (qrcode package missing) — use the link '
           'below.</em></p>')
    html = (
        '<!doctype html><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Tofu Android — local install</title>'
        '<style>body{font:15px/1.6 system-ui,sans-serif;margin:3em auto;'
        'max-width:26em;padding:0 1.2em;color:#222;text-align:center}'
        'h1{font-size:1.3rem}code{background:#f3f3f3;padding:.1em .4em;'
        'border-radius:3px;word-break:break-all}'
        'p.hint{color:#666;font-size:.85rem}</style>'
        '<h1>Tofu Android — local build</h1>'
        f'<p>{status}</p>{img}'
        f'<p class="hint">Same Wi-Fi as this server, scan to download.<br>'
        f'<a href="{escape(url, quote=True)}">{escape(url)}</a></p>')
    return Response(html, content_type='text/html; charset=utf-8')


# ═══════════════════════════════════════════════════════════════════════
#  Scan-to-connect: this server's own address as a phone-addable QR
# ═══════════════════════════════════════════════════════════════════════

#: Deep-link scheme the Android shell registers (see AndroidManifest.xml).
_CONNECT_DEEP_LINK = 'tofu://connect'
_MAX_CONNECT_URL = 2048
_MAX_CONNECT_NAME = 64


def _is_absolute_http_url(value: str) -> bool:
    if not value or len(value) > _MAX_CONNECT_URL:
        return False
    parsed = urlparse(value)
    return parsed.scheme in ('http', 'https') and bool(parsed.netloc)


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
        'display name) that the Android shell\'s QR scanner turns into a saved '
        'server. `url` defaults to this request\'s root, but callers behind a '
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
        return api_bad_request('url must be an absolute http(s) URL')
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
_CONNECT_PAGE_HTML = """<!doctype html>
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
   <strong>Scan QR</strong> in the app. It adds this server in one step.</p>
<img id="qr" alt="Tofu connect QR">
<p id="err" class="hint"></p>
<p class="hint">Address: <code id="u">…</code></p>
<p class="hint">Link: <code id="d">…</code></p>
<script>
(function () {
  // This page's path is `<tofu-root>/api/v1/mobile/connect`; strip at `/api/`
  // to recover the address the PHONE should dial.
  var path = location.pathname;
  var at = path.indexOf('/api/');
  var root = location.origin + (at >= 0 ? path.slice(0, at + 1) : '/');
  var q = '/api/v1/mobile/connect/link?url=' + encodeURIComponent(root);
  fetch(q, {headers: {'Accept': 'application/json'}})
    .then(function (r) { return r.json(); })
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
        'address, so a phone adds the server with one scan. The address is '
        'resolved from `location` in the browser, so the page stays correct '
        'behind any reverse-proxy prefix.'),
    tags=['mobile'],
    request_body=False,
)
@raw_success_response
def mobile_connect_page_v1():
    return Response(_CONNECT_PAGE_HTML, content_type='text/html; charset=utf-8')
