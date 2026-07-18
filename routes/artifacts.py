"""routes/artifacts.py — REST + raw-render endpoints for chat artifacts.

The frontend ArtifactPanel consumes:

  GET    /api/artifacts/<id>          — metadata JSON (no content)
  GET    /api/artifacts/<id>/raw      — raw bytes (text/markdown | text/html | image/svg+xml)
                                        Always served with a strict CSP so
                                        that even a top-level navigation
                                        sandboxes scripts / forms / etc.
  GET    /api/artifacts/conv/<id>     — list metadata rows for a conv
  POST   /api/artifacts/<id>/pin      — toggle pin flag
  DELETE /api/artifacts/<id>          — soft-delete

Security invariants (also covered in tests/test_artifacts_api.py):

  • /raw never sets ``X-Frame-Options: DENY`` — the iframe is the
    intended container.  Instead, we rely on ``Content-Security-Policy:
    sandbox`` to neutralize scripts and forms, and on the iframe's own
    ``sandbox`` attribute on the consumer side.
  • ``X-Content-Type-Options: nosniff`` prevents MIME confusion attacks.
  • ``Referrer-Policy: no-referrer`` so embedded assets don't leak the
    artifact URL.
  • The route does NOT issue ``Content-Disposition: attachment`` — the
    panel renders inline.
"""

from __future__ import annotations

import re
from urllib.parse import quote as _urlquote

from flask import Blueprint, Response, request

from lib.artifacts import (
    ArtifactNotFoundError,
    get_artifact,
    get_artifact_meta,
)
from lib.log import audit_log, get_logger
from lib.api_response import api_bad_request, api_error, api_internal_error, api_not_found

logger = get_logger(__name__)

artifacts_bp = Blueprint('artifacts', __name__)


# ── Format → response Content-Type mapping (used by /raw) ────────────
_FORMAT_CONTENT_TYPE = {
    'markdown': 'text/markdown; charset=utf-8',
    'html':     'text/html; charset=utf-8',
    'svg':      'image/svg+xml; charset=utf-8',
}

# CSP for the /raw endpoint.  ``sandbox`` (no allow-scripts) means scripts
# are blocked even in a top-level navigation.  ``default-src 'none'``
# blocks all resource loads — model HTML cannot reach back to your origin
# for an internal-only iframe leak.  ``style-src 'unsafe-inline'`` and
# ``img-src data: https:`` are the only relaxations: visual reports
# routinely use inline CSS and remote/data: images, and neither vector
# can run code under sandbox.
_CSP_HEADER = (
    "sandbox; "
    "default-src 'none'; "
    "style-src 'unsafe-inline' https:; "
    "img-src data: https:; "
    "font-src data: https:; "
    "media-src data: https:"
)

_COMMON_SECURITY_HEADERS = {
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy':        'no-referrer',
    'Cache-Control':          'private, max-age=300',
}

# CSP for the /view endpoint.  Same-origin scripts ('self') ARE allowed
# so that our KaTeX bundle + tofu-auto-render.js can run.  Model
# `<script>` blocks and inline `on*=` handlers do NOT run because:
#   1. ``script-src 'self'`` forbids inline + remote scripts even if
#      they appear in the page source.
#   2. The iframe in artifacts.js sets ``sandbox="allow-scripts"``
#      WITHOUT ``allow-same-origin``, so the document is an opaque
#      origin → cookies/localStorage of the parent are unreachable.
# See lib/artifacts/__init__.py for the full security invariant list.
_VIEW_CSP_HEADER = (
    "sandbox allow-scripts; "
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src data: https: 'self'; "
    "font-src 'self' data:; "
    "media-src data: https:; "
    "frame-ancestors 'self'"
)


# ── HTML view template ───────────────────────────────────────────────
# Wraps an HTML / SVG artifact with our trusted KaTeX bundle.  KaTeX
# loads first (same-origin, allowed by CSP), then tofu-auto-render
# (also same-origin) walks the document and converts $..$ / $$..$$ /
# \(..\) / \[..\] in the model body.  No model script runs.

import os as _os
import re as _re_mod

_BASE_DIR = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))


def _strip_model_scripts(html: str) -> str:
    """Remove `<script>` blocks before injecting model HTML — defense in
    depth on top of the CSP that already disallows their execution.

    Inline ``on*=`` attributes and ``javascript:`` href values are NOT
    stripped here because the CSP forbids their execution; sanitizing
    them risks corrupting otherwise-legitimate text content.
    """
    if not html:
        return ''
    return _re_mod.sub(
        r'<script\b[^>]*>.*?</script>',
        '',
        html,
        flags=_re_mod.IGNORECASE | _re_mod.DOTALL,
    )


