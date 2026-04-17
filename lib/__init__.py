import json as _json
import os

# ── Public API ──
__all__ = [
    'LLM_API_KEYS', 'LLM_API_KEY', 'LLM_BASE_URL', 'LLM_MODEL',
    'FALLBACK_MODEL',
    'QWEN_MODEL',
    'GEMINI_MODEL', 'GEMINI_PRO_MODEL', 'GEMINI_PRO_PREVIEW_MODEL',
    'GEMINI_FLASH_PREVIEW_MODEL',
    'MINIMAX_MODEL',
    'DOUBAO_MODEL', 'CLAUDE_SONNET_MODEL',
    'IMAGE_GEN_MODEL', 'EMBEDDING_MODELS',
    'TRADING_ENABLED',
    'PPTX_TRANSLATE_ENABLED',
    'DEBUG_MODE',
    'FETCH_TOP_N', 'FETCH_TIMEOUT',
    'FETCH_MAX_CHARS_SEARCH', 'FETCH_MAX_CHARS_DIRECT',
    'FETCH_MAX_CHARS_PDF', 'FETCH_MAX_BYTES',
    'SKIP_DOMAINS', 'MODEL_PRICING',
    'QWEN_PRICING_CNY', 'DEFAULT_USD_CNY_RATE',
    'MT_PROVIDER_CONFIG',
]

# ══════════════════════════════════════════════════════════
#  Server Config Persistence
# ══════════════════════════════════════════════════════════
# On startup, this module reads data/config/server_config.json FIRST.
# Values saved there override env-var defaults so that models/providers
# added via the Settings UI survive a server restart.
# Each project copy has its own isolated config — no cross-contamination.
#
# Priority chain:  ENV VAR (explicit)  >  server_config.json  >  hardcoded default

from lib.config_dir import config_path as _config_path

_SERVER_CONFIG_PATH = _config_path('server_config.json')

def _load_server_config():
    """Read data/config/server_config.json, return dict or {} on any error.

    This is called ONCE at import time. Auto-migrates from legacy
    ~/.chatui/ path on first run (handled by lib.config_dir import).
    """
    try:
        if os.path.isfile(_SERVER_CONFIG_PATH):
            with open(_SERVER_CONFIG_PATH) as f:
                return _json.load(f)
    except Exception as _e:
        import logging as _logging
        _logging.getLogger(__name__).debug('Could not load server config: %s', _e)
    return {}

_SAVED_CONFIG = _load_server_config()

def _cfg(env_key, saved_key, default):
    """Resolve a config value: env var > server_config.json > default.

    Only env vars that are EXPLICITLY SET override saved config.
    """
    env_val = os.environ.get(env_key)
    if env_val is not None:
        return env_val
    # Check saved config — look in 'presets' mapping and 'models' dict
    saved_presets = _SAVED_CONFIG.get('presets', {})
    saved_models = _SAVED_CONFIG.get('models', {})
    if saved_key in saved_presets:
        return saved_presets[saved_key]
    if saved_key in saved_models:
        return saved_models[saved_key]
    return default

# ── API Keys: flat list, all keys are equal ──
# Preferred: LLM_API_KEYS=key1,key2,key3  (comma-separated, any number)
# Legacy single-var: LLM_API_KEY still works (for 1 key only)
_DEFAULT_KEYS = [
    # Add your API keys here, or set LLM_API_KEYS env var
]  # No hardcoded keys — set LLM_API_KEYS env var or configure via Settings UI

def _parse_api_keys():
    """Build a flat list of API keys from environment variables and saved config.

    Priority: LLM_API_KEYS env var > saved provider keys > LLM_API_KEY env var > defaults.
    """
    keys_env = os.environ.get('LLM_API_KEYS', '')
    if keys_env:
        return [k.strip() for k in keys_env.split(',') if k.strip()]
    # Check saved providers for keys
    saved_providers = _SAVED_CONFIG.get('providers', [])
    if saved_providers:
        all_keys = []
        for p in saved_providers:
            if p.get('enabled', True):
                all_keys.extend(p.get('api_keys', []))
        if all_keys:
            return all_keys
    # Legacy: single env var
    single = os.environ.get('LLM_API_KEY', '')
    if single:
        return [single]
    # Default hardcoded keys
    return list(_DEFAULT_KEYS)

LLM_API_KEYS = _parse_api_keys()
LLM_API_KEY  = LLM_API_KEYS[0] if LLM_API_KEYS else ''  # backward compat alias

