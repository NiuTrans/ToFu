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
    'FETCH_TOP_N', 'FETCH_TIMEOUT',
    'FETCH_MAX_CHARS_SEARCH', 'FETCH_MAX_CHARS_DIRECT',
    'FETCH_MAX_CHARS_PDF', 'FETCH_MAX_BYTES',
    'SKIP_DOMAINS', 'MODEL_PRICING',
    'QWEN_PRICING_CNY', 'DEFAULT_USD_CNY_RATE',
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


# ── Trading module (default OFF — enable in Settings or with TRADING_ENABLED=1) ──
# Priority: env-var > features.json > default(off)
def _resolve_trading_enabled():
    """Resolve TRADING_ENABLED from env-var or persistent features.json.

    Priority: env-var > data/config/features.json > default(OFF).
    Each project copy has its own feature flags.
    """
    env_val = os.environ.get('TRADING_ENABLED')
    if env_val is not None:
        return env_val == '1'
    # Check persistent features.json in per-project config dir
    _features_path = _config_path('features.json')
    try:
        if os.path.isfile(_features_path):
            import json as _json
            with open(_features_path) as f:
                feats = _json.load(f)
            if isinstance(feats, dict) and 'trading_enabled' in feats:
                return bool(feats['trading_enabled'])
    except Exception as _e:
        import logging as _logging
        _logging.getLogger(__name__).debug('Could not read features.json: %s', _e)
    return False  # default OFF

TRADING_ENABLED = _resolve_trading_enabled()
FETCH_TOP_N            = int(os.environ.get('FETCH_TOP_N', '6'))
FETCH_TIMEOUT          = int(os.environ.get('FETCH_TIMEOUT', '15'))
FETCH_MAX_CHARS_SEARCH = int(os.environ.get('FETCH_MAX_CHARS_SEARCH', '60000'))
FETCH_MAX_CHARS_DIRECT = int(os.environ.get('FETCH_MAX_CHARS_DIRECT', '200000'))
FETCH_MAX_CHARS_PDF    = int(os.environ.get('FETCH_MAX_CHARS_PDF', '0'))
FETCH_MAX_BYTES        = int(os.environ.get('FETCH_MAX_BYTES', str(20*1024*1024)))

SKIP_DOMAINS = {
    'youtube.com','youtu.be','twitter.com','x.com',
    'facebook.com','instagram.com','tiktok.com',
    'linkedin.com','discord.com',
}

