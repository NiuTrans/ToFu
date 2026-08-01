"""routes/config.py — Server configuration API endpoints.

Extracted from routes/common.py for better separation of concerns.
Handles provider management, model discovery, Feishu config, proxy settings.
"""

import json
import os
import sys

from flask import jsonify, request

from lib.config_dir import config_path as _config_path
from lib.log import get_logger
from lib.api_response import api_bad_request, api_error, api_internal_error, api_ok, safe_route
from lib.request_parser import parse_body

logger = get_logger(__name__)

from routes.api_v1.config import api_v1_config_bp as config_bp  # noqa: E402
# (alias kept for back-compat: server.py + routes/upload.py import _read_server_config from routes.config)

_SERVER_CONFIG_PATH = _config_path('server_config.json')


# ══════════════════════════════════════════════════════
#  Config File I/O
# ══════════════════════════════════════════════════════

def _read_server_config():
    """Read server_config.json and return as dict (empty dict on failure)."""
    from lib.json_store import read_json
    cfg = read_json(_SERVER_CONFIG_PATH, default={})
    return cfg if isinstance(cfg, dict) else {}


def _write_server_config(data):
    """Atomically replace server_config.json (locked). Returns True/False.

    Back-compat whole-file writer. Callers that read-modify-write should
    prefer the mutator form of ``update_json_atomic`` (see
    ``save_server_config``) so the RMW is serialised against the background
    writers (context_limits / model_info / dispatcher / health_local).
    """
    try:
        from lib.json_store import update_json_atomic
        update_json_atomic(_SERVER_CONFIG_PATH, lambda _cur: data, default={})
        logger.info('[ServerConfig] Saved server_config.json')
        return True
    except Exception as e:
        logger.error('[ServerConfig] Failed to write server_config.json: %s', e, exc_info=True)
        return False


# ══════════════════════════════════════════════════════
#  Provider defaults + pricing-tier tags
#  → moved to lib/provider_defaults.py (2026-06). Re-exported with the
#    legacy private names for the existing call sites in this module.
# ══════════════════════════════════════════════════════

from lib.provider_defaults import (  # noqa: E402
    build_default_providers as _build_default_providers,
    reeval_cheap_tags as _reeval_cheap_tags,
)
from lib.provider_balance import normalize_balance as _normalize_balance  # noqa: E402


# ══════════════════════════════════════════════════════
#  Feishu Helpers
# ══════════════════════════════════════════════════════

def _get_feishu_config(saved_config: dict) -> dict:
    """Build Feishu config for the settings UI."""
    from lib.feishu._state import ALLOWED_USERS as ENV_ALLOWED_USERS
    from lib.feishu._state import APP_ID as ENV_APP_ID
    from lib.feishu._state import APP_SECRET as ENV_APP_SECRET
    from lib.feishu._state import DEFAULT_PROJECT_PATH as ENV_DEFAULT_PROJECT
    from lib.feishu._state import ENABLED as ENV_ENABLED
    from lib.feishu._state import WORKSPACE_ROOT as ENV_WORKSPACE_ROOT

    saved_feishu = saved_config.get('feishu', {})
    app_id = saved_feishu.get('app_id') or ENV_APP_ID
    has_secret = bool(saved_feishu.get('app_secret') or ENV_APP_SECRET)
    enabled = ENV_ENABLED or bool(app_id and has_secret)

    return {
        'enabled': enabled,
        'app_id': app_id,
        'app_id_masked': ('***' + app_id[-4:]) if len(app_id) > 4 else ('*' * len(app_id)) if app_id else '',
        'has_secret': has_secret,
        'allowed_users': saved_feishu.get('allowed_users', sorted(ENV_ALLOWED_USERS)),
        'default_project': saved_feishu.get('default_project') or ENV_DEFAULT_PROJECT,
        'workspace_root': saved_feishu.get('workspace_root') or ENV_WORKSPACE_ROOT,
        'connected': _feishu_is_connected(),
    }


def _feishu_is_connected() -> bool:
    """Check if the Feishu bot WebSocket is currently connected."""
    try:
        from lib.feishu._state import _lark_client
        return _lark_client is not None
    except Exception as _e:
        logger.debug('[Feishu] Connection check failed: %s', _e)
        return False


# ══════════════════════════════════════════════════════
#  Endpoints
# ══════════════════════════════════════════════════════