def _resolve_base_url():
    """Resolve LLM_BASE_URL: env var > first saved provider > default."""
    env_val = os.environ.get('LLM_BASE_URL')
    if env_val is not None:
        return env_val
    for p in _SAVED_CONFIG.get('providers', []):
        if p.get('enabled', True) and p.get('base_url'):
            return p['base_url']
    return 'https://api.openai.com/v1'

LLM_BASE_URL    = _resolve_base_url()
LLM_MODEL       = _cfg('LLM_MODEL', 'opus', 'gpt-4o')

# ── Fallback model — used when the primary model fails ──
# Configurable via Settings UI > 显示 > 模型默认. Empty string = disabled.
FALLBACK_MODEL  = _cfg('FALLBACK_MODEL', 'fallback_model', '')
QWEN_MODEL      = _cfg('QWEN_MODEL', 'qwen', '')
GEMINI_MODEL    = _cfg('GEMINI_MODEL', 'gemini', '')
GEMINI_PRO_MODEL = os.environ.get('GEMINI_PRO_MODEL', '')
GEMINI_PRO_PREVIEW_MODEL = os.environ.get('GEMINI_PRO_PREVIEW_MODEL', '')
GEMINI_FLASH_PREVIEW_MODEL = _cfg('GEMINI_FLASH_PREVIEW_MODEL', 'gemini_flash', '')
MINIMAX_MODEL   = _cfg('MINIMAX_MODEL', 'minimax', '')
DOUBAO_MODEL    = _cfg('DOUBAO_MODEL', 'doubao', '')
CLAUDE_SONNET_MODEL = os.environ.get('CLAUDE_SONNET_MODEL', '')

# ── Image generation model ──
IMAGE_GEN_MODEL = _cfg('IMAGE_GEN_MODEL', 'IMAGE_GEN_MODEL', '')

# ── Embedding models ──
# Benchmarked: all three work. v4 is fastest/recommended.
_saved_embed = _SAVED_CONFIG.get('models', {}).get('EMBEDDING_MODELS')
EMBEDDING_MODELS = _saved_embed if isinstance(_saved_embed, list) and _saved_embed else [
    'text-embedding-3-small',     # 1536d — works on OpenAI and most compatible APIs
    'text-embedding-3-large',     # 3072d — highest quality
]


# ── Machine Translation Provider (optional, for faster/cheaper translation) ──
# When configured, translation uses a dedicated MT API (e.g. NiuTrans) instead
# of the cheap LLM model.  Config stored in server_config.json under 'mt_provider'.
# Priority: server_config.json > default (disabled)
def _resolve_mt_provider_config():
    """Resolve MT provider config from server_config.json.

    Returns dict with: provider, api_url, api_key, app_id, enabled
    """
    mt = _SAVED_CONFIG.get('mt_provider', {})
    if not isinstance(mt, dict):
        return {}
    return {
        'provider': mt.get('provider', 'niutrans'),
        'api_url': mt.get('api_url', ''),
        'api_key': mt.get('api_key', ''),
        'app_id': mt.get('app_id', ''),
        'enabled': bool(mt.get('enabled', False)),
    }

MT_PROVIDER_CONFIG = _resolve_mt_provider_config()


# ── Feature flag resolver (DRY helper) ──
# All boolean flags follow: env-var > data/config/features.json > default
def _resolve_feature_flag(env_key, json_key, default):
    """Resolve a boolean feature flag.

    Priority: env-var > data/config/features.json > default.
    Each project copy has its own feature flags.
    """
    env_val = os.environ.get(env_key)
    if env_val is not None:
        return env_val == '1'
    _features_path = _config_path('features.json')
    try:
        if os.path.isfile(_features_path):
            with open(_features_path) as f:
                feats = _json.load(f)
            if isinstance(feats, dict) and json_key in feats:
                return bool(feats[json_key])
    except Exception as _e:
        import logging as _logging
        _logging.getLogger(__name__).debug('Could not read features.json for %s: %s', json_key, _e)
    return default

TRADING_ENABLED = _resolve_feature_flag('TRADING_ENABLED', 'trading_enabled', False)
PPTX_TRANSLATE_ENABLED = _resolve_feature_flag('PPTX_TRANSLATE_ENABLED', 'pptx_translate_enabled', False)
DEBUG_MODE = _resolve_feature_flag('DEBUG_MODE', 'debug_mode', False)
# Cache Extended TTL: 1h TTL for stable prefix (system+tools), 5m for tail
CACHE_EXTENDED_TTL = _resolve_feature_flag('CACHE_EXTENDED_TTL', 'cache_extended_ttl', True)

