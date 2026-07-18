"""lib/mt_provider/_niutrans.py — NiuTrans translation API adapters.

Supports both the v1 (apikey-only) and v2 (appId + MD5 authStr signature)
NiuTrans HTTP APIs. ``_niutrans_translate`` auto-selects based on whether an
``app_id`` is configured.
"""

import hashlib
import time

import requests

from lib.log import get_logger
from lib.http_client import http_post

from ._config import _normalize_lang, _REQUEST_TIMEOUT

logger = get_logger(__name__)


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

    url = api_url or 'https://api.niutrans.com/NiuTransServer/translation'

    payload = {
        'src_text': text,
        'from': src_lang,
        'to': tgt_lang,
        'apikey': api_key,
    }

    try:
        resp = http_post(url, data=payload, timeout=_REQUEST_TIMEOUT)
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
        resp = http_post(url, json=payload, timeout=_REQUEST_TIMEOUT)
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