@config_bp.route('/api/v1/server-config')
def get_server_config():
    """GET — return full server configuration."""
    import lib as _lib

    logger.debug('[ServerConfig] GET /api/server-config requested')
    saved = _read_server_config()

    if 'providers' in saved and any('models' in p for p in saved.get('providers', [])):
        providers = saved['providers']
        presets = saved.get('presets', {})
    elif 'providers' in saved and 'models_registry' in saved:
        providers = saved['providers']
        presets = saved.get('presets', {})
        old_models = saved.get('models_registry', [])
        by_prov = {}
        for m in old_models:
            pid = m.get('provider_id', 'default')
            by_prov.setdefault(pid, []).append({
                'model_id': m.get('model_id', ''),
                'aliases': m.get('aliases', []),
                'capabilities': m.get('capabilities', ['text']),
                'rpm': m.get('rpm', 30),
                'cost': m.get('cost', 0.01),
                'thinking_default': m.get('thinking_default', False),
            })
        for p in providers:
            p['models'] = by_prov.get(p['id'], [])
    else:
        providers, presets = _build_default_providers()

    # Re-evaluate cheap tags using real pricing data (fixes stale tags from
    # old discovery runs or name-heuristic fallback).
    _reeval_cheap_tags(providers)

    model_keys = [
        'LLM_MODEL', 'QWEN_MODEL',
        'GEMINI_MODEL', 'GEMINI_PRO_MODEL', 'GEMINI_PRO_PREVIEW_MODEL',
        'GEMINI_FLASH_PREVIEW_MODEL', 'MINIMAX_MODEL',
        'DOUBAO_MODEL', 'CLAUDE_SONNET_MODEL', 'IMAGE_GEN_MODEL',
    ]
    models = {}
    for k in model_keys:
        models[k] = getattr(_lib, k, '')
    models['EMBEDDING_MODELS'] = list(getattr(_lib, 'EMBEDDING_MODELS', []))
    if 'models' in saved:
        for k, v in saved['models'].items():
            models[k] = v

    search_info = {
        'fetch_top_n': getattr(_lib, 'FETCH_TOP_N', 6),
        'fetch_timeout': getattr(_lib, 'FETCH_TIMEOUT', 15),
        'max_chars_search': getattr(_lib, 'FETCH_MAX_CHARS_SEARCH', 60000),
        'max_chars_direct': getattr(_lib, 'FETCH_MAX_CHARS_DIRECT', 200000),
        'max_chars_pdf': getattr(_lib, 'FETCH_MAX_CHARS_PDF', 0),
        'max_bytes': getattr(_lib, 'FETCH_MAX_BYTES', 20 * 1024 * 1024),
        'skip_domains': sorted(getattr(_lib, 'SKIP_DOMAINS', set())),
        'llm_content_filter': getattr(_lib, 'LLM_CONTENT_FILTER_ENABLED', True),
    }
    if 'search' in saved:
        search_info.update(saved['search'])
        # Apply saved llm_content_filter on config load (page refresh / startup)
        if 'llm_content_filter' in saved['search']:
            _lib.LLM_CONTENT_FILTER_ENABLED = bool(saved['search']['llm_content_filter'])
            from lib.search_bridge import sync_search_config
            sync_search_config()

    total_keys = sum(len(p.get('api_keys', [])) for p in providers)
    total_models = sum(len(p.get('models', [])) for p in providers)
    server_info = {
        'Python': sys.version.split()[0],
        'Config Path': _SERVER_CONFIG_PATH,
        'Providers': '%d configured (%d enabled)' % (
            len(providers), sum(1 for p in providers if p.get('enabled', True))),
        'API Keys': '%d total across providers' % total_keys,
        'Models': '%d registered across providers' % total_models,
    }

    logger.info('[ServerConfig] Returning config: %d providers, %d presets, %d models total',
               len(providers), len(presets), total_models)
    feishu_info = _get_feishu_config(saved)

    dropdown_models = []
    for prov in providers:
        if not prov.get('enabled', True):
            continue
        prov_id = prov.get('id', 'default')
        prov_name = prov.get('name', prov_id)
        for m in prov.get('models', []):
            mid = m.get('model_id', '')
            # Per-model enable toggle: skip when explicitly disabled by the user
            # in Settings. Matches the dispatcher's slot-pool filter.
            if m.get('enabled') is False:
                continue
            if mid:
                dropdown_models.append({
                    'model_id': mid,
                    'brand': m.get('brand', ''),
                    'thinking_default': m.get('thinking_default', False),
                    'capabilities': m.get('capabilities', ['text']),
                    'provider_id': prov_id,
                    'provider_name': prov_name,
                })

    hidden_models = saved.get('hidden_models', [])
    hidden_ig_models = saved.get('hidden_ig_models', [])

    model_pricing = {}
    for model_name, info in getattr(_lib, 'MODEL_PRICING', {}).items():
        model_pricing[model_name] = {
            'input': info.get('input', 0),
            'output': info.get('output', 0),
            'name': info.get('name', model_name),
        }
    # Also include input/output pricing from provider models (e.g. from discovery enrichment)
    for prov in providers:
        for m in prov.get('models', []):
            mid = m.get('model_id', '')
            if mid and mid not in model_pricing:
                # Two on-model shapes carry the same data: top-level
                # input_price/output_price (discovery enrichment) and the
                # nested pricing{input,output} dict (user/template override,
                # also registered into PROVIDER_PRICING below). Both should
                # feed the frontend pricing cache or a user-set price stays
                # invisible until some other mechanism fills the row.
                _pinfo = m.get('pricing') if isinstance(m.get('pricing'), dict) else {}
                _inp = m.get('input_price')
                if _inp is None:
                    _inp = _pinfo.get('input')
                _out = m.get('output_price')
                if _out is None:
                    _out = _pinfo.get('output')
                if _inp is not None and _out is not None:
                    model_pricing[mid] = {
                        'input': _inp,
                        'output': _out,
                        'name': mid,
                    }

    # ── Per-provider pricing overrides ──
    # Each provider's models may carry an explicit 'pricing' dict. We:
    #   1. Register it into PROVIDER_PRICING so backend cost calc honors it.
    #   2. Surface it to the frontend as ``provider_pricing`` so the UI's
    #      calcCostCny() uses the same numbers when a message has a known
    #      provider_id.
    # Templates that omit 'pricing' transparently fall back to the global
    # MODEL_PRICING table — this stays compatible with all existing templates.
    from lib.pricing import (
        clear_provider_pricing as _clear_pp,
        set_provider_pricing as _set_pp,
    )
    provider_pricing = {}
    for prov in providers:
        pid = prov.get('id') or prov.get('key')
        if not pid:
            continue
        _clear_pp(pid)
        per_model = {}
        for m in prov.get('models', []):
            mid = m.get('model_id', '')
            pinfo = m.get('pricing')
            if not (mid and isinstance(pinfo, dict)):
                continue
            if pinfo.get('input') is None or pinfo.get('output') is None:
                continue
            entry = {
                'input': pinfo['input'],
                'output': pinfo['output'],
                'name': pinfo.get('name', mid),
            }
            for k in ('cacheWriteMul', 'cacheReadMul', 'context_tiers'):
                if k in pinfo:
                    entry[k] = pinfo[k]
            _set_pp(pid, mid, entry)
            per_model[mid] = entry
        if per_model:
            provider_pricing[pid] = per_model

    model_limits = saved.get('model_limits', {})
    model_context_limits = saved.get('model_context_limits', {})
    model_defaults = {
        'fallback_model': getattr(_lib, 'FALLBACK_MODEL', ''),
        'default_model': getattr(_lib, 'LLM_MODEL', ''),
    }
    model_defaults.update(saved.get('model_defaults', {}))

    from lib.proxy import get_proxy_config
    _pc = get_proxy_config()
    network_info = {
        'http_proxy': _pc['http_proxy'],
        'https_proxy': _pc['https_proxy'],
        'env_http_proxy': _pc['env_http_proxy'],
        'env_https_proxy': _pc['env_https_proxy'],
        'proxy_configured': _pc['configured'],
        'proxy_bypass_domains': saved.get('proxy_bypass_domains', []),
        'env_proxy_bypass': os.environ.get('PROXY_BYPASS_DOMAINS', ''),
    }
    try:
        from lib.netpath import status_summary as _np_status
        network_info['netpath'] = _np_status()
    except Exception as _e:
        logger.debug('get server config: failed (%s)', _e)
        pass

    # Machine translation provider config
    mt_provider_cfg = getattr(_lib, 'MT_PROVIDER_CONFIG', {})
    mt_provider_info = {
        'provider': mt_provider_cfg.get('provider', 'niutrans'),
        'api_url': mt_provider_cfg.get('api_url', ''),
        'api_key': mt_provider_cfg.get('api_key', ''),
        'app_id': mt_provider_cfg.get('app_id', ''),
        'enabled': mt_provider_cfg.get('enabled', False),
    }

    # Upload-shrink policy — single source of truth for image re-encode rules.
    # Frontend compressImage() reads this to mirror the backend exactly, so
    # the browser doesn't double-shrink what the server would have kept.
    try:
        from routes.upload import get_upload_policy
        upload_policy = get_upload_policy()
    except Exception as e:
        logger.warning('[ServerConfig] upload policy unavailable: %s', e)
        upload_policy = {}

    # Context-window policy — single source of truth for the Context Health
    # Bar (static/js/context-bar.js). The bar used to hard-code a copy of the
    # limit table + compaction thresholds, which silently drifted from the
    # Python constants (e.g. 0.82 vs the real 0.90). Now it reads these.
    # ``per_model`` carries the resolved limit (static preset + auto-learned
    # override) for every model in the dropdown, so the gauge never re-derives.
    try:
        from lib.tasks_pkg.compaction import (
            build_context_policy, resolve_model_context_limit,
        )
        context_policy = build_context_policy()
        per_model = {}
        for dm in dropdown_models:
            mid = dm.get('model_id', '')
            if not mid or mid in per_model:
                continue
            per_model[mid] = resolve_model_context_limit(
                mid, dm.get('provider_id', '') or '')
        context_policy['per_model'] = per_model
    except Exception as e:
        logger.warning('[ServerConfig] context policy unavailable: %s', e)
        context_policy = {}

    # Translation policy — single source of truth for the frontend's
    # stale-partial-translation heuristic (was hard-coded 0.15 in
    # static/js/translation.js). See lib/text_lang.stale_translation_policy().
    try:
        from lib.text_lang import stale_translation_policy
        translation_policy = stale_translation_policy()
    except Exception as e:
        logger.warning('[ServerConfig] translation policy unavailable: %s', e)
        translation_policy = {}

    # Language-detection cascade policy — single source of truth for the
    # Tier-1→Tier-2 escalation thresholds. See lib/text_lang.detect_language_policy().
    try:
        from lib.text_lang import detect_language_policy
        lang_detect_policy = detect_language_policy()
    except Exception as e:
        logger.warning('[ServerConfig] lang-detect policy unavailable: %s', e)
        lang_detect_policy = {}

    # Capability classification (single source of truth) — the frontend
    # reads this at boot to filter chat-model pickers, so ASR-only /
    # image-gen / embedding models don't leak into the model dropdown.
    try:
        from lib.model_info.capability_taxonomy import taxonomy_payload
        capability_taxonomy = taxonomy_payload()
    except Exception as e:
        logger.warning('[ServerConfig] capability taxonomy unavailable: %s', e)
        capability_taxonomy = {}

    # Model entries the dispatcher REFUSED to register because their wire face
    # could not be resolved safely (a Claude model on a dual-face gateway whose
    # provider declares no anthropic face). Surfaced because a refusal the user
    # cannot see is its own silent failure: the model would simply be absent
    # from the picker with no explanation.
    face_refusals = []
    try:
        from lib.llm_dispatch.factory import get_dispatcher
        face_refusals = list(getattr(get_dispatcher(), 'face_refusals', []) or [])
    except Exception as e:
        logger.debug('[ServerConfig] face_refusals unavailable: %s', e)

    return jsonify({
        'providers': providers, 'presets': presets,
        'models': models, 'search': search_info,
        'server_info': server_info,
        'feishu': feishu_info,
        'face_refusals': face_refusals,
        'dropdown_models': dropdown_models,
        'hidden_models': hidden_models,
        'hidden_ig_models': hidden_ig_models,
        'model_pricing': model_pricing,
        'provider_pricing': provider_pricing,
        'model_limits': model_limits,
        'model_context_limits': model_context_limits,
        'model_defaults': model_defaults,
        'network': network_info,
        'mt_provider': mt_provider_info,
        'upload': upload_policy,
        'context': context_policy,
        'translation': translation_policy,
        'langDetect': lang_detect_policy,
        'capability_taxonomy': capability_taxonomy,
    })


