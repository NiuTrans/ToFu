"""routes/config.py — Server configuration API endpoints.

Extracted from routes/common.py for better separation of concerns.
Handles provider management, model discovery, Feishu config, proxy settings.
"""

import json
import os
import sys

from flask import Blueprint, jsonify, request

from lib.config_dir import config_path as _config_path
from lib.log import get_logger

logger = get_logger(__name__)

config_bp = Blueprint('config', __name__)

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
#  Cheap Tag Re-evaluation
# ══════════════════════════════════════════════════════

def _reeval_cheap_tags(providers: list):
    """Re-evaluate 'cheap' capability on all provider models using real pricing.

    Fixes stale cheap tags from old discovery runs that used the blended
    threshold or name-heuristic fallback.  Uses the strict two-sided check:
    input < Sonnet input ($3/1M) AND output < Sonnet output ($15/1M).
    """
    from lib.llm_dispatch.config import is_model_cheap

    for prov in providers:
        for m in prov.get('models', []):
            mid = m.get('model_id', '')
            if not mid:
                continue
            caps = set(m.get('capabilities', []))
            # Skip non-chat models
            if 'image_gen' in caps or 'embedding' in caps:
                continue
            cheap = is_model_cheap(
                mid,
                fallback_cost_per_1k=m.get('cost'),
                input_price=m.get('input_price'),
                output_price=m.get('output_price'),
            )
            if cheap and 'cheap' not in caps:
                caps.add('cheap')
                m['capabilities'] = sorted(caps)
            elif not cheap and 'cheap' in caps:
                caps.discard('cheap')
                m['capabilities'] = sorted(caps)


# ══════════════════════════════════════════════════════
#  Provider Defaults Builder
# ══════════════════════════════════════════════════════

def _build_default_providers():
    """Build default provider config from environment/hardcoded values."""
    import lib as _lib
    from lib.llm_dispatch.config import DEFAULT_SLOT_CONFIGS, MODEL_ALIAS_GROUPS, is_model_cheap

    base_url = getattr(_lib, 'LLM_BASE_URL', '')
    api_keys = list(getattr(_lib, 'LLM_API_KEYS', []))

    def _auto_cheap(model_id, caps_set, cost):
        if 'image_gen' not in caps_set and 'embedding' not in caps_set and 'cheap' not in caps_set:
            if is_model_cheap(model_id, fallback_cost_per_1k=cost):
                caps_set.add('cheap')
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

@config_bp.route('/api/server-config')
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

    model_limits = saved.get('model_limits', {})
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

    return jsonify({
        'providers': providers, 'presets': presets,
        'models': models, 'search': search_info,
        'server_info': server_info,
        'feishu': feishu_info,
        'dropdown_models': dropdown_models,
        'hidden_models': hidden_models,
        'hidden_ig_models': hidden_ig_models,
        'model_pricing': model_pricing,
        'model_limits': model_limits,
        'model_defaults': model_defaults,
        'network': network_info,
        'mt_provider': mt_provider_info,
    })


@config_bp.route('/api/feishu/status')
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
        return jsonify({'ok': False, 'error': str(e)}), 500


@config_bp.route('/api/provider-balance', methods=['POST'])
def check_provider_balance():
    """Proxy a balance/billing check to a provider's billing API."""
    import requests as _requests

    data = request.get_json(silent=True) or {}
    balance_url = (data.get('balance_url') or '').strip()
    api_key = (data.get('api_key') or '').strip()

    if not balance_url:
        return jsonify({'ok': False, 'error': 'No balance_url provided'}), 400
    if not api_key:
        return jsonify({'ok': False, 'error': 'No api_key provided'}), 400
    if not balance_url.startswith('https://'):
        return jsonify({'ok': False, 'error': 'balance_url must use HTTPS'}), 400

    headers = {'Authorization': 'Bearer %s' % api_key}
    logger.info('[Balance] Checking balance at %.200s', balance_url)

    try:
        resp = _requests.get(balance_url, headers=headers, timeout=15)
        resp.raise_for_status()
        billing = resp.json()
    except _requests.Timeout:
        logger.warning('[Balance] Timeout fetching %s', balance_url)
        return jsonify({'ok': False, 'error': 'Request timed out (15s)'}), 504
    except _requests.RequestException as e:
        logger.warning('[Balance] Request failed for %s: %s', balance_url, e)
        return jsonify({'ok': False, 'error': 'Request failed: %s' % e}), 502
    except (ValueError, TypeError) as e:
        logger.warning('[Balance] Invalid JSON from %s: %s', balance_url, e)
        return jsonify({'ok': False, 'error': 'Invalid JSON response'}), 502

    result = _normalize_balance(billing, balance_url, headers, _requests)

    logger.info('[Balance] Result: %s', {k: v for k, v in result.items() if k != 'raw'})
    return jsonify({'ok': True, 'balance': result})


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


