"""lib/mt_provider.py — Machine Translation provider adapters.

Provides a unified interface for dedicated machine translation APIs
(NiuTrans, etc.) that are faster and cheaper than using LLMs for translation.

When a provider is configured in Settings → 通用 → 机器翻译, translation
routes use the MT API directly instead of the LLM cheap model.
No LLM prompt is needed — the MT API handles translation directly.

If nothing is configured, the LLM-based translation path is used as before.

Usage:
    from lib.mt_provider import mt_translate_chunked, is_mt_configured

    if is_mt_configured():
        translated = mt_translate_chunked(text, source='en', target='zh')
"""

import hashlib
import re
import time

import requests

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


# ── Code block extraction for MT ──
# MT APIs don't understand markdown — code blocks would get corrupted.
# We extract them before translation and reinsert after.
_CODE_BLOCK_RE = re.compile(r'(```[\w]*\n[\s\S]*?```)', re.MULTILINE)
_INLINE_CODE_RE = re.compile(r'(`[^`\n]+`)')
def _extract_code_blocks(text):
    """Extract fenced code blocks and inline code, replacing with placeholders.

    Uses ``[CBLOCK_N]`` format which NiuTrans preserves verbatim.

    Returns:
        (cleaned_text, blocks_dict) where blocks_dict maps placeholder → original.
    """
    blocks = {}
    counter = [0]

    def _replace_block(m):
        key = '[CBLOCK_%d]' % counter[0]
        blocks[key] = m.group(0)
        counter[0] += 1
        return key

    # Fenced code blocks first (greedy, before inline)
    text = _CODE_BLOCK_RE.sub(_replace_block, text)
    # Inline code
    text = _INLINE_CODE_RE.sub(_replace_block, text)
    return text, blocks


def _restore_code_blocks(text, blocks):
    """Reinsert code blocks from placeholders."""
    for key, original in blocks.items():
        # NiuTrans may add spaces around the placeholder
        text = text.replace(key, original)
    return text


def mt_translate(text, source='', target='zh'):
    """Translate text using the configured machine translation provider.

    Automatically extracts code blocks (fenced and inline) before
    translation and reinserts them after, since MT APIs don't understand
    markdown formatting.

    Args:
        text: Text to translate.
        source: Source language name/code (empty or 'auto' for auto-detect).
        target: Target language name/code.

    Returns:
        Translated text string.

    Raises:
        ValueError: If MT provider is not configured.
        RuntimeError: If the API call fails.
    """
    cfg = _get_mt_config()
    if not cfg:
        raise ValueError('Machine translation provider not configured')

    # Short-circuit: source == target → return as-is
    src_norm = _normalize_lang(source)
    tgt_norm = _normalize_lang(target)
    if src_norm and src_norm != 'auto' and src_norm == tgt_norm:
        return text

    # Extract code blocks to protect them from MT corruption
    clean_text, code_blocks = _extract_code_blocks(text)
    if code_blocks:
        logger.debug('[MT] Extracted %d code blocks before translation', len(code_blocks))

    provider = cfg.get('provider', 'niutrans')
    if provider == 'niutrans':
        translated = _niutrans_translate(clean_text, source, target, cfg)
    else:
        raise ValueError('Unknown MT provider: %s' % provider)

    # Restore code blocks
    if code_blocks:
        translated = _restore_code_blocks(translated, code_blocks)

    return translated


def _niutrans_translate(text, source, target, cfg):
    """Call NiuTrans translation API.

    Supports both v1 (apikey-only) and v2 (appId + authStr signature) APIs.
    Auto-detects based on whether app_id is configured.

    Args:
        text: Text to translate.
        source: Source language.
        target: Target language.
        cfg: MT provider config dict.

    Returns:
        Translated text.
    """
    api_key = cfg.get('api_key', '')
    app_id = cfg.get('app_id', '')
    api_url = cfg.get('api_url', '').strip()

    src_lang = _normalize_lang(source)
    tgt_lang = _normalize_lang(target)

    if app_id:
        return _niutrans_v2(text, src_lang, tgt_lang, api_key, app_id, api_url)
    else:
        return _niutrans_v1(text, src_lang, tgt_lang, api_key, api_url)


def _niutrans_v1(text, src_lang, tgt_lang, api_key, api_url=''):
    """NiuTrans v1 API — simple apikey authentication.

    Endpoint: POST https://api.niutrans.com/NiuTransServer/translation
    Params: src_text, from, to, apikey
    """
    from lib.proxy import proxies_for

    url = api_url or 'https://api.niutrans.com/NiuTransServer/translation'

    payload = {
        'src_text': text,
        'from': src_lang,
        'to': tgt_lang,
        'apikey': api_key,
    }

    try:
        resp = requests.post(url, data=payload, timeout=_REQUEST_TIMEOUT,
                             proxies=proxies_for(url))
        resp.raise_for_status()
        data = resp.json()
    except requests.Timeout:
        logger.warning('[MT:niutrans-v1] Timeout after %ds', _REQUEST_TIMEOUT)
        raise RuntimeError('NiuTrans API timeout')
    except requests.RequestException as e:
        logger.warning('[MT:niutrans-v1] Request failed: %s', e)
        raise RuntimeError('NiuTrans API request failed: %s' % e)
    except (ValueError, TypeError) as e:
        logger.warning('[MT:niutrans-v1] Invalid response: %s', e)
        raise RuntimeError('NiuTrans API invalid response')

    if 'error_code' in data or 'errorCode' in data:
        err_code = data.get('error_code') or data.get('errorCode', '')
        err_msg = data.get('error_msg') or data.get('errorMsg', '')
        logger.warning('[MT:niutrans-v1] API error: code=%s msg=%s', err_code, err_msg)
        raise RuntimeError('NiuTrans API error %s: %s' % (err_code, err_msg))

    result = data.get('tgt_text', '').strip()
    if not result:
        logger.warning('[MT:niutrans-v1] Empty translation result for %d-char input', len(text))
        raise RuntimeError('NiuTrans returned empty translation')

    return result