# ── Fetch / search settings ──
# Priority: ENV VAR > server_config.json search section > hardcoded default
_search_cfg = _SAVED_CONFIG.get('search', {})

def _fetch_cfg(env_key, saved_key, default):
    """Resolve a fetch/search integer setting.  0 is a valid value (e.g. PDF no-limit)."""
    env = os.environ.get(env_key)
    if env is not None and env != '':
        return int(env)
    saved = _search_cfg.get(saved_key)
    if saved is not None:
        return int(saved)
    return default

FETCH_TOP_N            = _fetch_cfg('FETCH_TOP_N', 'fetch_top_n', 6)
FETCH_TIMEOUT          = _fetch_cfg('FETCH_TIMEOUT', 'fetch_timeout', 15)
FETCH_MAX_CHARS_SEARCH = _fetch_cfg('FETCH_MAX_CHARS_SEARCH', 'max_chars_search', 60000)
FETCH_MAX_CHARS_DIRECT = _fetch_cfg('FETCH_MAX_CHARS_DIRECT', 'max_chars_direct', 200000)
FETCH_MAX_CHARS_PDF    = _fetch_cfg('FETCH_MAX_CHARS_PDF', 'max_chars_pdf', 0)
FETCH_MAX_BYTES        = _fetch_cfg('FETCH_MAX_BYTES', 'max_bytes', 20*1024*1024)

SKIP_DOMAINS = {
    'youtube.com','youtu.be','twitter.com','x.com',
    'facebook.com','instagram.com','tiktok.com',
    'linkedin.com','discord.com',
}
# Apply saved skip_domains on top of defaults
if isinstance(_search_cfg.get('skip_domains'), list):
    SKIP_DOMAINS = set(_search_cfg['skip_domains'])

# Apply saved llm_content_filter setting at startup (not just on hot-reload)
if 'llm_content_filter' in _search_cfg:
    try:
        import lib.fetch.content_filter as _cf_mod
        _cf_mod.FILTER_ENABLED = bool(_search_cfg['llm_content_filter'])
    except Exception as _e:
        import logging as _logging
        _logging.getLogger(__name__).debug('Could not apply saved llm_content_filter: %s', _e)

# ── Model pricing tables — now live in lib/pricing.py ──
# Imported here for backward compatibility (all consumers use `from lib import MODEL_PRICING`)
from lib.pricing import DEFAULT_USD_CNY_RATE, MODEL_PRICING, QWEN_PRICING_CNY  # noqa: E402




# ══════════════════════════════════════════════════════════
#  Hot Reload — update module-level variables from disk
# ══════════════════════════════════════════════════════════
# Called by routes/config.py after saving settings so that ALL
# consumers (who use ``import lib as _lib; _lib.X``) see the new
# values immediately without a server restart.

