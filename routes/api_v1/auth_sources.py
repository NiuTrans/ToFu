"""routes/api_v1/auth_sources.py — Authenticated-fetch source management.

These endpoints let the user connect login-walled sites (Xiaohongshu / RED,
…) so the server-side fetch pipeline can read them by replaying the user's
logged-in browser session (cookies + optional proxy) through Playwright.
See ``lib/auth_sources.py`` for the storage model and ``lib/fetch/core.py``
for how a matched source short-circuits the anonymous fetch path.

Routes:
  GET    /api/v1/auth-sources                 — list configured sources (redacted)
  POST   /api/v1/auth-sources                 — create/update one (cookies/proxy/label)
  POST   /api/v1/auth-sources/<domain>/toggle — enable/disable
  DELETE /api/v1/auth-sources/<domain>        — delete (defaults reset, not removed)
  POST   /api/v1/auth-sources/<domain>/login  — interactive headful login (auto-capture cookies)

The interactive-login route launches a *non-headless* Playwright browser on
the SERVER host, navigates to the site's login page, and waits (bounded) for
the user to log in; on success it captures ``storage_state`` cookies and
persists them. This only works where the server has a display — the manual
cookie-paste path (POST with ``cookie_header``) is the universal fallback.
"""

from __future__ import annotations

from flask import Blueprint

from lib.api_response import api_bad_request, api_error, api_internal_error, api_ok
from lib.log import get_logger
from lib.openapi import api_meta
from lib.request_parser import parse_body

from .auth import require_auth

logger = get_logger(__name__)

api_v1_auth_sources_bp = Blueprint('api_v1_auth_sources', __name__)





@api_v1_auth_sources_bp.route('/api/v1/auth-sources', methods=['GET'])
@require_auth
@api_meta(
    summary='List authenticated-fetch sources',
    description=(
        'Returns ``{sources: [...]}``. Each entry is redacted: cookie '
        'values and proxy credentials are never echoed — only '
        '``cookie_count`` / ``has_cookies`` / ``has_proxy`` / ``proxy_hint`` '
        'plus ``domain`` / ``label`` / ``enabled`` / ``updated_at``.'
    ),
    tags=['capabilities'],
)
def list_auth_sources():
    from lib.auth_sources import list_sources
    return api_ok({'sources': list_sources()})


@api_v1_auth_sources_bp.route('/api/v1/auth-sources', methods=['POST'])
@require_auth
@api_meta(
    summary='Create or update an authenticated-fetch source',
    description=(
        'Body: ``{domain, label?, enabled?, cookie_fields?, cookie_header?, '
        'proxy?, aliases?}``. ``cookie_fields`` is a ``{cookie_name: value}`` '
        'mapping (the structured path the Settings UI uses — one input per '
        'cookie, no delimiters for the user to mistype); ``cookie_header`` is '
        'a raw devtools ``Cookie:`` string. Either replaces the stored '
        'cookies, and a payload omitting a cookie the catalog marks '
        '``required`` is rejected with 400. Returns the redacted row.'
    ),
    tags=['capabilities'],
)
def upsert_auth_source():
    from lib.auth_sources import upsert_source

    data = parse_body()
    domain = (data.get('domain') or '').strip()
    if not domain:
        return api_bad_request('domain is required', field='domain')
    cookie_fields = data.get('cookie_fields')
    if cookie_fields is not None and not isinstance(cookie_fields, dict):
        return api_bad_request('cookie_fields must be an object',
                               field='cookie_fields')
    try:
        row = upsert_source(
            domain,
            label=data.get('label'),
            enabled=data.get('enabled'),
            cookie_fields=cookie_fields,
            cookie_header=data.get('cookie_header'),
            proxy=data.get('proxy'),
            aliases=data.get('aliases'),
        )
    except ValueError as e:
        return api_bad_request(str(e), field='domain')
    return api_ok({'source': row})


@api_v1_auth_sources_bp.route('/api/v1/auth-sources/<domain>/toggle', methods=['POST'])
@require_auth
@api_meta(summary='Enable/disable an authenticated-fetch source', tags=['capabilities'])
def toggle_auth_source(domain):
    from lib.auth_sources import set_enabled

    data = parse_body()
    enabled = bool(data.get('enabled', True))
    if not set_enabled(domain, enabled):
        return api_error(f'Unknown source: {domain}', status=404)
    return api_ok({'domain': domain, 'enabled': enabled})


@api_v1_auth_sources_bp.route('/api/v1/auth-sources/<domain>', methods=['DELETE'])
@require_auth
@api_meta(
    summary='Delete an authenticated-fetch source',
    description='Default-catalog domains are reset (disabled + cookies cleared) rather than removed.',
    tags=['capabilities'],
)
def delete_auth_source(domain):
    from lib.auth_sources import delete_source
    if not delete_source(domain):
        return api_error(f'Unknown source: {domain}', status=404)
    return api_ok({'domain': domain})