def _niutrans_v2(text, src_lang, tgt_lang, api_key, app_id, api_url=''):
    """NiuTrans v2 API — appId + authStr (MD5 signature) authentication.

    Endpoint: POST https://api.niutrans.com/v2/text/translate
    Params: srcText, from, to, appId, timestamp, authStr
    """
    from lib.proxy import proxies_for

    url = api_url or 'https://api.niutrans.com/v2/text/translate'
    timestamp = str(int(time.time() * 1000))

    # Build params for auth string generation
    # Rule: sort all params (including apikey) by ASCII key name, MD5 hash
    params = {
        'appId': app_id,
        'from': src_lang,
        'srcText': text,
        'timestamp': timestamp,
        'to': tgt_lang,
    }

    # Generate authStr: sort by key, include apikey, concatenate with &
    auth_params = dict(params)
    auth_params['apikey'] = api_key
    sorted_items = sorted(auth_params.items(), key=lambda x: x[0])
    param_str = '&'.join('%s=%s' % (k, v) for k, v in sorted_items if v)
    auth_str = hashlib.md5(param_str.encode('utf-8')).hexdigest()

    # Final request payload
    payload = dict(params)
    payload['authStr'] = auth_str

    try:
        resp = requests.post(url, json=payload, timeout=_REQUEST_TIMEOUT,
                             proxies=proxies_for(url))
        resp.raise_for_status()
        data = resp.json()
    except requests.Timeout:
        logger.warning('[MT:niutrans-v2] Timeout after %ds', _REQUEST_TIMEOUT)
        raise RuntimeError('NiuTrans v2 API timeout')
    except requests.RequestException as e:
        logger.warning('[MT:niutrans-v2] Request failed: %s', e)
        raise RuntimeError('NiuTrans v2 API request failed: %s' % e)
    except (ValueError, TypeError) as e:
        logger.warning('[MT:niutrans-v2] Invalid response: %s', e)
        raise RuntimeError('NiuTrans v2 API invalid response')

    if 'errorCode' in data:
        err_code = data.get('errorCode', '')
        err_msg = data.get('errorMsg', '')
        logger.warning('[MT:niutrans-v2] API error: code=%s msg=%s', err_code, err_msg)
        raise RuntimeError('NiuTrans v2 API error %s: %s' % (err_code, err_msg))

    result = data.get('tgtText', '').strip()
    if not result:
        logger.warning('[MT:niutrans-v2] Empty translation result for %d-char input', len(text))
        raise RuntimeError('NiuTrans v2 returned empty translation')

    return result


def mt_translate_chunked(text, source='', target='zh', max_chunk=4500):
    """Translate text with automatic chunking for long inputs.

    NiuTrans has a 5000 char limit per request. This function splits
    longer texts on paragraph boundaries and translates each chunk.

    Args:
        text: Text to translate.
        source: Source language.
        target: Target language.
        max_chunk: Max chars per chunk (default 4500, leaving margin).

    Returns:
        Translated text.
    """
    if len(text) <= max_chunk:
        return mt_translate(text, source, target)

    # Split on paragraph boundaries
    paragraphs = text.split('\n\n')
    chunks = []
    current = ''
    for para in paragraphs:
        if current and len(current) + len(para) + 2 > max_chunk:
            chunks.append(current.strip())
            current = para
        else:
            current = current + '\n\n' + para if current else para
    if current.strip():
        chunks.append(current.strip())

    # Further split any chunks that are still too long
    final_chunks = []
    for chunk in chunks:
        if len(chunk) <= max_chunk:
            final_chunks.append(chunk)
        else:
            # Split on single newlines
            lines = chunk.split('\n')
            cur = ''
            for line in lines:
                if cur and len(cur) + len(line) + 1 > max_chunk:
                    final_chunks.append(cur.strip())
                    cur = line
                else:
                    cur = cur + '\n' + line if cur else line
            if cur.strip():
                final_chunks.append(cur.strip())

    if not final_chunks:
        return mt_translate(text, source, target)

    logger.info('[MT] Chunked translation: %d chars → %d chunks', len(text), len(final_chunks))

    translated_parts = []
    for i, chunk in enumerate(final_chunks):
        try:
            part = mt_translate(chunk, source, target)
            translated_parts.append(part)
        except Exception as e:
            logger.error('[MT] Chunk %d/%d failed: %s', i + 1, len(final_chunks), e)
            raise

    return '\n\n'.join(translated_parts)