@config_bp.route('/api/v1/feishu/status')
@safe_route
def feishu_status():
    """Return Feishu bot runtime status.

    @safe_route (pt_63eb7f02 batch 5): the ad-hoc "except Exception →
    api_internal_error(e)" wrap around the body was a pure logger.warning
    + api_internal_error with no distinct context / side effects; the
    decorator reproduces it via fn.__qualname__.
    """
    from lib.feishu._state import (
        ALLOWED_USERS,
        APP_ID,
        APP_SECRET,
        DEFAULT_PROJECT_PATH,
        ENABLED,
        WORKSPACE_ROOT,
        _conversations,
    )
    active_users = len(_conversations)
    return jsonify({
        'ok': True,
        'enabled': ENABLED,
        'connected': _feishu_is_connected(),
        'app_id_masked': ('***' + APP_ID[-4:]) if len(APP_ID) > 4 else '',
        'has_secret': bool(APP_SECRET),
        'active_users': active_users,
        'allowed_users': sorted(ALLOWED_USERS),
        'default_project': DEFAULT_PROJECT_PATH,
        'workspace_root': WORKSPACE_ROOT,
    })


@config_bp.route('/api/v1/providers/balance', methods=['POST'])
def check_provider_balance():
    """Proxy a balance/billing check to a provider's billing API."""
    import requests as _requests
    from lib.http_client import http_get

    data = parse_body()
    balance_url = (data.get('balance_url') or '').strip()
    api_key = (data.get('api_key') or '').strip()

    if not balance_url:
        return api_bad_request('No balance_url provided')
    if not api_key:
        return api_bad_request('No api_key provided')
    if not balance_url.startswith('https://'):
        return api_bad_request('balance_url must use HTTPS')

    headers = {'Authorization': 'Bearer %s' % api_key}
    logger.info('[Balance] Checking balance at %.200s', balance_url)

    try:
        resp = http_get(balance_url, headers=headers, timeout=15)
        resp.raise_for_status()
        billing = resp.json()
    except _requests.Timeout:
        logger.warning('[Balance] Timeout fetching %s', balance_url)
        return api_error('Request timed out (15s)', status=504)
    except _requests.RequestException as e:
        logger.warning('[Balance] Request failed for %s: %s', balance_url, e)
        return api_error('Request failed: %s' % e, status=502)
    except (ValueError, TypeError) as e:
        logger.warning('[Balance] Invalid JSON from %s: %s', balance_url, e)
        return api_error('Invalid JSON response', status=502)

    result = _normalize_balance(billing, balance_url, headers)

    logger.info('[Balance] Result: %s', {k: v for k, v in result.items() if k != 'raw'})
    return api_ok({'balance': result})


@config_bp.route('/api/v1/providers/discover-models', methods=['POST'])
@safe_route
def discover_models_endpoint():
    """Auto-discover models from a provider's /v1/models endpoint.

    @safe_route (pt_63eb7f02 batch 5): the ad-hoc "except Exception \u2192
    api_internal_error(e)" wrapping the discover_models + enrich pass was
    pure logger.error + api_internal_error; @safe_route reproduces it.
    """ 
    data = parse_body()
    base_url = data.get('base_url', '').strip()
    api_key = data.get('api_key', '').strip()
    models_path = data.get('models_path', '').strip()

    if not base_url:
        return api_bad_request('base_url is required')
    if not api_key:
        return api_bad_request('api_key is required')

    # @safe_route on the enclosing view catches any exception from either
    # discover_models or enrich_models_with_pricing (pt_63eb7f02 batch 5).
    from lib.llm_dispatch.discovery import discover_models, enrich_models_with_pricing
    models = discover_models(base_url, api_key, models_path=models_path)
    if not models:
        return api_error('No models found at %s' % base_url, status=404)

    models = enrich_models_with_pricing(models)
    logger.info('[Discovery] Endpoint returned %d models for %s', len(models), base_url)
    return api_ok({'models': models})