@api_v1_auth_sources_bp.route('/api/v1/auth-sources/<domain>/login', methods=['POST'])
@require_auth
@api_meta(
    summary='Interactive login to auto-capture cookies (headful browser)',
    description=(
        'Launches a non-headless browser on the SERVER host pointed at the '
        "site's login page and waits (bounded) for the user to sign in, "
        'then captures + stores the session cookies. Requires the server '
        'to have a display + a non-headless-capable Playwright. Returns '
        '``503`` when interactive login is unavailable — fall back to '
        'pasting a cookie header via POST /api/v1/auth-sources.'
    ),
    tags=['capabilities'],
)
def interactive_login(domain):
    from lib.auth_sources import normalize_domain, source_spec

    dom = normalize_domain(domain)
    if not dom:
        return api_bad_request('domain is required', field='domain')
    login_url = source_spec(dom).get('login_url') or f'https://{dom}/'
    data = parse_body()
    timeout_s = int(data.get('timeout') or 180)
    timeout_s = max(30, min(timeout_s, 600))

    try:
        from tofu_search.fetch.interactive_login import capture_login_cookies
    except Exception as e:
        logger.error('[AuthSrc.v1] interactive login module unavailable: %s', e, exc_info=True)
        return api_internal_error(e, source='api_v1.auth_sources.login')

    try:
        result = capture_login_cookies(dom, login_url, timeout_s=timeout_s)
    except Exception as e:
        logger.error('[AuthSrc.v1] interactive login crashed for %s: %s', dom, e, exc_info=True)
        return api_internal_error(e, source='api_v1.auth_sources.login')

    if not result.get('ok'):
        # Unavailable (no display / headless-only) → 503 so the UI can
        # cleanly fall back to the manual paste path.
        code = 503 if result.get('reason') == 'unavailable' else 400
        return api_error(result.get('error', 'login failed'), status=code,
                         reason=result.get('reason', ''))

    # tofu-search's capture_login_cookies RETURNS the cookies (it has no
    # knowledge of chatui's auth-source store); persist them here.
    source_row = None
    cookies = result.get('cookies') or []
    if cookies:
        try:
            from lib.auth_sources import upsert_source
            source_row = upsert_source(dom, enabled=True, cookies=cookies)
        except Exception as e:
            logger.error('[AuthSrc.v1] failed to persist captured cookies for %s: %s',
                         dom, e, exc_info=True)
            return api_internal_error(e, source='api_v1.auth_sources.login')

    logger.info('[AuthSrc.v1] interactive login captured %d cookie(s) for %s',
                result.get('cookie_count', 0), dom)
    return api_ok({'domain': dom, 'cookie_count': result.get('cookie_count', 0),
                   'source': source_row})


@api_v1_auth_sources_bp.route('/api/v1/auth-sources/cookie-consent/pending', methods=['GET'])
@require_auth
@api_meta(
    summary='List pending cookie-capture consent prompts',
    description=(
        'Returns ``{pending: [{id, domain, url, created_at}]}`` — consent '
        'prompts fired by the login-wall capture chain that no client has '
        'answered yet. The frontend polls this on load so a banner survives '
        'a page reload (the push frame alone would be missed).'
    ),
    tags=['capabilities'],
)
def cookie_consent_pending():
    from lib.browser.cookie_capture import pending_consents
    return api_ok({'pending': pending_consents()})


@api_v1_auth_sources_bp.route('/api/v1/auth-sources/cookie-consent/resolve', methods=['POST'])
@require_auth
@api_meta(
    summary='Resolve a cookie-capture consent prompt',
    description=(
        'Body: ``{id, approved}``. A grant is persisted per-domain (one-time '
        '— later walls for the same domain capture without re-asking); a '
        'denial suppresses re-asking for a cooldown.'
    ),
    tags=['capabilities'],
)
def cookie_consent_resolve():
    from lib.browser.cookie_capture import resolve_consent

    data = parse_body()
    consent_id = (data.get('id') or '').strip()
    if not consent_id:
        return api_bad_request('id is required', field='id')
    if not resolve_consent(consent_id, bool(data.get('approved'))):
        return api_error(f'Unknown or expired consent prompt: {consent_id}', status=404)
    return api_ok({'id': consent_id, 'approved': bool(data.get('approved'))})


@api_v1_auth_sources_bp.route('/api/v1/auth-sources/cookie-consent/grants', methods=['GET'])
@require_auth
@api_meta(
    summary='List per-domain cookie-capture grants',
    description='Returns ``{grants: [{domain, granted_at}]}`` (no cookie data).',
    tags=['capabilities'],
)
def cookie_consent_grants():
    from lib.browser.cookie_capture import consent_grants
    return api_ok({'grants': consent_grants()})


@api_v1_auth_sources_bp.route('/api/v1/auth-sources/cookie-consent/<domain>', methods=['DELETE'])
@require_auth
@api_meta(
    summary='Revoke a cookie-capture consent grant',
    description='The next login wall for this domain asks for consent again.',
    tags=['capabilities'],
)
def cookie_consent_revoke(domain):
    from lib.browser.cookie_capture import revoke_consent
    if not revoke_consent(domain):
        return api_error(f'No consent grant for: {domain}', status=404)
    return api_ok({'domain': domain})


__all__ = ['api_v1_auth_sources_bp']
