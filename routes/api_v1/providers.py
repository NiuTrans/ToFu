"""routes/api_v1/providers.py — BYO model-endpoint registration.

Routes:
  GET    /api/v1/providers              — list providers owned by the principal
  POST   /api/v1/providers              — register a new provider; auto-discovers models
  GET    /api/v1/providers/{id}         — fetch one (api_key redacted)
  PATCH  /api/v1/providers/{id}         — mutate name/url/key/models/disabled
  DELETE /api/v1/providers/{id}         — drop the provider
  POST   /api/v1/providers/{id}/probe   — re-discover models from /v1/models

Once a provider is registered, callers can pin runs to it by appending
``@<prov_id>`` to the model name in any chat / agent request:

    POST /api/v1/chat/completions
      { "model": "deepseek-v4-pro@prov_a3f2c1", ... }

Or via :py:mod:`lib.byo_providers.resolve_model_string` from inside the
process. The ``@prov_…`` suffix bypasses the global slot pool — the
dispatcher mints an ephemeral slot scoped to that single request.
"""

from __future__ import annotations

from quart import Blueprint

from lib.api_response import (
    api_bad_request, api_created, api_internal_error, api_not_found, api_ok,
)
from lib.byo_providers import (
    create_provider, delete_provider, get_provider, get_public,
    list_providers, redact, sanitise_extra_headers, update_provider,
)
from lib.log import audit_log, get_logger
from lib.openapi import api_meta
from lib.request_parser import (
    optional_bool, optional_dict, optional_list, optional_str,
    parse_body, require_str,
)

from .auth import current_auth, require_scope

logger = get_logger(__name__)

api_v1_providers_bp = Blueprint('api_v1_providers', __name__)


# Header policy lives in the domain module so every caller, including direct
# service use, crosses the same validation boundary.  This route imports the
# public function only to expose it to its own request parsing below.


# ── Routes ──────────────────────────────────────────────────────────


@api_v1_providers_bp.route('/api/v1/providers', methods=['GET'])
@require_scope('providers')
@api_meta(summary='List BYO providers',
          description='Returns providers registered against the calling '
                       'repository owner. The api_key field is redacted to a '
                       '`key_hint` (first four + last four characters).',
          tags=['providers'], scope='providers')
def list_providers_route():
    ctx = current_auth()
    if ctx is None or ctx.owner_user_id is None:
        return api_ok(providers=[])
    return api_ok(providers=list_providers(
        ctx.owner_user_id, tenant_id=ctx.tenant_id))


@api_v1_providers_bp.route('/api/v1/providers', methods=['POST'])
@require_scope('providers')
@api_meta(
    summary='Register a BYO model endpoint',
    description=(
        'Register an OpenAI-compatible LLM endpoint owned by the '
        'authenticated repository owner. Returns the provider row including the '
        'opaque `id` (e.g. `prov_a3f2c1`) which can be used to pin '
        'runs to this endpoint via the model-string convention '
        '`<model_id>@<prov_id>`.\n\n'
        'When `auto_discover=true` (default) and `models` is empty, '
        'we synchronously hit `<base_url>/v1/models` and ingest the '
        'served list. Pass the canonical model registration '
        '`models=[{model_id, capabilities, rpm, context_window, pricing}]` '
        'to skip discovery. `pricing.input` and `pricing.output` are the '
        'billable per-million-token rates; the old blended `cost` routing '
        'hint is not accepted.'),
    tags=['providers'], scope='providers',
    request_body={'required': True, 'content': {'application/json': {
        'schema': {
            'type': 'object',
            'required': ['name', 'base_url'],
            'properties': {
                'name': {'type': 'string',
                          'description': 'Human-readable label'},
                'base_url': {'type': 'string',
                              'description': 'OpenAI-compatible base URL '
                                             '(must include scheme, e.g. '
                                             '`http://10.0.0.5:8080/v1`)'},
                'api_key': {'type': 'string',
                             'description': 'Bearer credential. Empty '
                                             'string is permitted for '
                                             'unauthenticated local '
                                             'engines (vLLM / Ollama).'},
                'models': {'type': 'array',
                            'items': {
                                'type': 'object',
                                'required': ['model_id'],
                                'properties': {
                                    'model_id': {'type': 'string'},
                                    'capabilities': {
                                        'type': 'array',
                                        'items': {'type': 'string'}},
                                    'rpm': {
                                        'type': 'integer', 'minimum': 1},
                                    'context_window': {
                                        'type': 'integer', 'minimum': 1,
                                        'description': 'Maximum input context '
                                                       'window in tokens'},
                                    'pricing': {
                                        'type': 'object',
                                        'required': ['input', 'output'],
                                        'properties': {
                                            'input': {'type': 'number',
                                                      'minimum': 0},
                                            'output': {'type': 'number',
                                                       'minimum': 0},
                                            'currency': {
                                                'type': 'string',
                                                'enum': ['USD', 'CNY'],
                                                'default': 'USD'},
                                        },
                                        'description': 'Billable rates per '
                                                       'million tokens'},
                                }}},
                'extra_headers': {
                    'type': 'object',
                    'description': (
                        'Additional outbound HTTP headers, e.g. '
                        '`{"X-Internal-Tag": "..."}`. The names '
                        '`Authorization`, `x-api-key`, `Cookie`, '
                        '`Host`, `Content-Length`, `Transfer-Encoding`, '
                        'and `Proxy-Authorization` are reserved and '
                        'rejected.')},
                'auto_discover': {'type': 'boolean',
                                   'description': (
                                       'If true and `models` is empty, '
                                       'synchronously probe '
                                       '`<base_url>/v1/models` during '
                                       'registration. Default **false** '
                                       '— registration stays fast; call '
                                       '`POST /providers/{id}/probe` '
                                       'separately to ingest models.')},
                'thinking_format': {
                    'type': 'string',
                    'enum': ['', 'enable_thinking', 'thinking_type',
                              'chat_template_kwargs', 'none'],
                    'description': (
                        'Body-shape dialect for the thinking flag on '
                        'this engine. Empty string (default) means '
                        'auto-detect from model name. Set explicitly '
                        'when the auto-detect would be wrong — '
                        'most commonly for self-hosted sglang / vLLM '
                        'servers (use `chat_template_kwargs`) running '
                        'a model whose name otherwise matches a cloud '
                        'family (e.g. self-hosted Qwen3). When '
                        '`auto_discover=true` the probe sets a sane '
                        'default from `/v1/models`’ `owned_by` field, '
                        'so you usually never need to set this by hand.')},
            }}}}})