@config_bp.route('/api/v1/providers/templates/update', methods=['PUT'])
def update_provider_template():
    """Persist discovered models back into the hardcoded JS provider template.

    Accepts ``{key, models}`` — finds the template with matching ``key`` in
    ``settings.js`` (and ``bundle-*.js`` if present), replaces its ``models``
    array with the new list, preserving all other template fields and comments.

    The models array is formatted to match the existing code style.
    """
    import glob
    import re

    data = parse_body()
    tpl_key = (data.get('key') or '').strip()
    models = data.get('models')

    if not tpl_key:
        return api_bad_request('key is required')
    if not models or not isinstance(models, list):
        return api_bad_request('models list is required')

    # Sanitize models to only keep template-relevant fields
    clean_models = []
    for m in models:
        mid = m.get('model_id', '')
        if not mid:
            continue
        cm = {
            'model_id': mid,
            'capabilities': m.get('capabilities', ['text']),
            'rpm': m.get('rpm', 60),
            'cost': m.get('cost', 0.001),
        }
        clean_models.append(cm)

    if not clean_models:
        return api_bad_request('No valid models in list')

    # Format models array as JS source (matching existing code style)
    def _fmt_model(m):
        mid = m['model_id']
        caps = m['capabilities']
        rpm = m['rpm']
        cost = m['cost']
        caps_str = ', '.join("'%s'" % c for c in caps)
        # Align columns for readability
        mid_part = "model_id: '%s'," % mid
        mid_part = mid_part.ljust(35)
        caps_part = "capabilities: [%s]," % caps_str
        caps_part = caps_part.ljust(50)
        rpm_part = 'rpm: %d,' % rpm
        rpm_part = rpm_part.ljust(10)
        cost_part = 'cost: %s' % _fmt_cost(cost)
        return '      { %s %s %s %s },' % (mid_part, caps_part, rpm_part, cost_part)

    def _fmt_cost(c):
        """Format cost number without trailing zeros."""
        if c == 0:
            return '0.0'
        s = '%.6f' % c
        # Strip trailing zeros but keep at least one decimal place
        s = s.rstrip('0')
        if s.endswith('.'):
            s += '0'
        return s

    models_lines = '\n'.join(_fmt_model(m) for m in clean_models)
    new_models_block = 'models: [\n%s\n    ]' % models_lines

    # Find and update settings.js (and any bundle-*.js)
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    js_dir = os.path.join(base_dir, 'static', 'js')

    targets = [os.path.join(js_dir, 'settings.js')]
    targets.extend(glob.glob(os.path.join(js_dir, 'bundle-*.js')))

    # Regex: find the template block by key, then match its models array
    # Pattern: key: '<key>' ... models: [ ... ] (with balanced brackets)
    updated_files = []
    for fpath in targets:
        if not os.path.isfile(fpath):
            continue
        try:
            with open(fpath, 'r', encoding='utf-8') as f:
                content = f.read()

            # Find the template entry by key
            # Look for: key: 'KEY' (possibly with brand, name, base_url lines)
            # Then find models: [ ... ] block
            key_pattern = re.escape("key: '%s'" % tpl_key)
            key_match = re.search(key_pattern, content)
            if not key_match:
                logger.debug('[TemplateUpdate] key=%s not found in %s', tpl_key, fpath)
                continue

            # From the key position, find the models: [ ... ] block
            # We need to find 'models: [' and then the matching ']'
            after_key = content[key_match.start():]
            models_start_match = re.search(r'models:\s*\[', after_key)
            if not models_start_match:
                logger.warning('[TemplateUpdate] models array not found after key=%s in %s',
                             tpl_key, fpath)
                continue

            # Find the absolute position of 'models: ['
            abs_models_start = key_match.start() + models_start_match.start()
            bracket_start = key_match.start() + models_start_match.end() - 1  # position of '['

            # Find the matching ']' (handle nested brackets)
            depth = 0
            pos = bracket_start
            while pos < len(content):
                if content[pos] == '[':
                    depth += 1
                elif content[pos] == ']':
                    depth -= 1
                    if depth == 0:
                        break
                pos += 1

            if depth != 0:
                logger.error('[TemplateUpdate] Unbalanced brackets in %s for key=%s',
                           fpath, tpl_key)
                continue

            bracket_end = pos  # position of matching ']'

            # Replace: from 'models: [' to matching ']'
            new_content = content[:abs_models_start] + new_models_block + content[bracket_end + 1:]

            with open(fpath, 'w', encoding='utf-8') as f:
                f.write(new_content)

            fname = os.path.basename(fpath)
            updated_files.append(fname)
            logger.info('[TemplateUpdate] Updated %s: key=%s, %d models',
                       fname, tpl_key, len(clean_models))

        except Exception as e:
            logger.error('[TemplateUpdate] Failed to update %s: %s', fpath, e, exc_info=True)

    if not updated_files:
        return api_error("Template key '%s' not found in any JS file" % tpl_key, status=404)

    return jsonify({
        'ok': True,
        'updated_files': updated_files,
        'model_count': len(clean_models),
    })


