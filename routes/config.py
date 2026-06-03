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
from lib.api_response import api_bad_request, api_error, api_internal_error, api_ok
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
    try:
        if os.path.isfile(_SERVER_CONFIG_PATH):
            with open(_SERVER_CONFIG_PATH) as f:
                return json.load(f)
    except Exception as e:
        logger.warning('[ServerConfig] Failed to read server_config.json: %s', e)
    return {}


def _write_server_config(data):
    """Write server_config.json, creating directories as needed."""
    try:
        os.makedirs(os.path.dirname(_SERVER_CONFIG_PATH), exist_ok=True)
        with open(_SERVER_CONFIG_PATH, 'w') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info('[ServerConfig] Saved server_config.json')
        return True
    except Exception as e:
        logger.error('[ServerConfig] Failed to write server_config.json: %s', e, exc_info=True)
        return False


# ══════════════════════════════════════════════════════
#  Pricing-Tier Tag Re-evaluation (cheap, and any future tiers)
# ══════════════════════════════════════════════════════

def _reeval_cheap_tags(providers: list):
    """Re-evaluate pricing-tier capability tags on all provider models.

    Delegates to :func:`lib.llm_dispatch.config.reevaluate_pricing_tags`,
    which is driven by the PRICING_TIERS table (currently just 'cheap' —
    input < $3/1M AND output < $15/1M, strict).  New tiers added to that
    table are auto-applied here with no further code changes.

    The legacy name ``_reeval_cheap_tags`` is retained for continuity
    with existing call sites; it now covers every managed tier tag.
    """
    from lib.llm_dispatch.config import reevaluate_pricing_tags

    for prov in providers:
        models = prov.get('models') or []
        if not models:
            continue
        reevaluate_pricing_tags(models, log_prefix='provider=%s' % prov.get('id', '?'))


# ══════════════════════════════════════════════════════
#  Provider Defaults Builder
# ══════════════════════════════════════════════════════