# ── Model pricing (USD per 1M tokens) — hardcoded fallback ──
# cacheWriteMul / cacheReadMul are multipliers of the base input price:
#   Anthropic Claude: write=1.25x, read=0.10x (5-min TTL)
#   OpenAI GPT:       write=1.00x, read=0.50x
#   DeepSeek:         write=1.00x, read=0.10x (disk cache)
MODEL_PRICING = {
    'aws.claude-opus-4.6':       {'input': 15.0,  'output': 75.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 4'},
    'aws.claude-opus-4.6-b':    {'input': 15.0,  'output': 75.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 4 (B)'},
    'vertex.claude-opus-4.6':   {'input': 15.0,  'output': 75.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 4 (Vertex)'},
    'claude-opus-4-6-20250514':  {'input': 15.0,  'output': 75.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 4'},
    'aws.claude-sonnet-4.6':     {'input': 3.0,   'output': 15.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Sonnet 4'},
    'claude-sonnet-4-6-20250514':{'input': 3.0,   'output': 15.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Sonnet 4'},
    'claude-3-5-sonnet-20241022':{'input': 3.0,   'output': 15.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude 3.5 Sonnet'},
    'claude-3-opus-20240229':    {'input': 15.0,  'output': 75.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude 3 Opus'},
    'claude-3-5-haiku-20241022': {'input': 0.8,   'output': 4.0,   'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude 3.5 Haiku'},
    'gpt-4o':                    {'input': 2.5,   'output': 10.0,  'cacheWriteMul': 1.00, 'cacheReadMul': 0.50, 'name': 'GPT-4o'},

    'gpt-4o-mini':               {'input': 0.15,  'output': 0.6,   'cacheWriteMul': 1.00, 'cacheReadMul': 0.50, 'name': 'GPT-4o Mini'},
    'gpt-4-turbo':               {'input': 10.0,  'output': 30.0,  'cacheWriteMul': 1.00, 'cacheReadMul': 0.50, 'name': 'GPT-4 Turbo'},
    'deepseek-chat':             {'input': 0.27,  'output': 1.10,  'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'DeepSeek V3'},
    'deepseek-v3.2':             {'input': 0.28,  'output': 0.41,  'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'DeepSeek V3.2'},  # ¥2/¥3 per 1M
    'deepseek-reasoner':         {'input': 0.55,  'output': 2.21,  'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'DeepSeek R1'},
    'LongCat-Flash-Thinking-2601': {'input': 0.0, 'output': 0.0,  'cacheWriteMul': 0,    'cacheReadMul': 0,    'name': 'LongCat Flash'},
    'LongCat-Flash-Chat-2603':      {'input': 0.28,'output': 1.10, 'cacheWriteMul': 0,    'cacheReadMul': 0,    'name': 'LongCat Flash Chat'},  # ¥2/¥8 per 1M
    'LongCat-MoE3B-Chat-YourProvider':  {'input': 0.0, 'output': 0.0,  'cacheWriteMul': 0,    'cacheReadMul': 0,    'name': 'LongCat MoE 3B'},
    # ── Qwen (DashScope) — converted from CNY at 7.24 ──
    'qwen3.6-plus':              {'input': 0.28, 'output': 1.66, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Qwen 3.6 Plus'},  # ¥2/¥12 per 1M (≤256K)
    'qwen3.5-plus':              {'input': 0.11, 'output': 0.66, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Qwen 3.5 Plus'},  # ¥0.8/¥4.8 per 1M (≤128K)
    'qwen3.5-flash':             {'input': 0.03, 'output': 0.28, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Qwen 3.5 Flash'},  # ¥0.2/¥2 per 1M (≤128K)
    'qwen3-max':                 {'input': 0.35, 'output': 1.38, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Qwen3 Max'},  # ¥2.5/¥10 per 1M (≤32K)
    'qwen3-vl-plus':             {'input': 0.14, 'output': 1.38, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Qwen3 VL Plus'},  # ¥1/¥10 per 1M (≤32K)
    'qwen3-vl-flash':            {'input': 0.02, 'output': 0.21, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Qwen3 VL Flash'},  # ¥0.15/¥1.5 per 1M (≤32K)
    'qwen3-coder-plus':          {'input': 0.55, 'output': 2.21, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Qwen3 Coder Plus'},  # ¥4/¥16 per 1M (≤32K)
    'qwen3-coder-flash':         {'input': 0.14, 'output': 0.55, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Qwen3 Coder Flash'},  # ¥1/¥4 per 1M (≤32K)
    'qwen-plus':                 {'input': 0.11, 'output': 0.28, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Qwen Plus'},  # ¥0.8/¥2 non-think, ¥8 think per 1M (≤128K)
    'qwen-max':                  {'input': 0.33, 'output': 1.33, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Qwen Max'},  # ¥2.4/¥9.6 per 1M
    'qwen-flash':                {'input': 0.02, 'output': 0.21, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Qwen Flash'},  # ¥0.15/¥1.5 per 1M (≤128K)
    'qwq-plus':                  {'input': 0.22, 'output': 0.55, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'QwQ Plus'},  # ¥1.6/¥4 per 1M
    'qvq-max':                   {'input': 1.10, 'output': 4.42, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'QVQ Max'},  # ¥8/¥32 per 1M
    'qvq-plus':                  {'input': 0.28, 'output': 0.69, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'QVQ Plus'},  # ¥2/¥5 per 1M
    'qwen-vl-max':               {'input': 0.22, 'output': 0.55, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Qwen VL Max'},  # ¥1.6/¥4 per 1M
    'qwen-vl-plus':              {'input': 0.11, 'output': 0.28, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Qwen VL Plus'},  # ¥0.8/¥2 per 1M
    'qwen-turbo':                {'input': 0.04, 'output': 0.08, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Qwen Turbo'},  # ¥0.3/¥0.6 non-think, ¥3 think per 1M
    'qwen-long':                 {'input': 0.07, 'output': 0.28, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Qwen Long'},  # ¥0.5/¥2 per 1M
    # ── Gemini ──
    'gemini-2.5-pro':            {'input': 1.25, 'output': 10.0, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 2.5 Pro'},
    'gemini-2.5-flash':          {'input': 0.15, 'output': 0.60, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 2.5 Flash'},
    'gemini-2.0-flash-lite':     {'input': 0.075,'output': 0.30, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 2.0 Flash-Lite'},
    'gemini-3.1-flash-lite-preview': {'input': 0.25, 'output': 1.50, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 3.1 Flash-Lite'},
    'gemini-3.1-pro-preview':    {'input': 2.00, 'output': 12.0, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 3.1 Pro'},
    'gemini-3-flash-preview':    {'input': 0.15, 'output': 0.60, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 3 Flash'},
    'gemini-3.1-flash-image-preview': {'input': 0.25, 'output': 1.50, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 3.1 Flash Image'},
    'gemini-3-pro-image-preview':    {'input': 2.50, 'output': 12.0, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 3 Pro Image'},
    'gemini-2.5-flash-image':        {'input': 0.15, 'output': 0.60, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 2.5 Flash Image'},
    'gemini-2.0-flash-preview-image-generation': {'input': 0.10, 'output': 0.40, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 2.0 Flash Image'},
    'gpt-image-1.5':                 {'input': 0.0,  'output': 0.0,  'cacheWriteMul': 0, 'cacheReadMul': 0, 'name': 'GPT Image 1.5'},
    'gpt-image-1':                   {'input': 0.0,  'output': 0.0,  'cacheWriteMul': 0, 'cacheReadMul': 0, 'name': 'GPT Image 1'},
    'gpt-image-1-mini':              {'input': 0.0,  'output': 0.0,  'cacheWriteMul': 0, 'cacheReadMul': 0, 'name': 'GPT Image 1 Mini'},
    'dall-e-3':                      {'input': 0.0,  'output': 0.0,  'cacheWriteMul': 0, 'cacheReadMul': 0, 'name': 'DALL-E 3'},
    # ── OpenAI (GPT-5.4 family — March 2026) ──
    'gpt-5.4':                   {'input': 2.50, 'output': 15.00, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GPT-5.4'},
    'gpt-5.4-pro':               {'input': 30.0, 'output': 180.0, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GPT-5.4 Pro'},
    'gpt-5.4-mini':              {'input': 0.75, 'output': 4.50, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GPT-5.4 Mini'},
    'gpt-5.4-nano':              {'input': 0.20, 'output': 1.25, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GPT-5.4 Nano'},
    # ── OpenAI (GPT-5 family) ──
    'gpt-5':                     {'input': 1.25, 'output': 10.00, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GPT-5'},
    'gpt-5.2':                   {'input': 1.75, 'output': 14.00, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GPT-5.2'},
    'gpt-5-mini':                {'input': 0.25, 'output': 2.00, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GPT-5 Mini'},
    'gpt-5-nano':                {'input': 0.05, 'output': 0.40, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GPT-5 Nano'},
    # ── OpenAI (o-series reasoning) ──
    'o3':                        {'input': 2.00, 'output': 8.00, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.50, 'name': 'o3'},
    'o4-mini':                   {'input': 1.10, 'output': 4.40, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.50, 'name': 'o4-mini'},
    'o3-mini':                   {'input': 1.10, 'output': 4.40, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.50, 'name': 'o3-mini'},
    # ── OpenAI (GPT-4 family — previous gen) ──
    'gpt-4.1':                   {'input': 2.00, 'output': 8.00, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.50, 'name': 'GPT-4.1'},
    'gpt-4.1-mini':              {'input': 0.40, 'output': 1.60, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'GPT-4.1 Mini'},
    'gpt-4.1-nano':              {'input': 0.10, 'output': 0.40, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'GPT-4.1 Nano'},
    # ── Anthropic (Claude 4.6 family — Feb 2026) ──
    'claude-opus-4-6':           {'input': 5.0,   'output': 25.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 4.6'},
    'claude-sonnet-4-6':         {'input': 3.0,   'output': 15.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Sonnet 4.6'},
    'claude-haiku-4-5':          {'input': 1.0,   'output': 5.0,   'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Haiku 4.5'},
    'claude-haiku-4-5-20251001': {'input': 1.0,   'output': 5.0,   'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Haiku 4.5'},
    # ── Anthropic (Claude 4.5 family) ──
    'claude-opus-4-5':           {'input': 5.0,   'output': 25.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 4.5'},
    'claude-sonnet-4-5':         {'input': 3.0,   'output': 15.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Sonnet 4.5'},
    # ── Anthropic (Claude 4 — legacy) ──
    'claude-opus-4-20250514':    {'input': 15.0,  'output': 75.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 4'},
    'claude-sonnet-4-20250514':  {'input': 3.0,   'output': 15.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Sonnet 4'},
    # ── MiniMax ──
    'MiniMax-M2':                {'input': 0.30, 'output': 1.20, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'MiniMax M2'},
    'MiniMax-M2.1':              {'input': 0.30, 'output': 1.20, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'MiniMax M2.1'},
    'MiniMax-M2.1-highspeed':    {'input': 0.30, 'output': 2.40, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'MiniMax M2.1 HS'},
    'MiniMax-M2.5':              {'input': 0.30, 'output': 1.20, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'MiniMax M2.5'},
    'MiniMax-M2.5-highspeed':    {'input': 0.30, 'output': 1.20, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'MiniMax M2.5 HS'},
    'MiniMax-M2.7':              {'input': 0.30, 'output': 1.20, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'MiniMax M2.7'},
    'MiniMax-M2.7-highspeed':    {'input': 0.30, 'output': 1.20, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'MiniMax M2.7 HS'},
    'M2-her':                    {'input': 0.30, 'output': 1.20, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'MiniMax M2-her'},
    # ── GLM (Zhipu AI) — converted from CNY at 7.24 ──
    'glm-5.1':                   {'input': 3.45, 'output': 13.81, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GLM-5.1'},
    'glm-5':                     {'input': 3.45, 'output': 13.81, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GLM-5'},
    'glm-4.7':                   {'input': 0.69, 'output': 0.69, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GLM-4.7'},
    'glm-4.5-air':               {'input': 0.28, 'output': 1.10, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GLM-4.5 Air'},
    'glm-4.5-flash':             {'input': 0.0,  'output': 0.0,  'cacheWriteMul': 0,    'cacheReadMul': 0,    'name': 'GLM-4.5 Flash'},
    # ── Doubao (Volcengine) — converted from CNY at 7.24 ──
    'Doubao-Seed-2.0-pro':       {'input': 0.55, 'output': 2.21, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Doubao Seed 2.0 Pro'},
    'Doubao-Seed-2.0-lite':      {'input': 0.04, 'output': 0.14, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Doubao Seed 2.0 Lite'},
    'Doubao-Seed-2.0-mini':      {'input': 0.02, 'output': 0.06, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Doubao Seed 2.0 Mini'},
    # ── Mistral AI ──
    'mistral-large-latest':      {'input': 2.00, 'output': 6.00, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Mistral Large'},
    'mistral-small-latest':      {'input': 0.10, 'output': 0.30, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Mistral Small'},
    'codestral-latest':          {'input': 0.30, 'output': 0.90, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Codestral'},
    # ── xAI (Grok) ──
    'grok-3':                    {'input': 3.00, 'output': 15.0, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Grok 3'},
    'grok-3-mini':               {'input': 0.30, 'output': 0.50, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Grok 3 Mini'},
}

# ── Qwen tiered pricing (CNY per 1M tokens) ──
# The MODEL_PRICING above uses the cheapest tier converted to USD.
# For precise CNY cost, use these per-model tiers directly:
QWEN_PRICING_CNY = {
    # qwen3.6-plus: ¥2/¥12 (≤256K), ¥8/¥48 (256K-1M)
    'qwen3.6-plus': {
        'input':  [(256_000, 2.0), (1_000_000, 8.0)],
        'output': [(256_000, 12.0), (1_000_000, 48.0)],
    },
    # qwen3.5-plus: ¥0.8/¥4.8 (≤128K), ¥2/¥12 (128K-256K), ¥4/¥24 (256K-1M)
    'qwen3.5-plus': {
        'input':  [(128_000, 0.8), (256_000, 2.0), (1_000_000, 4.0)],
        'output': [(128_000, 4.8), (256_000, 12.0), (1_000_000, 24.0)],
    },
    # qwen3.5-flash: ¥0.2/¥2 (≤128K), ¥0.8/¥8 (128K-256K), ¥1.2/¥12 (256K-1M)
    'qwen3.5-flash': {
        'input':  [(128_000, 0.2), (256_000, 0.8), (1_000_000, 1.2)],
        'output': [(128_000, 2.0), (256_000, 8.0), (1_000_000, 12.0)],
    },
    # qwen3-max: ¥2.5/¥10 (≤32K), ¥4/¥16 (32K-128K), ¥7/¥28 (128K-252K)
    'qwen3-max': {
        'input':  [(32_000, 2.5), (128_000, 4.0), (252_000, 7.0)],
        'output': [(32_000, 10.0), (128_000, 16.0), (252_000, 28.0)],
    },
    # qwen-plus: ¥0.8/¥2 non-think (≤128K), ¥2.4/¥20 (128K-256K), ¥4.8/¥48 (256K-1M)
    'qwen-plus': {
        'input':  [(128_000, 0.8), (256_000, 2.4), (1_000_000, 4.8)],
        'output': [(128_000, 2.0), (256_000, 20.0), (1_000_000, 48.0)],
    },
    # qwen-flash: ¥0.15/¥1.5 (≤128K), ¥0.6/¥6 (128K-256K), ¥1.2/¥12 (256K-1M)
    'qwen-flash': {
        'input':  [(128_000, 0.15), (256_000, 0.6), (1_000_000, 1.2)],
        'output': [(128_000, 1.5), (256_000, 6.0), (1_000_000, 12.0)],
    },
    # qwen3-vl-plus: ¥1/¥10 (≤32K), ¥1.5/¥15 (32K-128K), ¥3/¥30 (128K-256K)
    'qwen3-vl-plus': {
        'input':  [(32_000, 1.0), (128_000, 1.5), (256_000, 3.0)],
        'output': [(32_000, 10.0), (128_000, 15.0), (256_000, 30.0)],
    },
    # qwen3-vl-flash: ¥0.15/¥1.5 (≤32K), ¥0.3/¥3 (32K-128K), ¥0.6/¥6 (128K-256K)
    'qwen3-vl-flash': {
        'input':  [(32_000, 0.15), (128_000, 0.3), (256_000, 0.6)],
        'output': [(32_000, 1.5), (128_000, 3.0), (256_000, 6.0)],
    },
    # qwen3-coder-plus: ¥4/¥16 (≤32K), ¥6/¥24 (32K-128K), ¥10/¥40 (128K-256K), ¥20/¥200 (256K-1M)
    'qwen3-coder-plus': {
        'input':  [(32_000, 4.0), (128_000, 6.0), (256_000, 10.0), (1_000_000, 20.0)],
        'output': [(32_000, 16.0), (128_000, 24.0), (256_000, 40.0), (1_000_000, 200.0)],
    },
    # qwen3-coder-flash: ¥1/¥4 (≤32K), ¥1.5/¥6 (32K-128K), ¥2.5/¥10 (128K-256K), ¥5/¥25 (256K-1M)
    'qwen3-coder-flash': {
        'input':  [(32_000, 1.0), (128_000, 1.5), (256_000, 2.5), (1_000_000, 5.0)],
        'output': [(32_000, 4.0), (128_000, 6.0), (256_000, 10.0), (1_000_000, 25.0)],
    },
    # qwq-plus: flat ¥1.6/¥4
    'qwq-plus': {
        'input':  [(1_000_000, 1.6)],
        'output': [(1_000_000, 4.0)],
    },
    # qvq-max: flat ¥8/¥32
    'qvq-max': {
        'input':  [(1_000_000, 8.0)],
        'output': [(1_000_000, 32.0)],
    },
    # qvq-plus: flat ¥2/¥5
    'qvq-plus': {
        'input':  [(1_000_000, 2.0)],
        'output': [(1_000_000, 5.0)],
    },
    # qwen-max: flat ¥2.4/¥9.6
    'qwen-max': {
        'input':  [(1_000_000, 2.4)],
        'output': [(1_000_000, 9.6)],
    },
    # qwen-turbo: flat ¥0.3/¥0.6 non-think, ¥3 think
    'qwen-turbo': {
        'input':  [(1_000_000, 0.3)],
        'output': [(1_000_000, 0.6)],
    },
    # qwen-long: flat ¥0.5/¥2
    'qwen-long': {
        'input':  [(1_000_000, 0.5)],
        'output': [(1_000_000, 2.0)],
    },
    # qwen-vl-max: flat ¥1.6/¥4
    'qwen-vl-max': {
        'input':  [(1_000_000, 1.6)],
        'output': [(1_000_000, 4.0)],
    },
    # qwen-vl-plus: flat ¥0.8/¥2
    'qwen-vl-plus': {
        'input':  [(1_000_000, 0.8)],
        'output': [(1_000_000, 2.0)],
    },
    # deepseek-v3.2 (on DashScope): flat ¥2/¥3
    'deepseek-v3.2': {
        'input':  [(1_000_000, 2.0)],
        'output': [(1_000_000, 3.0)],
    },
    # deepseek-r1 (on DashScope): flat ¥4/¥16
    'deepseek-r1': {
        'input':  [(1_000_000, 4.0)],
        'output': [(1_000_000, 16.0)],
    },
    # Default (fallback for unlisted models)
    '_default': {
        'input':  [(128_000, 0.8), (256_000, 2.0), (1_000_000, 4.0)],
        'output': [(128_000, 4.8), (256_000, 12.0), (1_000_000, 24.0)],
    },
}

DEFAULT_USD_CNY_RATE = 7.24


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

    # Fetch settings (may have been overridden by search section in server_config)
    _search = _SAVED_CONFIG.get('search', {})
    _mod.FETCH_TOP_N = int(_search.get('fetch_top_n', os.environ.get('FETCH_TOP_N', '6')))
    _mod.FETCH_TIMEOUT = int(_search.get('fetch_timeout', os.environ.get('FETCH_TIMEOUT', '15')))
    _mod.FETCH_MAX_CHARS_SEARCH = int(_search.get('max_chars_search', os.environ.get('FETCH_MAX_CHARS_SEARCH', '60000')))
    _mod.FETCH_MAX_CHARS_DIRECT = int(_search.get('max_chars_direct', os.environ.get('FETCH_MAX_CHARS_DIRECT', '200000')))
    _mod.FETCH_MAX_CHARS_PDF = int(_search.get('max_chars_pdf', os.environ.get('FETCH_MAX_CHARS_PDF', '0')))
    _mod.FETCH_MAX_BYTES = int(_search.get('max_bytes', os.environ.get('FETCH_MAX_BYTES', str(20*1024*1024))))
    if 'skip_domains' in _search and isinstance(_search['skip_domains'], list):
        _mod.SKIP_DOMAINS = set(_search['skip_domains'])

    # Trading flag
    _mod.TRADING_ENABLED = _resolve_trading_enabled()

    # Model defaults (from model_defaults section)
    _md = _SAVED_CONFIG.get('model_defaults', {})
    if _md.get('fallback_model') is not None:
        _mod.FALLBACK_MODEL = _md['fallback_model'] or ''
    if _md.get('default_model'):
        _mod.LLM_MODEL = _md['default_model']

    import logging as _logging
    _logging.getLogger(__name__).info(
        '[Config] Hot-reloaded: model=%s, base_url=%.60s, keys=%d, fetch_top_n=%d',
        _mod.LLM_MODEL, _mod.LLM_BASE_URL, len(_mod.LLM_API_KEYS), _mod.FETCH_TOP_N,
    )