@config_bp.route('/api/v1/providers/resolve-faces', methods=['POST'])
@safe_route
def resolve_provider_faces():
    """Resolve which wire face every model of ONE provider dispatches over.

    Body: ``{provider: {...}}`` — a provider dict shaped like a
    ``config['providers'][i]`` entry. Only the routing-relevant fields are
    read (``base_url`` / ``protocol`` / ``faces`` / ``models``); credentials
    are never touched, so the Settings UI can post its UNSAVED working copy.

    Returns ``{ok, resolutions: [{model_id, ok, face, protocol, base_url,
    forced, error}], faces: [name, ...], dual_face_host: bool}``.

    WHY THIS ENDPOINT EXISTS
    ------------------------
    The Settings panel edits a working copy that has not been saved yet, so
    the ``face_refusals`` list computed at GET-time (dispatcher slot build)
    goes STALE the moment the user pins a face, adds a Claude model or edits
    ``faces{}``. Rendering a stale verdict is worse than rendering none: the
    failure mode is a pill reading "anthropic" on a model that will actually
    dispatch over the OpenAI wire — the silent signature-dropping shape
    ``provider_face`` exists to prevent.

    The alternative — re-implementing the family rule in JavaScript — is
    barred by charter #12 (no hand-copied backend enums) and would drift in
    exactly that direction. So the UI asks the SAME resolver the dispatcher
    uses (``lib.llm_dispatch.provider_face.resolve_face``); there is one
    implementation of "which wire does this model use", and the UI cannot
    disagree with routing.

    Stateless: reads no config file, mutates nothing, builds no slots.
    """
    from lib.llm_dispatch.provider_face import (
        DEFAULT_FACE, dual_face_hosts, provider_faces, resolve_face,
    )

    data = parse_body()
    provider = data.get('provider')
    if not isinstance(provider, dict):
        return api_bad_request('provider (object) is required')

    models = provider.get('models')
    if not isinstance(models, list):
        models = []

    known = dual_face_hosts()
    faces = provider_faces(provider)

    # ── The dispatcher's PRE-RESOLVE filters, mirrored ──
    # ``_build_slots_from_providers`` skips these BEFORE it calls
    # resolve_face, so a skipped entry can never appear in ``face_refusals``.
    # Reporting a refusal for one would paint an amber "missing wire face"
    # banner on a model the user simply switched off — an alarm naming the
    # wrong cause. Charter #26: a new consumer of a filtered surface MUST
    # inventory that filter.
    #
    # The card-level reason is computed once; ``effective_keys`` mirrors
    # dispatcher.py L401 exactly — a brand=='local' provider with NO keys
    # still builds (one blank-key slot), so keying this on ``api_keys``
    # alone would wrongly blank the pills on every self-hosted deployment.
    card_skip = ''
    if provider.get('enabled', True) is False:
        card_skip = 'provider_disabled'
    else:
        # The UI posts a COUNT, never the key strings (this endpoint promises
        # credentials never travel to it). A saved provider dict passed
        # straight in still has `api_keys`, so accept either shape.
        if 'api_key_count' in provider:
            n_keys = provider.get('api_key_count') or 0
        else:
            n_keys = len([k for k in (provider.get('api_keys') or [])
                          if isinstance(k, str) and k.strip()])
        # Mirrors dispatcher.py L401: a brand=='local' card with no keys is
        # given ONE blank-key slot and does build, so `no_keys` must not fire
        # for it. Keying this on key-count alone would blank the pills on
        # every self-hosted deployment.
        if not n_keys and provider.get('brand') != 'local':
            card_skip = 'no_keys'

    resolutions = []
    for m in models:
        if not isinstance(m, dict):
            continue
        mid = (m.get('model_id') or '').strip()
        if not mid:
            continue

        # Skipped entries are REPORTED (with the reason) rather than omitted:
        # omitting them would be a cache miss on the client, and a miss
        # renders nothing — the right pixels for the wrong reason. An
        # explicit marker also lets the UI grow a real "disabled" state
        # later without another round of backend archaeology.
        skip = card_skip or ('model_disabled' if m.get('enabled') is False else '')
        if skip:
            resolutions.append({
                'model_id': mid, 'skipped': skip, 'ok': True,
                'face': DEFAULT_FACE, 'protocol': '', 'base_url': '',
                'forced': False, 'error': '',
            })
            continue

        r = resolve_face(provider, m, dual_face_hosts=known)
        resolutions.append({
            'model_id': mid,
            'ok': r.ok,
            'face': r.face_name,
            'protocol': r.protocol or '',
            'base_url': r.base_url,
            'forced': r.forced,
            'error': r.error or '',
        })

    from urllib.parse import urlparse
    host = (urlparse(provider.get('base_url') or '').hostname or '').lower()

    n_skipped = sum(1 for r in resolutions if r.get('skipped'))
    logger.debug('[Face] resolved %d model(s) for provider %s (faces=%s, '
                 '%d skipped)', len(resolutions), provider.get('id', '?'),
                 sorted(faces), n_skipped)
    return api_ok({
        'resolutions': resolutions,
        # The declared face names, default first — the UI's pin dropdown is
        # built from THIS, never from a hand-written list (charter #12).
        'faces': [DEFAULT_FACE] + sorted(n for n in faces if n != DEFAULT_FACE),
        'dual_face_host': host in known,
    })


@config_bp.route('/api/v1/providers/probe', methods=['POST'])
def probe_provider_endpoint():
    """One-shot provider auto-setup: discover models, detect brand, find balance URL.

    Accepts ``{base_url, api_key, models_path?}`` and returns a complete
    provider configuration ready to be inserted into the providers list.
    """
    data = parse_body()
    base_url = (data.get('base_url') or '').strip()
    api_key = (data.get('api_key') or '').strip()
    models_path = (data.get('models_path') or '').strip()

    if not base_url:
        return api_bad_request('请填写 API 地址 (Base URL)')

    from lib.llm_dispatch.discovery import is_local_endpoint, probe_provider
    # Local self-hosted endpoints (vLLM / SGLang / Ollama) usually run without
    # auth — make the API-key field optional in that case.
    if not api_key and not is_local_endpoint(base_url):
        return api_bad_request('请填写 API 密钥')

    logger.info('[Probe] Provider probe requested for %.200s', base_url)

    try:
        result = probe_provider(base_url, api_key, models_path=models_path)

        if result.get('ok'):
            logger.info('[Probe] Success: %s — %d models, balance=%s',
                       result.get('name', '?'), len(result.get('models', [])),
                       bool(result.get('balance_url')))
        else:
            logger.warning('[Probe] Failed for %s: %s', base_url, result.get('error', '?'))

        return jsonify(result)
    except Exception as e:
        logger.error('[Probe] Endpoint failed: %s', e, exc_info=True)
        return api_error('探测出错: %s' % e, status=500)