def _build_default_providers():
    """Build default provider config from environment/hardcoded values."""
    import lib as _lib
    from lib.llm_dispatch.config import (
        DEFAULT_SLOT_CONFIGS,
        MODEL_ALIAS_GROUPS,
        MANAGED_TIER_TAGS,
        get_pricing_tiers,
    )

    base_url = getattr(_lib, 'LLM_BASE_URL', '')
    api_keys = list(getattr(_lib, 'LLM_API_KEYS', []))

    def _auto_cheap(model_id, caps_set, cost):
        # Apply every managed pricing-tier tag (cheap, plus any future tier).
        if 'image_gen' in caps_set or 'embedding' in caps_set:
            return caps_set
        tiers = get_pricing_tiers(model_id, fallback_cost_per_1k=cost)
        # Drop any stale managed tier tag not in the current desired set.
        caps_set -= (MANAGED_TIER_TAGS - tiers)
        caps_set |= tiers
        return caps_set

    def _build_chat_model_entry(model_id, think_default):
        slot_cfg = DEFAULT_SLOT_CONFIGS.get(model_id, {})
        caps_set = _auto_cheap(model_id, set(slot_cfg.get('caps', {'text'})), slot_cfg.get('cost', 0.01))
        aliases = []
        for group in MODEL_ALIAS_GROUPS:
            if model_id in group:
                aliases = sorted(a for a in group if a != model_id)
                break
        return {
            'model_id': model_id, 'aliases': aliases, 'capabilities': sorted(caps_set),
            'rpm': slot_cfg.get('rpm', 30), 'cost': slot_cfg.get('cost', 0.01),
            'thinking_default': think_default,
        }

    preset_model_keys = [
        ('opus', 'LLM_MODEL', True), ('qwen', 'QWEN_MODEL', True),
        ('gemini', 'GEMINI_MODEL', True), ('gemini_flash', 'GEMINI_FLASH_PREVIEW_MODEL', True),
        ('doubao', 'DOUBAO_MODEL', True), ('minimax', 'MINIMAX_MODEL', True),
    ]
    seen_model_ids = set()
    models = []
    presets = {}
    for preset_key, env_key, think_default in preset_model_keys:
        model_id = getattr(_lib, env_key, '')
        if not model_id:
            continue
        if preset_key != 'opus':
            presets[preset_key] = model_id
        if model_id in seen_model_ids:
            continue
        seen_model_ids.add(model_id)
        models.append(_build_chat_model_entry(model_id, think_default))

    extra_model_keys = [
        ('GEMINI_PRO_MODEL', True),
        ('GEMINI_PRO_PREVIEW_MODEL', True),
        ('CLAUDE_SONNET_MODEL', True),
    ]
    for env_key, think_default in extra_model_keys:
        model_id = getattr(_lib, env_key, '')
        if not model_id or model_id in seen_model_ids:
            continue
        seen_model_ids.add(model_id)
        models.append(_build_chat_model_entry(model_id, think_default))

    image_gen_id = getattr(_lib, 'IMAGE_GEN_MODEL', '')
    if image_gen_id and image_gen_id not in seen_model_ids:
        seen_model_ids.add(image_gen_id)
        slot_cfg = DEFAULT_SLOT_CONFIGS.get(image_gen_id, {})
        models.append({
            'model_id': image_gen_id, 'aliases': [],
            'capabilities': sorted(slot_cfg.get('caps', {'image_gen'})),
            'rpm': slot_cfg.get('rpm', 10),
            'cost': slot_cfg.get('cost', 0.015),
            'thinking_default': False,
        })

    for emb_id in getattr(_lib, 'EMBEDDING_MODELS', []):
        if emb_id in seen_model_ids:
            continue
        seen_model_ids.add(emb_id)
        from lib.embeddings import AVAILABLE_EMBEDDING_MODELS
        emb_info = AVAILABLE_EMBEDDING_MODELS.get(emb_id, {})
        models.append({
            'model_id': emb_id, 'aliases': [],
            'capabilities': ['embedding'],
            'rpm': emb_info.get('max_rpm', 60),
            'cost': 0.001,
            'thinking_default': False,
        })

    return [{'id': 'default', 'name': 'Default', 'base_url': base_url,
             'api_keys': api_keys, 'enabled': True, 'models': models}], presets


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

    import lib.fetch.content_filter as _cf_mod
    search_info = {
        'fetch_top_n': getattr(_lib, 'FETCH_TOP_N', 6),
        'fetch_timeout': getattr(_lib, 'FETCH_TIMEOUT', 15),
        'max_chars_search': getattr(_lib, 'FETCH_MAX_CHARS_SEARCH', 60000),
        'max_chars_direct': getattr(_lib, 'FETCH_MAX_CHARS_DIRECT', 200000),
        'max_chars_pdf': getattr(_lib, 'FETCH_MAX_CHARS_PDF', 0),
        'max_bytes': getattr(_lib, 'FETCH_MAX_BYTES', 20 * 1024 * 1024),
        'skip_domains': sorted(getattr(_lib, 'SKIP_DOMAINS', set())),
        'llm_content_filter': _cf_mod.FILTER_ENABLED,
    }
    if 'search' in saved:
        search_info.update(saved['search'])
        # Apply saved llm_content_filter on config load (page refresh / startup)
        if 'llm_content_filter' in saved['search']:
            _cf_mod.FILTER_ENABLED = bool(saved['search']['llm_content_filter'])

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
                if m.get('input_price') is not None and m.get('output_price') is not None:
                    model_pricing[mid] = {
                        'input': m['input_price'],
                        'output': m['output_price'],
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

    return jsonify({
        'providers': providers, 'presets': presets,
        'models': models, 'search': search_info,
        'server_info': server_info,
        'feishu': feishu_info,
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
    })


@config_bp.route('/api/v1/feishu/status')
def feishu_status():
    """Return Feishu bot runtime status."""
    from lib.feishu._state import (
        ALLOWED_USERS,
        APP_ID,
        APP_SECRET,
        DEFAULT_PROJECT_PATH,
        ENABLED,
        WORKSPACE_ROOT,
        _conversations,
    )
    try:
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
    except Exception as e:
        logger.warning('[Feishu] Status check error: %s', e, exc_info=True)
        return api_internal_error(e)


@config_bp.route('/api/v1/providers/balance', methods=['POST'])
def check_provider_balance():
    """Proxy a balance/billing check to a provider's billing API."""
    import requests as _requests

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
        resp = _requests.get(balance_url, headers=headers, timeout=15)
        resp.raise_for_status()
        billing = resp.json()
    except _requests.Timeout:
        logger.warning('[Balance] Timeout fetching %s', balance_url)
        return api_error('Request timed out (15s)', status=504)
    except _requests.RequestException as e:
        logger.warning('[Balance] Request failed for %s: %s', balance_url, e)
        return jsonify({'ok': False, 'error': 'Request failed: %s' % e}), 502
    except (ValueError, TypeError) as e:
        logger.warning('[Balance] Invalid JSON from %s: %s', balance_url, e)
        return api_error('Invalid JSON response', status=502)

    result = _normalize_balance(billing, balance_url, headers, _requests)

    logger.info('[Balance] Result: %s', {k: v for k, v in result.items() if k != 'raw'})
    return api_ok({'balance': result})
def _normalize_balance(billing, balance_url, headers, _requests):
    """Normalize different provider balance formats into a unified structure.

    Unified output fields (all optional):
      - balance_usd: remaining balance in USD
      - used_usd: total used in USD
      - limit_usd: total limit/quota in USD
      - currency: original currency if non-USD
      - balance_local: remaining in original currency
      - hard_limit_usd / total_usage_cents: legacy OpenAI format
      - raw: original response if nothing else matched
    """
    result = {}

    # ── Format 1: OpenAI /subscription style (hard_limit_usd) ──
    if 'hard_limit_usd' in billing:
        result['hard_limit_usd'] = billing['hard_limit_usd']
        result['limit_usd'] = billing['hard_limit_usd']
        result['soft_limit_usd'] = billing.get('soft_limit_usd')

        if balance_url.endswith('/subscription'):
            usage_url = balance_url.rsplit('/subscription', 1)[0] + '/usage'
            try:
                uresp = _requests.get(usage_url, headers=headers, timeout=15)
                uresp.raise_for_status()
                usage_data = uresp.json()
                if 'total_usage' in usage_data:
                    result['total_usage_cents'] = usage_data['total_usage']
                    result['used_usd'] = usage_data['total_usage'] / 100
                    result['balance_usd'] = result['limit_usd'] - result['used_usd']
            except Exception as e:
                logger.debug('[Balance] Usage fetch from %s failed (non-critical): %s', usage_url, e)
        return result

    # ── Format 2: DeepSeek /user/balance (balance_infos array) ──
    if 'balance_infos' in billing:
        infos = billing.get('balance_infos', [])
        result['is_available'] = billing.get('is_available', True)
        if infos:
            # Prefer USD, fallback to first entry
            info = infos[0]
            for bi in infos:
                if bi.get('currency', '').upper() == 'USD':
                    info = bi
                    break
            currency = info.get('currency', 'CNY')
            total = float(info.get('total_balance', 0))
            granted = float(info.get('granted_balance', 0))
            topped_up = float(info.get('topped_up_balance', 0))
            result['currency'] = currency
            result['balance_local'] = total
            result['granted_balance'] = granted
            result['topped_up_balance'] = topped_up
            # Approximate USD if CNY
            if currency.upper() == 'USD':
                result['balance_usd'] = total
            else:
                result['balance_usd'] = round(total / 7.2, 2)  # approximate CNY→USD
        return result

    # ── Format 3: OpenRouter /credits (data.total_credits / total_usage) ──
    credits_data = billing.get('data', billing)
    if 'total_credits' in credits_data:
        tc = float(credits_data.get('total_credits', 0))
        tu = float(credits_data.get('total_usage', 0))
        result['limit_usd'] = round(tc, 4)
        result['used_usd'] = round(tu, 4)
        result['balance_usd'] = round(tc - tu, 4)
        return result

    # ── Format 4: Generic — look for common field names ──
    for key in ('balance', 'remaining', 'credits', 'available_balance'):
        if key in billing:
            val = billing[key]
            if isinstance(val, (int, float)):
                result['balance_usd'] = float(val)
                return result
            if isinstance(val, str):
                try:
                    result['balance_usd'] = float(val)
                    return result
                except (ValueError, TypeError) as e:
                    logger.debug('[Config] balance_usd parse failed for key=%s: %s', key, e)

    # ── Fallback: return raw data ──
    result['raw'] = billing
    return result


@config_bp.route('/api/v1/providers/discover-models', methods=['POST'])
def discover_models_endpoint():
    """Auto-discover models from a provider's /v1/models endpoint."""
    data = parse_body()
    base_url = data.get('base_url', '').strip()
    api_key = data.get('api_key', '').strip()
    models_path = data.get('models_path', '').strip()

    if not base_url:
        return api_bad_request('base_url is required')
    if not api_key:
        return api_bad_request('api_key is required')

    try:
        from lib.llm_dispatch.discovery import discover_models, enrich_models_with_pricing
        models = discover_models(base_url, api_key, models_path=models_path)
        if not models:
            return jsonify({'ok': False, 'error': 'No models found at %s' % base_url}), 404

        models = enrich_models_with_pricing(models)
        logger.info('[Discovery] Endpoint returned %d models for %s', len(models), base_url)
        return api_ok({'models': models})
    except Exception as e:
        logger.error('[Discovery] Endpoint failed: %s', e, exc_info=True)
        return api_internal_error(e)


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
        return jsonify({'ok': False,
                       'error': "Template key '%s' not found in any JS file" % tpl_key}), 404

    return jsonify({
        'ok': True,
        'updated_files': updated_files,
        'model_count': len(clean_models),
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
        return jsonify({'ok': False, 'error': '探测出错: %s' % e}), 500


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


def _probe_one_cell(base_url, api_key, model_id, extra_headers, timeout,
                    protocol='openai'):
    """Send a minimal completion to test one (key, model) pair.

    Returns one of: 'ok', 'rate_limited', 'unauthorized', 'not_found',
    'unavailable', 'error' plus a short human-readable detail string.

    A 200 OR an HTTP 400 both count as ``ok`` — a 400 means the gateway
    accepted the (key, model) routing and only rejected the (deliberately
    tiny) request shape, which still proves the pair is reachable.

    ``protocol='anthropic'`` probes the Anthropic Messages API
    (``POST /v1/messages`` with ``x-api-key`` + ``anthropic-version``)
    instead of OpenAI Chat Completions. The status→verdict table is
    identical for both protocols.
    """
    from lib.http_client import http_post

    # ``max_tokens: 1`` is the floor — the probe only needs to learn whether
    # the gateway accepts the (key, model) routing, never the completion
    # itself, so output cost is held to a single token per attempt.
    if protocol == 'anthropic':
        from lib.llm.anthropic_outbound import (
            anthropic_headers, anthropic_messages_url,
        )
        url = anthropic_messages_url(base_url)
        headers = anthropic_headers(api_key, extra_headers)
        payload = {
            'model': model_id,
            'messages': [{'role': 'user', 'content': '.'}],
            'max_tokens': 1,
        }
    else:
        url = base_url.rstrip('/') + '/chat/completions'
        headers = {'Authorization': 'Bearer %s' % api_key} if api_key else {}
        if extra_headers:
            headers.update(extra_headers)
        payload = {
            'model': model_id,
            'messages': [{'role': 'user', 'content': 'hi'}],
            'max_tokens': 1,
            'stream': False,
        }
    try:
        resp = http_post(url, json=payload, headers=headers, timeout=timeout)
    except Exception as e:
        logger.warning('[CellProbe] %s @ %s network error: %s', model_id, base_url, e)
        return 'unavailable', 'network: %s' % str(e)[:120]

    code = resp.status_code
    try:
        body = resp.text[:400]
    except Exception:
        body = ''
    lower = body.lower()

    if code == 200 or code == 400:
        return 'ok', 'HTTP %d' % code
    if code == 429 or code == 402:
        return 'rate_limited', 'HTTP %d %.120s' % (code, body)
    if code in (401, 403):
        return 'unauthorized', 'HTTP %d %.120s' % (code, body)
    if code == 404 or 'model_not_found' in lower or 'does not exist' in lower or 'no such model' in lower:
        return 'not_found', 'HTTP %d %.120s' % (code, body)
    if code in (500, 502, 503, 504, 529):
        return 'unavailable', 'HTTP %d %.120s' % (code, body)
    return 'error', 'HTTP %d %.120s' % (code, body)


# Verdicts that warrant a retry (could be a transient blip), versus ones
# that are definitive on the first attempt (no point re-asking).
_PROBE_TRANSIENT = {'rate_limited', 'unavailable', 'error'}
_PROBE_DEFINITIVE = {'unauthorized', 'not_found'}


def _probe_cell_multi(base_url, api_key, model_id, extra_headers, timeout,
                      attempts=3, retry_delay=0.8, protocol='openai'):
    """Probe a cell up to ``attempts`` times to filter out FALSE 429s.

    Rationale: gateways routinely return a transient 429 / 5xx even for a
    (key, model) pair the key is fully entitled to. Flagging it after one
    shot would wrongly recommend disabling a working model. So:

      * A single ``ok`` on ANY attempt wins immediately — the earlier
        rate-limit was transient.
      * ``unauthorized`` / ``not_found`` are definitive → return at once.
      * Transient failures are retried after ``retry_delay`` seconds; if
        every attempt fails we return the LAST transient verdict with an
        ``(N/N attempts)`` note so the UI can show it was persistent.

    Returns ``(status, detail)`` like :func:`_probe_one_cell`.
    """
    attempts = max(1, int(attempts))
    last_status, last_detail = 'error', ''
    for i in range(attempts):
        status, detail = _probe_one_cell(base_url, api_key, model_id, extra_headers,
                                         timeout, protocol)
        if status == 'ok':
            note = '' if i == 0 else ' (ok on attempt %d/%d)' % (i + 1, attempts)
            return 'ok', detail + note
        if status in _PROBE_DEFINITIVE:
            return status, detail
        last_status, last_detail = status, detail
        if i < attempts - 1:
            _time.sleep(retry_delay)
    suffix = ' (%d/%d attempts failed)' % (attempts, attempts) if attempts > 1 else ''
    return last_status, '%.120s%s' % (last_detail, suffix)


# ══════════════════════════════════════════════════════
#  Access-Matrix Cell Probe — server-owned background task
# ══════════════════════════════════════════════════════
#
#  The probe sends a 1-token chat completion to every (key × concrete-id)
#  cell of a provider. Because each alias on a gateway can route to a
#  genuinely DIFFERENT upstream model, every alias is probed independently
#  (it is its own matrix row), not merged with its root.
#
#  The probe is a long-running fan-out, so it runs in a background thread
#  and its progress is **persisted to disk** under data/config/probe_cache/.
#  Closing the Settings dialog (or even restarting the server) does NOT lose
#  progress — the frontend re-attaches by provider id and keeps polling.
#  Only an explicit "retest" (force=true) discards the saved result and
#  starts over.

import hashlib  # noqa: E402
import threading  # noqa: E402
import time as _time  # noqa: E402
from concurrent.futures import ThreadPoolExecutor, as_completed  # noqa: E402

from lib.json_store import read_json, write_json_atomic  # noqa: E402

_CELL_PROBE_TASKS: dict = {}
_CELL_PROBE_LOCK = threading.Lock()
_PROBE_DISABLE_STATUSES = {'rate_limited', 'unauthorized', 'not_found', 'unavailable'}


def _probe_cache_path(provider_id: str) -> str:
    """Disk path for a provider's persisted probe snapshot."""
    safe = hashlib.sha1((provider_id or '').encode('utf-8')).hexdigest()[:16]
    return _config_path('probe_cache', '%s.json' % safe)


def _probe_cell_key(key_idx, model_id) -> str:
    return '%s::%s' % (key_idx, model_id)


def _persist_probe_task(task: dict):
    """Atomically write a public (key-free) snapshot of the task to disk."""
    try:
        write_json_atomic(_probe_cache_path(task['provider_id']),
                          _public_probe_snapshot(task), fsync=False)
    except Exception as e:
        logger.warning('[CellProbe] persist failed for %s: %s',
                       task.get('provider_id'), e)


def _public_probe_snapshot(task: dict) -> dict:
    """The serialisable, secret-free view of a probe task (for poll + disk)."""
    return {
        'provider_id': task['provider_id'],
        'status': task['status'],
        'started_at': task['started_at'],
        'finished_at': task['finished_at'],
        'total': task['total'],
        'done_count': task['done_count'],
        'attempts': task.get('attempts', 1),
        'cells': task['cells'],
        'summary': task['summary'],
        'error': task['error'],
    }


def _run_cell_probe_task(task: dict, work: list, timeout: int):
    """Background worker: fan out cell probes, updating + persisting progress."""
    provider_id = task['provider_id']
    base_url = task['_base_url']
    extra_headers = task['_extra_headers']
    protocol = task.get('_protocol', 'openai')
    attempts = task.get('attempts', 3)
    logger.info('[CellProbe] Started background probe for %s — %d cell(s), '
                'up to %d attempt(s) each (protocol=%s)', provider_id, len(work),
                attempts, protocol)

    def _run(item):
        key_idx, api_key, root, mid = item
        # Multi-attempt so a FALSE 429 / transient 5xx doesn't wrongly flag a
        # reachable cell. A single ok on any attempt wins.
        status, detail = _probe_cell_multi(base_url, api_key, mid, extra_headers,
                                           timeout, attempts=attempts,
                                           protocol=protocol)
        return {
            'key_idx': key_idx,
            'model_id': mid,
            'root_model_id': root,
            'status': status,
            'detail': detail,
            'recommend_disable': status in _PROBE_DISABLE_STATUSES,
        }

    last_persist = 0.0
    try:
        workers = min(8, len(work))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='cell-probe') as pool:
            futures = [pool.submit(_run, it) for it in work]
            for fut in as_completed(futures):
                if task.get('_abort'):
                    logger.info('[CellProbe] %s aborted', provider_id)
                    break
                try:
                    cell = fut.result()
                except Exception as e:
                    logger.error('[CellProbe] cell task raised: %s', e, exc_info=True)
                    continue
                with _CELL_PROBE_LOCK:
                    task['cells'][_probe_cell_key(cell['key_idx'], cell['model_id'])] = cell
                    task['done_count'] = len(task['cells'])
                    n_disable = sum(1 for c in task['cells'].values() if c['recommend_disable'])
                    task['summary'] = {'ok': task['done_count'] - n_disable, 'disable': n_disable}
                # Throttle disk writes: at most every ~1.5s during the run.
                now = _time.monotonic()
                if now - last_persist > 1.5:
                    last_persist = now
                    _persist_probe_task(task)
        with _CELL_PROBE_LOCK:
            task['status'] = 'done'
            task['finished_at'] = _time.time()
        _persist_probe_task(task)
        logger.info('[CellProbe] %s done: %d cells, %d flagged',
                    provider_id, task['done_count'], task['summary']['disable'])
    except Exception as e:
        logger.error('[CellProbe] background worker crashed for %s: %s',
                     provider_id, e, exc_info=True)
        with _CELL_PROBE_LOCK:
            task['status'] = 'error'
            task['error'] = str(e)[:300]
            task['finished_at'] = _time.time()
        _persist_probe_task(task)


@config_bp.route('/api/v1/providers/probe-cells/start', methods=['POST'])
def probe_provider_cells_start():
    """Start (or resume) a background per-(key × id) reachability probe.

    Body: ``{provider_id, base_url, api_keys: [str], extra_headers?: {},
    models: [{model_id, aliases?: [str]}], timeout?: int, attempts?: int,
    force?: bool}``. ``attempts`` (default 3, clamped 1..5) re-probes a
    transiently-failing cell to filter out FALSE 429s.

    Behaviour:
      * If a probe for ``provider_id`` is already running → return its
        live snapshot (does NOT restart).
      * Else if ``force`` is false and a persisted snapshot exists on disk
        → return that (resume after Settings was closed / server restarted).
      * Else start a fresh background probe and return its initial snapshot.

    Each alias is probed as its own cell (aliases are distinct models).
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
    force = bool(data.get('force'))

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

    if not force:
        cached = read_json(_probe_cache_path(provider_id), default=None)
        if isinstance(cached, dict) and cached.get('cells'):
            logger.info('[CellProbe] %s resumed from disk (%d cells, status=%s)',
                        provider_id, len(cached['cells']), cached.get('status'))
            return api_ok(cached)

    # Build the work list: one cell per (key_idx, concrete id). Every alias
    # is its own cell because aliases can be different upstream models.
    work = []
    for key_idx, api_key in enumerate(api_keys):
        for m in models:
            root = (m.get('model_id') or '').strip()
            if not root:
                continue
            for mid in [root] + [a for a in (m.get('aliases') or []) if a]:
                work.append((key_idx, api_key, root, mid))

    if not work:
        return api_bad_request('no testable (key, model) pairs')
    if len(work) > 400:
        return api_bad_request('too many cells to probe (%d > 400)' % len(work))

    task = {
        'provider_id': provider_id,
        'status': 'running',
        'started_at': _time.time(),
        'finished_at': None,
        'total': len(work),
        'done_count': 0,
        'cells': {},
        'summary': {'ok': 0, 'disable': 0},
        'error': None,
        'attempts': attempts,
        '_abort': False,
        '_base_url': base_url,
        '_extra_headers': extra_headers,
        '_protocol': protocol,
    }
    with _CELL_PROBE_LOCK:
        _CELL_PROBE_TASKS[provider_id] = task
    _persist_probe_task(task)

    th = threading.Thread(target=_run_cell_probe_task, args=(task, work, timeout),
                          name='cell-probe-%s' % provider_id[:24], daemon=True)
    th.start()

    logger.info('[CellProbe] Launched background probe for %s (%d cells, force=%s)',
                provider_id, len(work), force)
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

    data = parse_body()
    existing = _read_server_config()
    changes = []
    dispatch_reset_needed = False

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
            import lib.fetch.content_filter as _cf_mod
            _cf_mod.FILTER_ENABLED = bool(data['search']['llm_content_filter'])
            logger.info('[Config] LLM content filter → %s', _cf_mod.FILTER_ENABLED)
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

    # ── Persist to disk ──
    if not _write_server_config(existing):
        logger.error('[ServerConfig] Failed to write config file to %s', _SERVER_CONFIG_PATH)
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