def _build_artifact_view_html(artifact: dict) -> str:
    """Translate an HTML/SVG artifact into a view-mode HTML document.

    For full ``<html>`` documents we inject KaTeX + auto-render into the
    existing head; for fragments we wrap them in a minimal template.
    Either way, the output is a single self-contained HTML document
    served from /api/artifacts/<id>/view.
    """
    body = artifact.get('content') or ''
    title = (artifact.get('title') or 'Artifact').replace('<', '').replace('>', '')[:200]
    fmt = artifact.get('format')
    body = _strip_model_scripts(body)

    katex_css = '/static/vendor/katex/katex.min.css'
    katex_js = '/static/vendor/katex/katex.min.js'
    autorender_js = '/static/vendor/katex/tofu-auto-render.js'

    head_inject = (
        f'<link rel="stylesheet" href="{katex_css}">'
        f'<script defer src="{katex_js}"></script>'
        f'<script defer src="{autorender_js}"></script>'
    )

    # SVG → embed in a wrapper.  KaTeX in SVG is unusual but harmless.
    if fmt == 'svg':
        return (
            '<!doctype html><html><head>'
            '<meta charset="utf-8">'
            f'<title>{title}</title>'
            '<style>html,body{margin:0;padding:0;background:#fff;'
            'display:flex;align-items:center;justify-content:center;'
            'min-height:100vh}svg{max-width:100%;max-height:100vh}</style>'
            f'{head_inject}'
            '</head><body>'
            f'{body}'
            '</body></html>'
        )

    # HTML — distinguish full documents from fragments.
    is_full_doc = bool(_re_mod.search(r'<html[\s>]|<!doctype\s+html', body, _re_mod.IGNORECASE))
    if is_full_doc:
        # Inject our head additions just before </head> (or before <body>
        # if no </head>).  Falls through to wrap mode if neither found.
        for marker in ('</head>', '<body', '<BODY'):
            idx = body.lower().find(marker.lower())
            if idx != -1:
                return body[:idx] + head_inject + body[idx:]
        return head_inject + body  # last resort

    # Fragment — wrap in a minimal template.
    return (
        '<!doctype html><html><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{title}</title>'
        '<style>'
        'html,body{margin:0;padding:0}'
        'body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Helvetica Neue",Arial,'
        '"Noto Sans CJK SC",sans-serif;color:#111;line-height:1.6;padding:20px}'
        'pre,code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}'
        'pre{background:#f4f4f8;padding:10px;border-radius:6px;overflow:auto}'
        'a{color:#5b4dd1;text-decoration:none}'
        'a:hover{text-decoration:underline}'
        '</style>'
        f'{head_inject}'
        '</head><body>'
        f'{body}'
        '</body></html>'
    )


def _strip_meta_for_response(meta: dict) -> dict:
    """Return a copy with internal-only fields removed.

    Delegates to the canonical whitelist filter in lib.artifacts so the
    public field set lives in ONE place.
    """
    from lib.artifacts import public_meta
    return public_meta(meta)


# HTTP headers are ISO-8859-1 by spec — model-supplied titles routinely
# contain CJK/emoji which would crash the response writer with
# ``UnicodeEncodeError`` (we hit this once with a Chinese title in
# production).  RFC 5987 ``filename*=UTF-8''<percent-encoded>`` is the
# universal cure: keep an ASCII-only ``filename="..."`` for old browsers
# AND emit ``filename*`` for everything else.  Quotes are stripped from
# the ASCII fallback to avoid breaking the filename quote pair.
_NON_ASCII_RE = re.compile(r'[^\x20-\x7e]')


def _content_disposition(disposition: str, filename: str) -> str:
    """Build a header-safe ``Content-Disposition`` value.

    Args:
        disposition: ``'inline'`` or ``'attachment'``.
        filename: Display name (any UTF-8); emoji/CJK supported.
    """
    safe = (filename or '').replace('"', '').replace('\\', '')
    ascii_fallback = _NON_ASCII_RE.sub('_', safe).strip(' .') or 'artifact'
    encoded = _urlquote(safe.encode('utf-8'), safe='')
    return f"{disposition}; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded}"


# GET /api/artifacts/<id> moved to routes/api_v1/artifacts.py.