@config_bp.route('/api/v1/providers/probe-bulk', methods=['POST'])
def probe_provider_bulk():
    """Probe many endpoints at once for the 'Bulk Add Local Endpoints' UI.

    Accepts ``{base_urls: [str], api_key?: str}`` (api_key optional — most
    self-hosted vLLM/SGLang servers run without auth).  Probes are fanned
    out concurrently with a small thread pool; per-URL failures are reported
    individually so a single dead box doesn't kill the whole batch.

    Returns ``{ok, results: [{base_url, ok, ...probe_payload}]}``.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from lib.llm_dispatch.discovery import normalize_base_url, probe_provider

    data = parse_body()
    raw_urls = data.get('base_urls') or []
    api_key = (data.get('api_key') or '').strip()
    models_path = (data.get('models_path') or '').strip()

    if not isinstance(raw_urls, list) or not raw_urls:
        return api_bad_request('请提供至少一个 API 地址')

    # Normalize, dedupe, cap to a reasonable upper bound.
    seen = set()
    base_urls = []
    for raw in raw_urls:
        if not isinstance(raw, str):
            continue
        url = normalize_base_url(raw.strip())
        if not url:
            continue
        if not (url.startswith('http://') or url.startswith('https://')):
            url = 'http://' + url
            url = normalize_base_url(url)
        if url in seen:
            continue
        seen.add(url)
        base_urls.append(url)

    if not base_urls:
        return api_bad_request('所有 URL 解析后均为空')

    # Hard cap — keeps the UI snappy even when a user pastes 200 lines.
    if len(base_urls) > 50:
        return jsonify({
            'ok': False,
            'error': '单次最多探测 50 个端点（收到 %d 个）' % len(base_urls),
        }), 400

    logger.info('[Probe-Bulk] Probing %d endpoint(s)', len(base_urls))

    def _probe_one(url: str):
        try:
            # The bulk-probe UI is explicitly labeled 'local' — anything the
            # user adds through it is treated as a self-hosted endpoint, even
            # when the IP happens to live in publicly-routable space.
            res = probe_provider(url, api_key, models_path=models_path,
                                 force_local=True)
            res['base_url'] = res.get('base_url') or url
            return res
        except Exception as e:
            logger.error('[Probe-Bulk] %s failed: %s', url, e, exc_info=True)
            return {'ok': False, 'base_url': url, 'error': '探测异常: %s' % e}

    results = [None] * len(base_urls)
    workers = min(8, len(base_urls))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='probe-bulk') as pool:
        futures = {pool.submit(_probe_one, u): i for i, u in enumerate(base_urls)}
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                results[idx] = fut.result()
            except Exception as e:
                logger.error('[Probe-Bulk] future raised: %s', e, exc_info=True)
                results[idx] = {'ok': False, 'base_url': base_urls[idx],
                                'error': '探测异常: %s' % e}

    n_ok = sum(1 for r in results if r and r.get('ok'))
    logger.info('[Probe-Bulk] Done: %d/%d ok', n_ok, len(base_urls))

    return jsonify({
        'ok': True,
        'count': len(base_urls),
        'ok_count': n_ok,
        'results': results,
    })


# ══════════════════════════════════════════════════════
#  Access-matrix cell-probe engine
#  → moved to lib/provider_probe.py (2026-06). Re-exported here with the
#    legacy private names used by the probe route handlers below + tests.
# ══════════════════════════════════════════════════════
import threading  # noqa: E402
import time as _time  # noqa: E402

from lib.json_store import read_json  # noqa: E402,F401  (used by probe handlers)
from lib.provider_probe import (  # noqa: E402,F401
    probe_one_cell as _probe_one_cell,
    probe_cell_multi as _probe_cell_multi,
    run_cell_probe_task as _run_cell_probe_task,
    build_probe_work as _build_probe_work,
    probe_cache_path as _probe_cache_path,
    probe_cell_key as _probe_cell_key,
    persist_probe_task as _persist_probe_task,
    public_probe_snapshot as _public_probe_snapshot,
    CELL_PROBE_TASKS as _CELL_PROBE_TASKS,
    CELL_PROBE_LOCK as _CELL_PROBE_LOCK,
)

@config_bp.route('/api/v1/providers/probe-cells/start', methods=['POST'])
def probe_provider_cells_start():
    """Start (or resume) a background per-(key × id) reachability probe.

    Body: ``{provider_id, base_url, api_keys: [str], extra_headers?: {},
    models: [{model_id, aliases?: [str], request_ids?: [str],
    key_access?: {}, capabilities?: [str]}], timeout?: int, attempts?: int,
    force?: bool}``. ``attempts`` (default 3, clamped 1..5) re-probes a
    transiently-failing cell to filter out FALSE 429s.

    Behaviour:
      * If a probe for ``provider_id`` is already running → return its
        live snapshot (does NOT restart).
      * Else if ``force`` is false and a persisted snapshot exists on disk
        → return that (resume after Settings was closed / server restarted).
      * Else start a fresh background probe and return its initial snapshot.

    Every wire id of a model's pool (``request_ids``, or ``[model_id] +
    aliases`` for a legacy entry) is probed as its own cell — wire ids are
    distinct upstream deployments. The logical ``model_id`` of an
    explicit-pool entry is NEVER probed: it is a preset-facing identity
    that no real request carries.
    """
    data = parse_body()
    provider_id = (data.get('provider_id') or '').strip()
    base_url = (data.get('base_url') or '').strip()
    api_keys = data.get('api_keys') or []
    extra_headers = data.get('extra_headers') or {}
    models = data.get('models') or []
    timeout = int(data.get('timeout') or 12)
    attempts = max(1, min(5, int(data.get('attempts') or 3)))
    protocol = (data.get('protocol') or 'openai').strip() or 'openai'
    # Alternate wire faces of this ONE account (account/face separation). A
    # gateway can serve some models over OpenAI-compat and others over the
    # Anthropic-native wire with the SAME keys, so the probe must ask each
    # cell on the wire that cell actually dispatches over — probing a
    # Claude model on the OpenAI face yields a false 'not_found'.
    # Resolution is delegated to lib.llm_dispatch.provider_face so the probe
    # and the dispatcher can never disagree about which wire a model uses.
    faces = data.get('faces') if isinstance(data.get('faces'), dict) else {}
    # 'claude' / 'codex' for a SUBSCRIPTION provider — its configured api_key
    # is the 'oauth-managed' sentinel, so the probe resolves the live token
    # per cell instead of sending the sentinel (which would 401 → false
    # recommend-disable). Empty for normal key-based providers.
    oauth = (data.get('oauth') or '').strip()
    force = bool(data.get('force'))

    # Optional scope for row / column / single-cell probes. When present the
    # work list is restricted to the requested (key_idx × model_id) cells and
    # the fresh results are MERGED into the persisted snapshot (the rest of
    # the grid keeps its previous verdicts) instead of replacing it.
    only = data.get('only') if isinstance(data.get('only'), dict) else {}
    _ok = only.get('key_idxs')
    _om = only.get('model_ids')
    only_key_idxs = ({i for i in _ok if isinstance(i, int) and not isinstance(i, bool)}
                     if isinstance(_ok, list) else None)
    only_model_ids = ({x.strip() for x in _om if isinstance(x, str) and x.strip()}
                      if isinstance(_om, list) else None)
    scoped = bool(only_key_idxs) or bool(only_model_ids)

    if not provider_id:
        return api_bad_request('provider_id is required')
    if not base_url:
        return api_bad_request('base_url is required')
    if not isinstance(api_keys, list) or not api_keys:
        return api_bad_request('api_keys (non-empty list) is required')
    if not isinstance(models, list) or not models:
        return api_bad_request('models (non-empty list) is required')

    with _CELL_PROBE_LOCK:
        existing = _CELL_PROBE_TASKS.get(provider_id)
        if existing and existing['status'] == 'running' and not force:
            logger.info('[CellProbe] %s already running — returning live snapshot', provider_id)
            return api_ok(_public_probe_snapshot(existing))

    if not force and not scoped:
        cached = read_json(_probe_cache_path(provider_id), default=None)
        if isinstance(cached, dict) and cached.get('cells'):
            logger.info('[CellProbe] %s resumed from disk (%d cells, status=%s)',
                        provider_id, len(cached['cells']), cached.get('status'))
            return api_ok(cached)

    # Build the work list: one cell per (key_idx, wire id) — the same pool
    # the dispatcher resolves (resolve_request_ids), because wire ids can be
    # different upstream deployments.
    # Capabilities ride along so the probe can SKIP non-chat models
    # (image_gen / embedding / transcription) instead of chat-probing them
    # into a guaranteed false 'unavailable' verdict.
    work = _build_probe_work(
        {'id': provider_id, 'base_url': base_url, 'protocol': protocol,
         'faces': faces},
        models, api_keys)

    if not work:
        return api_bad_request('no testable (key, model) pairs')
    if len(work) > 400:
        return api_bad_request('too many cells to probe (%d > 400)' % len(work))

    # Scoped probe: restrict the run to the requested cells and seed the task
    # with the persisted snapshot's OTHER cells so the result merges into the
    # full grid. Seeds are pruned to the provider's current (key × id) space —
    # a model/key deleted since the cache was written must not come back as a
    # ghost pip. done_count tracks only this run's completions, so seeded
    # cells never inflate the progress counter.
    seed_cells = {}
    if scoped:
        full_work = work
        work = [w for w in full_work
                if (not only_key_idxs or w[0] in only_key_idxs)
                and (not only_model_ids or w[3] in only_model_ids)]
        if not work:
            return api_bad_request('no cells match the requested scope')
        probed_keys = {_probe_cell_key(w[0], w[3]) for w in work}
        valid_keys = {_probe_cell_key(w[0], w[3]) for w in full_work}
        cached = read_json(_probe_cache_path(provider_id), default=None)
        if isinstance(cached, dict) and isinstance(cached.get('cells'), dict):
            for k, v in cached['cells'].items():
                if k in valid_keys and k not in probed_keys and isinstance(v, dict):
                    seed_cells[k] = v

    task = {
        'provider_id': provider_id,
        'status': 'running',
        'started_at': _time.time(),
        'finished_at': None,
        'total': len(work),
        'done_count': 0,
        'cells': dict(seed_cells),
        'summary': {'ok': 0, 'disable': 0},
        'error': None,
        'attempts': attempts,
        '_abort': False,
        '_base_url': base_url,
        '_extra_headers': extra_headers,
        '_protocol': protocol,
        '_oauth': oauth,
    }
    with _CELL_PROBE_LOCK:
        _CELL_PROBE_TASKS[provider_id] = task
    _persist_probe_task(task)

    th = threading.Thread(target=_run_cell_probe_task, args=(task, work, timeout),
                          name='cell-probe-%s' % provider_id[:24], daemon=True)
    th.start()

    logger.info('[CellProbe] Launched background probe for %s (%d cells, force=%s, scoped=%s, seeded=%d)',
                provider_id, len(work), force, scoped, len(seed_cells))
    return api_ok(_public_probe_snapshot(task))


@config_bp.route('/api/v1/providers/probe-cells/status', methods=['GET'])
def probe_provider_cells_status():
    """Poll a provider's probe progress: live task if any, else disk snapshot.

    Query: ``?provider_id=X``. Returns the same snapshot shape as ``start``,
    or ``{status: 'none'}`` when nothing has ever been probed.
    """
    provider_id = (request.args.get('provider_id') or '').strip()
    if not provider_id:
        return api_bad_request('provider_id is required')

    with _CELL_PROBE_LOCK:
        task = _CELL_PROBE_TASKS.get(provider_id)
        if task:
            return api_ok(_public_probe_snapshot(task))

    cached = read_json(_probe_cache_path(provider_id), default=None)
    if isinstance(cached, dict) and cached.get('cells') is not None:
        return api_ok(cached)
    return api_ok({'status': 'none'})


@config_bp.route('/api/v1/providers/templates')
def get_provider_templates():
    """Serve external provider templates from static/provider_templates/*.json.

    Pricing-tier tags ('cheap', plus any future PRICING_TIERS row) are
    re-evaluated on every request against live MODEL_PRICING data, so a
    stale tag in the JSON file (e.g. a new model whose price was lowered)
    doesn't mislead the one-click provider setup UI.  The JSON files are
    NOT rewritten — use ``debug/reeval_pricing_tags.py`` for that.
    """
    from lib.llm_dispatch.config import reevaluate_pricing_tags

    templates_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'static', 'provider_templates'
    )
    result = []
    if not os.path.isdir(templates_dir):
        return jsonify(result)
    for fname in sorted(os.listdir(templates_dir)):
        if not fname.endswith('.json'):
            continue
        fpath = os.path.join(templates_dir, fname)
        try:
            with open(fpath, encoding='utf-8') as f:
                tpl = json.load(f)
            if not (isinstance(tpl, dict) and tpl.get('key')):
                continue
            # Normal templates must ship a model list. The 'local' template is
            # an exception — its model list is filled in at probe time, not
            # bundled in the file (every deployment hosts different models).
            if not tpl.get('models') and tpl.get('category') != 'local':
                continue
            # Normalize pricing-tier tags using live pricing data.
            try:
                reevaluate_pricing_tags(
                    tpl['models'],
                    log_prefix='template=%s' % tpl.get('key', fname),
                )
            except Exception as e:
                logger.warning('[ProviderTemplates] Tier re-eval failed for %s: %s',
                               fname, e, exc_info=True)
            result.append(tpl)
            logger.debug('[ProviderTemplates] Loaded %s (%d models)',
                        fname, len(tpl.get('models', [])))
        except Exception as e:
            logger.warning('[ProviderTemplates] Failed to load %s: %s', fname, e)
    return jsonify(result)


def _hot_reload_feishu(feishu_data: dict):
    """Hot-reload Feishu configuration by updating lib.feishu._state in place.

    This allows Feishu settings (app_id, secret, allowed_users, project paths)
    to take effect without restarting the server. Does NOT restart the WebSocket
    connection — that still requires a restart if the app_id/secret changed.
    """
    try:
        import lib.feishu._state as _st
        if 'app_id' in feishu_data:
            _st.APP_ID = feishu_data['app_id'] or ''
        if 'app_secret' in feishu_data:
            _st.APP_SECRET = feishu_data['app_secret'] or ''
        _st.ENABLED = bool(_st.APP_ID and _st.APP_SECRET)
        if 'allowed_users' in feishu_data and isinstance(feishu_data['allowed_users'], list):
            _st.ALLOWED_USERS = set(feishu_data['allowed_users'])
        if 'default_project' in feishu_data:
            _st.DEFAULT_PROJECT_PATH = feishu_data['default_project'] or _st.DEFAULT_PROJECT_PATH
        if 'workspace_root' in feishu_data:
            _st.WORKSPACE_ROOT = feishu_data['workspace_root'] or _st.WORKSPACE_ROOT
        logger.info('[Feishu] Hot-reloaded config: enabled=%s, app_id=%s',
                    _st.ENABLED, ('***' + _st.APP_ID[-4:]) if len(_st.APP_ID) > 4 else '(empty)')
    except Exception as e:
        logger.warning('[Feishu] Hot-reload failed: %s', e, exc_info=True)


@config_bp.route('/api/v1/server-config', methods=['POST'])
def save_server_config():
    """POST — save server configuration changes.

    All settings take effect immediately (hot-reload) — no server restart needed.
    The flow: write config to disk → reload_config() updates module-level vars →
    reset_dispatcher() rebuilds LLM slot pool → hot-reload Feishu/proxy/etc.
    """
    import lib as _lib
    from lib.log import audit_log
    from lib.json_store import update_json_atomic

    data = parse_body()
    changes = []
    dispatch_reset_needed = False

    # The whole read-modify-write runs inside ONE ``update_json_atomic``
    # mutator so it is serialised (per-path thread lock + cross-process
    # flock) against the background writers of this SAME file
    # (context_limits / model_info / dispatcher discovery / health_local).
    # Reading at the top and writing at the bottom — as this used to do —
    # would make each individual write atomic but still lose a concurrent
    # writer's update that landed in the read→write gap. The mutator sees
    # the FRESH on-disk config under the lock, so learned model_limits /
    # context limits added meanwhile are preserved. Interleaved
    # side-effects (feishu/proxy/search hot-reload) keep their original
    # positions so behaviour is unchanged.
    def _mutate(existing):
        nonlocal dispatch_reset_needed
        if not isinstance(existing, dict):
            existing = {}

        if 'providers' in data and isinstance(data['providers'], list):
            existing['providers'] = data['providers']
            total_models = sum(len(p.get('models', [])) for p in data['providers'])
            changes.append('providers (%d with %d models)' % (len(data['providers']), total_models))
            dispatch_reset_needed = True
            existing.pop('models_registry', None)

        if 'presets' in data and isinstance(data['presets'], dict):
            existing['presets'] = data['presets']
            changes.append('presets')
            dispatch_reset_needed = True

        if 'models' in data and isinstance(data['models'], dict):
            old_models = existing.get('models', {})
            existing['models'] = {**old_models, **data['models']}
            for k, v in data['models'].items():
                if old_models.get(k) != v:
                    changes.append('models.%s' % k)
                    dispatch_reset_needed = True

        if 'search' in data and isinstance(data['search'], dict):
            existing['search'] = data['search']
            # LLM content filter is a separate module-level flag
            if 'llm_content_filter' in data['search']:
                _lib.LLM_CONTENT_FILTER_ENABLED = bool(data['search']['llm_content_filter'])
                from lib.search_bridge import sync_search_config
                sync_search_config()
                logger.info('[Config] LLM content filter → %s', _lib.LLM_CONTENT_FILTER_ENABLED)
            changes.append('search.*')

        if 'hidden_models' in data and isinstance(data['hidden_models'], list):
            existing['hidden_models'] = data['hidden_models']
            changes.append('hidden_models')

        if 'hidden_ig_models' in data and isinstance(data['hidden_ig_models'], list):
            existing['hidden_ig_models'] = data['hidden_ig_models']
            changes.append('hidden_ig_models')

        if 'model_defaults' in data and isinstance(data['model_defaults'], dict):
            existing['model_defaults'] = data['model_defaults']
            md = data['model_defaults']
            if md.get('default_model'):
                existing.setdefault('presets', {})['opus'] = md['default_model']
                existing.setdefault('models', {})['LLM_MODEL'] = md['default_model']
            existing.setdefault('models', {})['fallback_model'] = md.get('fallback_model', '')
            changes.append('model_defaults')
            dispatch_reset_needed = True

        if 'proxy_bypass_domains' in data and isinstance(data['proxy_bypass_domains'], list):
            existing['proxy_bypass_domains'] = data['proxy_bypass_domains']
            from lib.proxy import set_bypass_domains
            set_bypass_domains(data['proxy_bypass_domains'])
            changes.append('proxy_bypass_domains')

        if 'proxy_config' in data and isinstance(data['proxy_config'], dict):
            pc = data['proxy_config']
            existing['proxy_config'] = {
                'http_proxy': (pc.get('http_proxy') or '').strip(),
                'https_proxy': (pc.get('https_proxy') or '').strip(),
            }
            existing['proxy_config'].pop('no_proxy', None)
            from lib.proxy import set_proxy_config
            set_proxy_config(
                http_proxy=existing['proxy_config']['http_proxy'],
                https_proxy=existing['proxy_config']['https_proxy'],
            )
            changes.append('proxy_config')

        if 'feishu' in data and isinstance(data['feishu'], dict):
            existing['feishu'] = data['feishu']
            changes.append('feishu')
            # Hot-reload Feishu state
            _hot_reload_feishu(data['feishu'])

        if 'mt_provider' in data and isinstance(data['mt_provider'], dict):
            mt = data['mt_provider']
            existing['mt_provider'] = {
                'provider': (mt.get('provider') or 'niutrans').strip(),
                'api_url': (mt.get('api_url') or '').strip(),
                'api_key': (mt.get('api_key') or '').strip(),
                'app_id': (mt.get('app_id') or '').strip(),
                'enabled': bool(mt.get('enabled', False)),
            }
            changes.append('mt_provider')
            logger.info('[Config] MT provider updated: provider=%s, enabled=%s',
                        existing['mt_provider']['provider'],
                        existing['mt_provider']['enabled'])

        return existing

    # ── Persist to disk (locked read-modify-write) ──
    try:
        update_json_atomic(_SERVER_CONFIG_PATH, _mutate, default={})
        logger.info('[ServerConfig] Saved server_config.json')
    except Exception as e:
        logger.error('[ServerConfig] Failed to write config file to %s: %s',
                     _SERVER_CONFIG_PATH, e, exc_info=True)
        return api_internal_error('Failed to write config file')

    # ── Hot-reload: update all module-level variables from disk ──
    try:
        _lib.reload_config()
    except Exception as e:
        logger.error('[ServerConfig] reload_config() failed: %s', e, exc_info=True)

    # ── Reset dispatcher if provider/model config changed ──
    if dispatch_reset_needed:
        try:
            from lib.llm_dispatch import reset_dispatcher
            reset_dispatcher()
            logger.info('[ServerConfig] Dispatcher reset — new config active immediately')
        except Exception as e:
            logger.warning('[ServerConfig] Dispatcher reset failed: %s', e, exc_info=True)

    if changes:
        audit_log('server_config_change', changes=changes)
        logger.info('[ServerConfig] Config changes applied (hot-reload): %s', changes)

    return api_ok({'needs_restart': False, 'changes': changes})