@config_bp.route('/api/discover-models', methods=['POST'])
def discover_models_endpoint():
    """Auto-discover models from a provider's /v1/models endpoint."""
    data = request.get_json(silent=True) or {}
    base_url = data.get('base_url', '').strip()
    api_key = data.get('api_key', '').strip()
    models_path = data.get('models_path', '').strip()

    if not base_url:
        return jsonify({'ok': False, 'error': 'base_url is required'}), 400
    if not api_key:
        return jsonify({'ok': False, 'error': 'api_key is required'}), 400

    try:
        from lib.llm_dispatch.discovery import discover_models, enrich_models_with_pricing
        models = discover_models(base_url, api_key, models_path=models_path)
        if not models:
            return jsonify({'ok': False, 'error': 'No models found at %s' % base_url}), 404

        models = enrich_models_with_pricing(models)
        logger.info('[Discovery] Endpoint returned %d models for %s', len(models), base_url)
        return jsonify({'ok': True, 'models': models})
    except Exception as e:
        logger.error('[Discovery] Endpoint failed: %s', e, exc_info=True)
        return jsonify({'ok': False, 'error': str(e)}), 500


@config_bp.route('/api/update-provider-template', methods=['POST'])
def update_provider_template():
    """Persist discovered models back into the hardcoded JS provider template.

    Accepts ``{key, models}`` — finds the template with matching ``key`` in
    ``settings.js`` (and ``bundle-*.js`` if present), replaces its ``models``
    array with the new list, preserving all other template fields and comments.

    The models array is formatted to match the existing code style.
    """
    import glob
    import re

    data = request.get_json(silent=True) or {}
    tpl_key = (data.get('key') or '').strip()
    models = data.get('models')

    if not tpl_key:
        return jsonify({'ok': False, 'error': 'key is required'}), 400
    if not models or not isinstance(models, list):
        return jsonify({'ok': False, 'error': 'models list is required'}), 400

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
        return jsonify({'ok': False, 'error': 'No valid models in list'}), 400

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


@config_bp.route('/api/provider-probe', methods=['POST'])
def probe_provider_endpoint():
    """One-shot provider auto-setup: discover models, detect brand, find balance URL.

    Accepts ``{base_url, api_key, models_path?}`` and returns a complete
    provider configuration ready to be inserted into the providers list.
    """
    data = request.get_json(silent=True) or {}
    base_url = (data.get('base_url') or '').strip()
    api_key = (data.get('api_key') or '').strip()
    models_path = (data.get('models_path') or '').strip()

    if not base_url:
        return jsonify({'ok': False, 'error': '请填写 API 地址 (Base URL)'}), 400
    if not api_key:
        return jsonify({'ok': False, 'error': '请填写 API 密钥'}), 400

    logger.info('[Probe] Provider probe requested for %.200s', base_url)

    try:
        from lib.llm_dispatch.discovery import probe_provider
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


@config_bp.route('/api/provider-templates')
def get_provider_templates():
    """Serve external provider templates from static/provider_templates/*.json."""
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
            if isinstance(tpl, dict) and tpl.get('key') and tpl.get('models'):
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


@config_bp.route('/api/server-config', methods=['POST'])
def save_server_config():
    """POST — save server configuration changes.

    All settings take effect immediately (hot-reload) — no server restart needed.
    The flow: write config to disk → reload_config() updates module-level vars →
    reset_dispatcher() rebuilds LLM slot pool → hot-reload Feishu/proxy/etc.
    """
    import lib as _lib
    from lib.log import audit_log

    data = request.get_json(silent=True) or {}
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
        return jsonify({'ok': False, 'error': 'Failed to write config file'}), 500

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

    return jsonify({'ok': True, 'needs_restart': False, 'changes': changes})