def reload_config():
    """Re-read server_config.json and update all module-level variables in place.

    This makes Settings UI changes take effect immediately for:
      - Model names (LLM_MODEL, QWEN_MODEL, GEMINI_MODEL, etc.)
      - API keys (LLM_API_KEYS, LLM_API_KEY)
      - Base URL (LLM_BASE_URL)
      - Fetch settings (FETCH_TOP_N, FETCH_TIMEOUT, FETCH_MAX_CHARS_*, etc.)
      - Feature flags (TRADING_ENABLED)

    The dispatcher (lib/llm_dispatch) is reset separately by the caller.
    """
    import sys
    _mod = sys.modules[__name__]

    global _SAVED_CONFIG
    _SAVED_CONFIG = _load_server_config()

    # ── Re-resolve all config values ──
    _mod.LLM_API_KEYS = _parse_api_keys()
    _mod.LLM_API_KEY = _mod.LLM_API_KEYS[0] if _mod.LLM_API_KEYS else ''
    _mod.LLM_BASE_URL = _resolve_base_url()
    _mod.LLM_MODEL = _cfg('LLM_MODEL', 'opus', 'gpt-4o')
    _mod.FALLBACK_MODEL = _cfg('FALLBACK_MODEL', 'fallback_model', '')
    _mod.QWEN_MODEL = _cfg('QWEN_MODEL', 'qwen', '')
    _mod.GEMINI_MODEL = _cfg('GEMINI_MODEL', 'gemini', '')
    _mod.GEMINI_PRO_MODEL = os.environ.get('GEMINI_PRO_MODEL', '')
    _mod.GEMINI_PRO_PREVIEW_MODEL = os.environ.get('GEMINI_PRO_PREVIEW_MODEL', '')
    _mod.GEMINI_FLASH_PREVIEW_MODEL = _cfg('GEMINI_FLASH_PREVIEW_MODEL', 'gemini_flash', '')
    _mod.MINIMAX_MODEL = _cfg('MINIMAX_MODEL', 'minimax', '')
    _mod.DOUBAO_MODEL = _cfg('DOUBAO_MODEL', 'doubao', '')
    _mod.CLAUDE_SONNET_MODEL = os.environ.get('CLAUDE_SONNET_MODEL', '')
    _mod.IMAGE_GEN_MODEL = _cfg('IMAGE_GEN_MODEL', 'IMAGE_GEN_MODEL', '')

    # Embedding models
    _saved_embed = _SAVED_CONFIG.get('models', {}).get('EMBEDDING_MODELS')
    _mod.EMBEDDING_MODELS = _saved_embed if isinstance(_saved_embed, list) and _saved_embed else [
        'text-embedding-3-small',
        'text-embedding-3-large',
    ]

    # Fetch settings — same priority chain as module init: ENV > saved > default
    _search = _SAVED_CONFIG.get('search', {})
    def _rcfg(env_key, saved_key, default):
        env = os.environ.get(env_key)
        if env is not None and env != '':
            return int(env)
        saved_val = _search.get(saved_key)
        if saved_val is not None:
            return int(saved_val)
        return default
    _mod.FETCH_TOP_N = _rcfg('FETCH_TOP_N', 'fetch_top_n', 6)
    _mod.FETCH_TIMEOUT = _rcfg('FETCH_TIMEOUT', 'fetch_timeout', 15)
    _mod.FETCH_MAX_CHARS_SEARCH = _rcfg('FETCH_MAX_CHARS_SEARCH', 'max_chars_search', 60000)
    _mod.FETCH_MAX_CHARS_DIRECT = _rcfg('FETCH_MAX_CHARS_DIRECT', 'max_chars_direct', 200000)
    _mod.FETCH_MAX_CHARS_PDF = _rcfg('FETCH_MAX_CHARS_PDF', 'max_chars_pdf', 0)
    _mod.FETCH_MAX_BYTES = _rcfg('FETCH_MAX_BYTES', 'max_bytes', 20*1024*1024)
    if 'skip_domains' in _search and isinstance(_search['skip_domains'], list):
        _mod.SKIP_DOMAINS = set(_search['skip_domains'])

    # Feature flags
    _mod.TRADING_ENABLED = _resolve_feature_flag('TRADING_ENABLED', 'trading_enabled', False)
    _mod.PPTX_TRANSLATE_ENABLED = _resolve_feature_flag('PPTX_TRANSLATE_ENABLED', 'pptx_translate_enabled', False)
    _mod.DEBUG_MODE = _resolve_feature_flag('DEBUG_MODE', 'debug_mode', False)
    _mod.CACHE_EXTENDED_TTL = _resolve_feature_flag('CACHE_EXTENDED_TTL', 'cache_extended_ttl', True)

    # Machine translation provider
    _mod.MT_PROVIDER_CONFIG = _resolve_mt_provider_config()

    # Model defaults (from model_defaults section)
    _md = _SAVED_CONFIG.get('model_defaults', {})
    if _md.get('fallback_model') is not None:
        _mod.FALLBACK_MODEL = _md['fallback_model'] or ''
    if _md.get('default_model'):
        _mod.LLM_MODEL = _md['default_model']

    import logging as _logging
    _logging.getLogger(__name__).info(
        '[Config] Hot-reloaded: model=%s, base_url=%.60s, keys=%d, '
        'fetch_top_n=%d, timeout=%d, max_chars_search=%d, max_chars_direct=%d',
        _mod.LLM_MODEL, _mod.LLM_BASE_URL, len(_mod.LLM_API_KEYS),
        _mod.FETCH_TOP_N, _mod.FETCH_TIMEOUT,
        _mod.FETCH_MAX_CHARS_SEARCH, _mod.FETCH_MAX_CHARS_DIRECT,
    )