@artifacts_bp.route('/api/artifacts/<artifact_id>/view', methods=['GET'])
def api_get_artifact_view(artifact_id):
    """HTML/SVG artifact wrapped with our trusted KaTeX so math renders
    even when model scripts are blocked.

    Defaults the iframe sandbox to running OUR scripts only (KaTeX +
    auto-render), via a strict CSP:

        sandbox allow-scripts;
        default-src 'self';
        script-src 'self';
        style-src  'self' 'unsafe-inline';
        img-src    data: https: 'self';
        font-src   'self' data:;
        media-src  data: https:;
        frame-ancestors 'self';

    Notes:
      * Model `<script>` and inline `on*=` handlers don't run because
        the CSP `script-src` is `'self'` — only same-origin scripts
        execute, and the model HTML isn't same-origin from the iframe's
        perspective (sandbox makes it an opaque origin).  In practice
        the CSP forbids inline scripts AND remote scripts, so even if
        the iframe weren't sandboxed the model JS would still be blocked.
      * KaTeX + auto-render are loaded from `/static/vendor/katex/`,
        which is `'self'` and therefore allowed.
      * Markdown is not served via this endpoint — frontend already
        renders markdown with our marked + DOMPurify pipeline.
    """
    try:
        artifact = get_artifact(artifact_id)
    except ArtifactNotFoundError:
        logger.debug('[Artifacts] view lookup miss id=%s', artifact_id[:8])
        return api_not_found('not_found')

    fmt = artifact['format']
    if fmt not in ('html', 'svg'):
        # /view doesn't add value for markdown; redirect callers to /raw
        return api_error('unsupported_format_for_view', status=400, format=fmt)

    body_html = _build_artifact_view_html(artifact)
    resp = Response(body_html, mimetype='text/html; charset=utf-8', status=200)
    resp.headers['Content-Type']            = 'text/html; charset=utf-8'
    resp.headers['Content-Security-Policy'] = _VIEW_CSP_HEADER
    for k, v in _COMMON_SECURITY_HEADERS.items():
        resp.headers[k] = v
    fname = artifact.get('title') or f'artifact-{artifact_id[:8]}'
    resp.headers['Content-Disposition'] = _content_disposition('inline', fname)
    return resp


@artifacts_bp.route('/api/artifacts/<artifact_id>/raw', methods=['GET'])
def api_get_artifact_raw(artifact_id):
    """Return the raw bytes with a strict CSP for safe iframe embedding."""
    try:
        artifact = get_artifact(artifact_id)
    except ArtifactNotFoundError:
        logger.debug('[Artifacts] raw lookup miss id=%s', artifact_id[:8])
        return api_not_found('not_found')

    fmt = artifact['format']
    content_type = _FORMAT_CONTENT_TYPE.get(fmt, 'text/plain; charset=utf-8')
    body = artifact['content']
    resp = Response(body, mimetype=content_type, status=200)
    resp.headers['Content-Type']             = content_type
    resp.headers['Content-Security-Policy']  = _CSP_HEADER
    for k, v in _COMMON_SECURITY_HEADERS.items():
        resp.headers[k] = v
    # Filename hint for "save as" — title may be empty, keep the id as
    # fallback so the browser doesn't default to a generic ``raw``.
    # ``_content_disposition`` is RFC 5987-safe for non-ASCII titles.
    fname = artifact.get('title') or f'artifact-{artifact_id[:8]}'
    resp.headers['Content-Disposition'] = _content_disposition('inline', fname)
    return resp


# Conv-list / pin / delete / versions / library / scan moved to
# routes/api_v1/artifacts.py.


@artifacts_bp.route('/api/artifacts/<artifact_id>/export', methods=['GET'])
def api_export_artifact(artifact_id):
    """Export an artifact as ``?format=pdf`` (Playwright-rendered)."""
    fmt = (request.args.get('format') or 'pdf').lower()
    if fmt != 'pdf':
        return api_bad_request(f'unsupported export format: {fmt}')

    try:
        from lib.artifacts.pdf_export import PdfRenderError, render_artifact_pdf
    except Exception as e:
        logger.warning('[Artifacts] PDF subsystem import failed: %s', e, exc_info=True)
        return api_error('pdf_subsystem_unavailable', status=503)

    try:
        pdf_bytes = render_artifact_pdf(artifact_id)
    except ArtifactNotFoundError:
        return api_not_found('not_found')
    except PdfRenderError as e:
        logger.info('[Artifacts] PDF export failed for id=%s: %s',
                    artifact_id[:8], e)
        return api_error('pdf_render_failed', status=503, detail=str(e))
    except Exception as e:
        logger.error('[Artifacts] PDF export crashed for id=%s: %s',
                     artifact_id[:8], e, exc_info=True)
        return api_internal_error('internal')

    try:
        audit_log('artifact_export_pdf',
                  artifact_id=artifact_id, bytes=len(pdf_bytes),
                  ip=request.remote_addr)
    except Exception as e:
        logger.debug('[Artifacts] audit_log artifact_export_pdf failed: %s', e)

    try:
        meta = get_artifact_meta(artifact_id)
        title = meta.get('title') or f'artifact-{artifact_id[:8]}'
    except ArtifactNotFoundError as e:
        logger.debug('[Artifacts] PDF export title fallback for id=%s: %s',
                     artifact_id[:8], e)
        title = f'artifact-{artifact_id[:8]}'
    if not title.lower().endswith('.pdf'):
        title = title + '.pdf'

    resp = Response(pdf_bytes, mimetype='application/pdf', status=200)
    resp.headers['Content-Type'] = 'application/pdf'
    resp.headers['Content-Disposition'] = _content_disposition('attachment', title)
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['Referrer-Policy'] = 'no-referrer'
    return resp


# scan moved to routes/api_v1/artifacts.py