def create_provider_route():
    ctx = current_auth()
    if ctx is None or ctx.owner_user_id is None:
        return api_bad_request('caller has no repository owner identity')
    body = parse_body()
    name = require_str(body, 'name', max_len=80)
    base_url = require_str(body, 'base_url', max_len=500)
    api_key = optional_str(body, 'api_key', default='', max_len=400)
    raw_headers = optional_dict(body, 'extra_headers') or {}
    extra_headers, hdr_err = sanitise_extra_headers(raw_headers)
    if hdr_err:
        return api_bad_request(hdr_err, field='extra_headers')
    models = optional_list(body, 'models', item_type=dict, default=[]) or []
    # Default OFF — registration is fast & unconditional. Operators
    # follow up with `POST /api/v1/providers/{id}/probe` to ingest
    # /v1/models, which can be slow or hang on a wedged endpoint and
    # shouldn't block the registration round-trip.
    auto_discover = optional_bool(body, 'auto_discover', default=False)
    user_thinking_format = optional_str(
        body, 'thinking_format', default='', max_len=40)

    # Optional auto-discovery — runs BEFORE persistence so the resulting
    # row already carries the right model list, which means the dispatch
    # path in /api/v1/chat/completions can resolve `<model>@<prov>`
    # immediately without a second round-trip.
    auto_thinking_format = ''
    if not models and auto_discover:
        try:
            from lib.llm_dispatch.discovery import probe_provider
            probe = probe_provider(base_url, api_key)
            if probe.get('ok'):
                models = probe.get('models') or []
                auto_thinking_format = probe.get('thinking_format') or ''
                logger.info(
                    '[BYOProv] auto-discovered %d models from %s '
                    '(thinking_format=%r)',
                    len(models), base_url,
                    auto_thinking_format or '(unset)')
            else:
                logger.warning(
                    '[BYOProv] auto-discovery returned no models for %s: %s',
                    base_url, probe.get('error') or 'unknown')
        except Exception as e:
            logger.warning('[BYOProv] auto-discovery failed for %s: %s',
                            base_url, e, exc_info=True)
            # Fall through — the provider is still registered without
            # models; user can patch later.

    # Caller's explicit thinking_format wins over auto-detect.
    final_thinking_format = user_thinking_format or auto_thinking_format

    try:
        row = create_provider(
            owner_user_id=ctx.owner_user_id,
            tenant_id=ctx.tenant_id,
            name=name, base_url=base_url,
            api_key=api_key, models=models, extra_headers=extra_headers,
            thinking_format=final_thinking_format,
        )
    except ValueError as e:
        return api_bad_request(str(e))
    except RuntimeError as e:
        return api_bad_request(str(e), kind='quota_exceeded')
    except Exception as e:
        return api_internal_error(e, context='api_v1.providers.create')

    return api_created(provider=redact(row))


