import json as _json
import os

# ── Public API ──
__all__ = [
    'LLM_API_KEYS', 'LLM_BASE_URL', 'LLM_MODEL',
    'FALLBACK_MODEL',
    'QWEN_MODEL',
    'GEMINI_MODEL', 'GEMINI_PRO_MODEL', 'GEMINI_PRO_PREVIEW_MODEL',
    'GEMINI_FLASH_PREVIEW_MODEL',
    'MINIMAX_MODEL',
    'DOUBAO_MODEL', 'CLAUDE_SONNET_MODEL',
    'IMAGE_GEN_MODEL',
    'PPTX_TRANSLATE_ENABLED',
    'DEBUG_MODE',
    'OPTIMIZER_ENABLED',
    'SCHEDULER_ALLOW_CODE_EXEC',
    'ARTIFACTS_ENABLED',
    'FETCH_TOP_N', 'FETCH_TIMEOUT',
    'FETCH_MAX_CHARS_SEARCH', 'FETCH_MAX_CHARS_DIRECT',
    'FETCH_MAX_CHARS_PDF', 'FETCH_MAX_BYTES',
    'SKIP_DOMAINS', 'MODEL_PRICING',
    'QWEN_PRICING_CNY', 'DEFAULT_USD_CNY_RATE',
    'PROVIDER_PRICING', 'lookup_pricing',
    'set_provider_pricing', 'clear_provider_pricing',
    'MT_PROVIDER_CONFIG',
]

# ══════════════════════════════════════════════════════════
#  Server Config Persistence
# ══════════════════════════════════════════════════════════
# On startup, this module reads data/config/server_config.json once for
# miscellaneous non-provider settings and legacy model-default projections.
# Provider access and credentials live exclusively in model-routing v2.
# Each project copy has its own isolated config — no cross-contamination.
#
# Priority chain:  ENV VAR (explicit)  >  server_config.json  >  hardcoded default

from lib.config_dir import config_path as _config_path

_SERVER_CONFIG_PATH = _config_path('server_config.json')

