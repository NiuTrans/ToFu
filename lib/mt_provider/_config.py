"""lib/mt_provider/_config.py — MT provider config + language code mapping.

Stateless: config is read per-call from ``lib.MT_PROVIDER_CONFIG`` so the
Settings UI hot-reload takes effect immediately. The language map + request
limits are module-level constants.
"""

from lib.log import get_logger

logger = get_logger(__name__)

# ── Language code mapping ──
# Maps user-facing language names (as used in our translate prompts) to
# NiuTrans language codes.  NiuTrans uses ISO 639-1 style codes.
_LANG_MAP = {
    # Chinese variants
    '中文': 'zh', 'chinese': 'zh', 'simplified chinese': 'zh',
    '简体中文': 'zh', '繁体中文': 'cht', 'traditional chinese': 'cht',
    # English
    'english': 'en', '英文': 'en', '英语': 'en',
    # Japanese
    '日文': 'ja', '日语': 'ja', 'japanese': 'ja',
    # Korean
    '韩文': 'ko', '韩语': 'ko', 'korean': 'ko',
    # Common European languages
    'french': 'fr', '法语': 'fr', '法文': 'fr',
    'german': 'de', '德语': 'de', '德文': 'de',
    'spanish': 'es', '西班牙语': 'es',
    'russian': 'ru', '俄语': 'ru', '俄文': 'ru',
    'portuguese': 'pt', '葡萄牙语': 'pt',
    'italian': 'it', '意大利语': 'it',
    'arabic': 'ar', '阿拉伯语': 'ar',
    'thai': 'th', '泰语': 'th',
    'vietnamese': 'vi', '越南语': 'vi',
    # Auto-detect
    'auto': 'auto', '': 'auto',
}

# NiuTrans character limit per request
_NIUTRANS_MAX_CHARS = 5000

# Request timeout
_REQUEST_TIMEOUT = 30


def _normalize_lang(lang_name):
    """Convert a user-facing language name to a NiuTrans language code.

    Args:
        lang_name: Language name like 'English', '中文', 'zh', etc.

    Returns:
        NiuTrans language code (e.g. 'en', 'zh', 'ja').
    """
    if not lang_name:
        return 'auto'
    key = lang_name.strip().lower()
    # Direct code pass-through (already a short code)
    return _LANG_MAP.get(key, key)


def _get_mt_config():
    """Read MT provider config from lib module (hot-reloadable).

    Uses the ``import lib as _lib; _lib.MT_PROVIDER_CONFIG`` pattern
    so that hot-reload via Settings UI takes effect immediately.

    Returns:
        dict with keys: provider, api_url, api_key, app_id, enabled
        or empty dict if not configured.
    """
    import lib as _lib
    cfg = getattr(_lib, 'MT_PROVIDER_CONFIG', None)
    if not cfg or not isinstance(cfg, dict):
        return {}
    if not cfg.get('enabled', False):
        return {}
    return cfg


def is_mt_configured():
    """Check if a machine translation provider is configured and enabled.

    Returns:
        True if an MT provider is ready to use.
    """
    cfg = _get_mt_config()
    if not cfg:
        return False
    provider = cfg.get('provider', '')
    api_key = cfg.get('api_key', '')
    if not provider or not api_key:
        return False
    return True
