"""Tier 3 Gemini ``countTokens`` backend.

Calls Google's native ``v1beta/models/{model}:countTokens`` endpoint
(or the Vertex AI equivalent on ``v1``). Free, exact, same
tokenization the serving model uses.

Activated ONLY when ``base_url`` points to:
  - ``generativelanguage.googleapis.com`` (Google AI Studio)
  - ``{region}-aiplatform.googleapis.com`` (Vertex AI)
  - a gateway that proxies ``/google/{v1,v1beta}`` paths

The Meituan gateway DOES NOT proxy this endpoint (verified 2026-05 —
all ``/v1/gemini/...`` paths return 404). On those setups this
backend silently declines, letting the dispatcher fall through to
tiktoken.
"""

from __future__ import annotations

from typing import Any, Optional

from lib.log import get_logger
from lib.http_client import http_post

from .base import TokenCounter
from .config import API_TIMEOUT

logger = get_logger(__name__)


def _resolve_gemini_count_url(base_url: str, model: str) -> Optional[str]:
    if not base_url or not model:
        return None
    b = base_url.rstrip('/')

    if 'generativelanguage.googleapis.com' in b:
        root = b.split('/v1beta', 1)[0].rstrip('/')
        return f'{root}/v1beta/models/{model}:countTokens'

    if 'aiplatform.googleapis.com' in b:
        root = b.split('/v1', 1)[0].rstrip('/')
        return f'{root}/v1/models/{model}:countTokens'

    if '/google/v1beta' in b or '/google/v1' in b:
        return f'{b}/models/{model}:countTokens'

    return None


def _build_gemini_count_body(messages, *, system=None) -> dict:
    contents = []
    for msg in messages or ():
        role = msg.get('role')
        if role not in ('user', 'assistant'):
            continue
        gem_role = 'user' if role == 'user' else 'model'
        text = ''
        c = msg.get('content')
        if isinstance(c, str):
            text = c
        elif isinstance(c, list):
            parts = []
            for blk in c:
                if isinstance(blk, dict) and blk.get('type') in (
                        'text', 'input_text', 'output_text'):
                    parts.append(blk.get('text') or '')
            text = '\n\n'.join(parts)
        if not text:
            continue
        contents.append({'role': gem_role, 'parts': [{'text': text}]})

    body: dict = {'contents': contents}
    if system:
        sys_txt = system if isinstance(system, str) else \
            '\n\n'.join(b.get('text', '') for b in system if isinstance(b, dict))
        body['systemInstruction'] = {'parts': [{'text': sys_txt}]}
    return body


class GeminiAPICounter(TokenCounter):
    """Exact upstream count for Gemini models (native endpoint only)."""

    name = 'gemini_api'
    confidence = 'exact'
    needs_network = True

    def supports(self, model: str) -> bool:
        m = (model or '').lower()
        return 'gemini' in m

    def count(self, messages: list, *, model: str,
              system: Any = None, tools: Any = None,
              api_base_url: Optional[str] = None,
              api_key: Optional[str] = None,
              **kwargs) -> Optional[int]:
        if not api_base_url or not api_key:
            return None
        url = _resolve_gemini_count_url(api_base_url, model)
        if not url:
            return None

        try:
            import requests  # noqa: F401  — availability probe (raises ImportError if absent)
            from lib.proxy import proxies_for as _proxies_for  # noqa: F401
        except ImportError as e:
            logger.warning('[TokenCounter] requests / lib.proxy import failed: %s', e)
            return None

        body = _build_gemini_count_body(messages, system=system)
        # Google uses ?key= for auth on Google AI; Bearer on Vertex.
        # Send both — server picks what it supports.
        sep = '&' if '?' in url else '?'
        url_with_key = f'{url}{sep}key={api_key}'
        headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        }
        try:
            r = http_post(url_with_key, json=body, headers=headers,
                              timeout=(10, API_TIMEOUT),
                              verify=True)
            if r.status_code != 200:
                logger.warning('[TokenCounter] Gemini countTokens HTTP %d: %.200s',
                               r.status_code, r.text)
                return None
            data = r.json()
            n = data.get('totalTokens') or data.get('total_tokens')
            if isinstance(n, int) and n >= 0:
                return n
            logger.warning('[TokenCounter] Gemini response missing totalTokens: %r', data)
            return None
        except Exception as e:
            logger.warning('[TokenCounter] Gemini countTokens exception: %s', e)
            return None


__all__ = ['GeminiAPICounter']