def _load_server_config():
    """Read data/config/server_config.json, return dict or {} on any error.

    Called ONCE at import time. All persistent config lives in-tree under
    ``<project>/data/config/``; see ``lib.config_dir``.
    """
    try:
        with open(_SERVER_CONFIG_PATH, encoding='utf-8') as config_file:
            loaded = _json.load(config_file)
            return loaded if isinstance(loaded, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as _e:
        import logging as _logging
        _logging.getLogger(__name__).debug('Could not load server config: %s', _e)
    return {}

_SAVED_CONFIG = _load_server_config()

def _cfg(env_key, saved_key, default, *, empty_env_is_unset=False):
    """Resolve a config value: env var > server_config.json > default.

    Only env vars that are EXPLICITLY SET override saved config. Callers whose
    values cannot be meaningfully empty may opt out of an empty environment
    override. This matters for launchers such as Compose that represent an
    omitted optional interpolation as an empty string.
    """
    env_val = os.environ.get(env_key)
    if env_val is not None and (env_val or not empty_env_is_unset):
        return env_val
    # Check saved config — look in 'presets' mapping and 'models' dict
    saved_presets = _SAVED_CONFIG.get('presets', {})
    saved_models = _SAVED_CONFIG.get('models', {})
    if saved_key in saved_presets:
        return saved_presets[saved_key]
    if saved_key in saved_models:
        return saved_models[saved_key]
    return default

# ── Legacy process-wide API environment ──
# These values support explicit headless/direct-library callers only. HTTP
# requests and Settings-managed providers route through owner-scoped v2.
# Preferred: LLM_API_KEYS=key1,key2,key3  (comma-separated, any number)
# Legacy single-var: LLM_API_KEY still works (for 1 key only)
_DEFAULT_KEYS = []  # No hardcoded or persisted-provider fallback.

def _parse_api_keys():
    """Build legacy direct-call keys strictly from explicit environment."""
    keys_env = os.environ.get('LLM_API_KEYS', '')
    if keys_env:
        return [k.strip() for k in keys_env.split(',') if k.strip()]
    # Legacy: single env var
    single = os.environ.get('LLM_API_KEY', '')
    if single:
        return [single]
    # Default hardcoded keys
    return list(_DEFAULT_KEYS)

LLM_API_KEYS = _parse_api_keys()

def _resolve_base_url():
    """Resolve the legacy direct-call base URL from environment only."""
    env_val = os.environ.get('LLM_BASE_URL')
    # Compose renders an absent optional interpolation as ''. Treat it as
    # unset, but never consult old persisted provider rows: doing so would
    # collapse every authenticated owner into one process-global credential.
    if env_val:
        return env_val
    return 'https://api.openai.com/v1'

LLM_BASE_URL    = _resolve_base_url()
LLM_MODEL       = _cfg(
    'LLM_MODEL', 'opus', 'gpt-4o', empty_env_is_unset=True)

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
# All boolean flags follow: env-var > data/config/features.json > default.
# The file is one launch snapshot: reading it once avoids one FUSE stat/open
# pair per flag while preserving explicit reload_config() hot application.
_FEATURES_CONFIG_PATH = _config_path('features.json')


def _load_features_config():
    try:
        with open(_FEATURES_CONFIG_PATH, encoding='utf-8') as features_file:
            loaded = _json.load(features_file)
            return loaded if isinstance(loaded, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as _e:
        import logging as _logging
        _logging.getLogger(__name__).debug(
            'Could not read features.json: %s', _e)
        return {}


_SAVED_FEATURES = _load_features_config()


def _resolve_feature_flag(env_key, json_key, default):
    """Resolve a boolean feature flag.

    Priority: env-var > data/config/features.json > default.
    Each project copy has its own feature flags.
    """
    env_val = os.environ.get(env_key)
    if env_val is not None:
        return env_val == '1'
    if json_key in _SAVED_FEATURES:
        return bool(_SAVED_FEATURES[json_key])
    return default

PPTX_TRANSLATE_ENABLED = _resolve_feature_flag('PPTX_TRANSLATE_ENABLED', 'pptx_translate_enabled', False)
DEBUG_MODE = _resolve_feature_flag('DEBUG_MODE', 'debug_mode', False)
# Cache Extended TTL: 1h TTL for stable prefix (system+tools), 5m for tail
CACHE_EXTENDED_TTL = _resolve_feature_flag('CACHE_EXTENDED_TTL', 'cache_extended_ttl', True)
# Daily Optimizer: nightly LLM-driven improvement loop. ON by default —
# users can disable it in Settings if they don't want autonomous changes
# (e.g. auto-applied block_search_domain).
OPTIMIZER_ENABLED = _resolve_feature_flag('OPTIMIZER_ENABLED', 'optimizer_enabled', True)
# Scheduler code-execution gate: when False, scheduled task_type='command'/'python'
# tasks can neither be created nor executed (the LLM-driven persistent
# arbitrary-code-execution seam is locked down). Built-in 'pg_backup' /
# 'optimizer' / 'prompt' / 'agent' types are unaffected. Default True
# preserves existing behavior; set SCHEDULER_ALLOW_CODE_EXEC=0 (or
# scheduler_allow_code_exec: false in features.json) to lock down.
SCHEDULER_ALLOW_CODE_EXEC = _resolve_feature_flag(
    'SCHEDULER_ALLOW_CODE_EXEC', 'scheduler_allow_code_exec', True)
# Renderable chat artifacts (md/html/svg) — see lib/artifacts/.  Default
# ON; set ``ARTIFACTS_ENABLED=0`` (env) or ``artifacts_enabled: false``
# in features.json to disable producers + chip rendering.  Routes stay
# registered for read-only access to existing rows.
ARTIFACTS_ENABLED = _resolve_feature_flag('ARTIFACTS_ENABLED', 'artifacts_enabled', True)

# ── Plugin feature flags (tofu.flags entry-point group) ──
# Optional features (e.g. the extracted trading subsystem) declare their own
# boolean flag via the tofu.flags entry point. We resolve each here and expose
# it as a module attribute under its env_key (e.g. TRADING_ENABLED) so existing
# `getattr(lib, 'TRADING_ENABLED', False)` consumers keep working when the
# plugin is installed, and harmlessly read False when it is not. Cheap: loading
# a flag entry point only imports the plugin's tiny flags module, not its code.
def _load_plugin_flags():
    try:
        from lib.feature_registry import discover_flag_plugins, registered_flags, mark_boot_enabled
        discover_flag_plugins()
        _mod = __import__('sys').modules[__name__]
        for _flag in registered_flags():
            _val = _resolve_feature_flag(_flag.env_key, _flag.json_key, _flag.default)
            setattr(_mod, _flag.env_key, _val)
            mark_boot_enabled(_flag.json_key, bool(_val))
    except Exception as _e:
        import logging as _logging
        _logging.getLogger(__name__).debug('Plugin feature-flag discovery skipped: %s', _e)

_load_plugin_flags()

# ── Fetch / search settings ──
# Priority: ENV VAR > server_config.json search section > hardcoded default
from lib.search_profiles import resolve_search_profile as _resolve_search_profile

_search_cfg_raw = _SAVED_CONFIG.get('search', {})
_search_cfg = _resolve_search_profile(
    _search_cfg_raw if isinstance(_search_cfg_raw, dict) else {})
SEARCH_PROFILE = _search_cfg['profile']
SEARCH_OVERRIDES = dict(_search_cfg.get('overrides') or {})
SEARCH_DEEPEN_ENABLED = bool(_search_cfg.get('deepen_enabled', False))

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

# Resolved LLM-content-filter toggle (env > saved config > default ON).
# tofu-search's pipeline reads this via lib/search_bridge.sync_search_config();
# the optional bridge is installed at the first real search/fetch use.
LLM_CONTENT_FILTER_ENABLED = (
    os.environ.get('FETCH_LLM_FILTER', '1') == '1'
    if os.environ.get('FETCH_LLM_FILTER') is not None
    else bool(_search_cfg.get('llm_content_filter', True))
)

# ── Model pricing compatibility ──
# Keep the historical ``from lib import MODEL_PRICING`` surface without making
# every unrelated ``lib.*`` import load HTTP clients and the online refresh
# implementation.  Pricing is materialized on first actual pricing access.
_LAZY_PRICING_EXPORTS = frozenset({
    'DEFAULT_USD_CNY_RATE',
    'MODEL_PRICING',
    'PROVIDER_PRICING',
    'QWEN_PRICING_CNY',
    'clear_provider_pricing',
    'lookup_pricing',
    'set_provider_pricing',
})


def __getattr__(name):
    if name not in _LAZY_PRICING_EXPORTS:
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    from importlib import import_module
    pricing = import_module('lib.pricing')
    value = getattr(pricing, name)
    globals()[name] = value
    return value




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
      - Explicit environment API keys/base URL for direct-library callers
      - Fetch settings (FETCH_TOP_N, FETCH_TIMEOUT, FETCH_MAX_CHARS_*, etc.)
      - Feature flags (TRADING_ENABLED)

    The dispatcher (lib/llm_dispatch) is reset separately by the caller.
    """
    import sys
    _mod = sys.modules[__name__]

    global _SAVED_CONFIG, _SAVED_FEATURES
    _SAVED_CONFIG = _load_server_config()
    _SAVED_FEATURES = _load_features_config()

    # ── Re-resolve all config values ──
    _mod.LLM_API_KEYS = _parse_api_keys()
    _mod.LLM_BASE_URL = _resolve_base_url()
    _mod.LLM_MODEL = _cfg(
        'LLM_MODEL', 'opus', 'gpt-4o', empty_env_is_unset=True)
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

    # Fetch settings — same priority chain as module init: ENV > saved > default
    _search_raw = _SAVED_CONFIG.get('search', {})
    _search = _resolve_search_profile(
        _search_raw if isinstance(_search_raw, dict) else {})
    _mod.SEARCH_PROFILE = _search['profile']
    _mod.SEARCH_OVERRIDES = dict(_search.get('overrides') or {})
    _mod.SEARCH_DEEPEN_ENABLED = bool(_search.get('deepen_enabled', False))
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
    # Re-resolve the content-filter toggle (env > saved > default ON).
    _mod.LLM_CONTENT_FILTER_ENABLED = (
        os.environ.get('FETCH_LLM_FILTER', '1') == '1'
        if os.environ.get('FETCH_LLM_FILTER') is not None
        else bool(_search.get('llm_content_filter', True))
    )
    # Push refreshed settings only when tofu-search is already resident. A
    # model/provider-only update must not cold-import the optional search graph;
    # first later activation reads these newest module values.
    try:
        from lib.search_runtime import sync_search_config_if_loaded
        sync_search_config_if_loaded()
    except Exception as _be:
        import logging as _logging
        _logging.getLogger(__name__).debug('tofu-search config re-sync skipped: %s', _be)

    # Feature flags
    _mod.PPTX_TRANSLATE_ENABLED = _resolve_feature_flag('PPTX_TRANSLATE_ENABLED', 'pptx_translate_enabled', False)
    _mod.DEBUG_MODE = _resolve_feature_flag('DEBUG_MODE', 'debug_mode', False)
    _mod.CACHE_EXTENDED_TTL = _resolve_feature_flag('CACHE_EXTENDED_TTL', 'cache_extended_ttl', True)
    _mod.OPTIMIZER_ENABLED = _resolve_feature_flag('OPTIMIZER_ENABLED', 'optimizer_enabled', True)
    _mod.SCHEDULER_ALLOW_CODE_EXEC = _resolve_feature_flag(
        'SCHEDULER_ALLOW_CODE_EXEC', 'scheduler_allow_code_exec', True)
    _mod.ARTIFACTS_ENABLED = _resolve_feature_flag('ARTIFACTS_ENABLED', 'artifacts_enabled', True)
    # Plugin flags (tofu.flags): re-resolve each registered flag in place.
    try:
        from lib.feature_registry import registered_flags as _rf
        for _flag in _rf():
            setattr(_mod, _flag.env_key,
                    _resolve_feature_flag(_flag.env_key, _flag.json_key, _flag.default))
    except Exception as _fe:
        import logging as _logging
        _logging.getLogger(__name__).debug('Plugin flag reload skipped: %s', _fe)

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