@api_v1_providers_bp.route('/api/v1/providers/<prov_id>', methods=['GET'])
@require_scope('providers')
@api_meta(summary='Get a BYO provider', tags=['providers'], scope='providers')
def get_provider_route(prov_id):
    ctx = current_auth()
    if ctx is None or ctx.owner_user_id is None:
        return api_not_found('Provider not found')
    row = get_public(
        prov_id, ctx.owner_user_id, tenant_id=ctx.tenant_id)
    if row is None:
        return api_not_found('Provider not found')
    return api_ok(provider=row)


@api_v1_providers_bp.route('/api/v1/providers/<prov_id>', methods=['PATCH'])
@require_scope('providers')
@api_meta(summary='Update a BYO provider', tags=['providers'],
          scope='providers')
def update_provider_route(prov_id):
    ctx = current_auth()
    if ctx is None or ctx.owner_user_id is None:
        return api_not_found('Provider not found')
    body = parse_body()
    fields = {}
    for k in ('name', 'base_url', 'api_key'):
        if k in body:
            fields[k] = optional_str(body, k, default='', max_len=500)
    if 'models' in body:
        fields['models'] = optional_list(body, 'models',
                                          item_type=dict, default=[]) or []
    if 'extra_headers' in body:
        clean_h, hdr_err = sanitise_extra_headers(
            optional_dict(body, 'extra_headers') or {})
        if hdr_err:
            return api_bad_request(hdr_err, field='extra_headers')
        fields['extra_headers'] = clean_h
    if 'thinking_format' in body:
        fields['thinking_format'] = optional_str(
            body, 'thinking_format', default='', max_len=40)
    if 'disabled' in body:
        fields['disabled'] = optional_bool(body, 'disabled', default=False)
    if not fields:
        return api_bad_request('No updatable fields provided')
    try:
        ok = update_provider(
            prov_id, ctx.owner_user_id, tenant_id=ctx.tenant_id, **fields)
    except ValueError as e:
        return api_bad_request(str(e))
    if not ok:
        return api_not_found('Provider not found')
    return api_ok(provider=get_public(
        prov_id, ctx.owner_user_id, tenant_id=ctx.tenant_id))


@api_v1_providers_bp.route('/api/v1/providers/<prov_id>', methods=['DELETE'])
@require_scope('providers')
@api_meta(summary='Delete a BYO provider', tags=['providers'],
          scope='providers')
def delete_provider_route(prov_id):
    ctx = current_auth()
    if ctx is None or ctx.owner_user_id is None:
        return api_not_found('Provider not found')
    if not delete_provider(
            prov_id, ctx.owner_user_id, tenant_id=ctx.tenant_id):
        return api_not_found('Provider not found')
    audit_log('byo_provider_deleted_route', prov_id=prov_id,
              owner_user_id=ctx.owner_user_id)
    return api_ok({'deleted': prov_id})


@api_v1_providers_bp.route('/api/v1/providers/<prov_id>/probe',
                             methods=['POST'])
@require_scope('providers')
@api_meta(summary='Re-discover models for a BYO provider',
          description='Hits `<base_url>/v1/models` and replaces the '
                       'stored model list with the result.',
          tags=['providers'], scope='providers')
def probe_provider_route(prov_id):
    ctx = current_auth()
    if ctx is None or ctx.owner_user_id is None:
        return api_not_found('Provider not found')
    row = get_provider(
        prov_id, ctx.owner_user_id, tenant_id=ctx.tenant_id)
    if row is None:
        return api_not_found('Provider not found')
    try:
        from lib.llm_dispatch.discovery import probe_provider
        probe = probe_provider(row['base_url'], row.get('api_key') or '')
        if not probe.get('ok'):
            return api_bad_request(
                probe.get('error') or 'Probe returned no models',
                kind='probe_empty')
        models = probe.get('models') or []
        auto_thinking_format = probe.get('thinking_format') or ''
    except Exception as e:
        logger.error('[BYOProv] probe failed for %s: %s', prov_id, e,
                      exc_info=True)
        return api_internal_error(e, context='api_v1.providers.probe')

    # Persist the new model list AND the auto-detected thinking_format.
    # Operators who already pinned a custom value via PATCH keep theirs:
    # we only overwrite when the row currently has '' (empty/auto).
    update_fields: dict = {'models': models}
    if auto_thinking_format and not (row.get('thinking_format') or ''):
        update_fields['thinking_format'] = auto_thinking_format
    update_provider(
        prov_id, ctx.owner_user_id, tenant_id=ctx.tenant_id, **update_fields)
    return api_ok(provider=get_public(
                       prov_id, ctx.owner_user_id, tenant_id=ctx.tenant_id),
                   discovered_count=len(models),
                   thinking_format_applied=update_fields.get(
                       'thinking_format', row.get('thinking_format', '')))


__all__ = ['api_v1_providers_bp', 'sanitise_extra_headers']
