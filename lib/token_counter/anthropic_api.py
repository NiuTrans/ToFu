"""Tier 3 Anthropic ``count_tokens`` backend.

Calls the upstream Anthropic API (or a gateway that proxies it) to
get the **exact** token count — the same number the model's billing
system uses.

Verified working via the YourProvider gateway (2026-05):
  base_url:  https://api.openai.com/v1
  rewritten: https://api.openai.com/v1/anthropic/v1/messages/count_tokens

Also works on the public Anthropic endpoint (api.anthropic.com/v1).

Returns None when:
  - base_url doesn't look like an Anthropic-compatible endpoint.
  - api_key is missing.
  - the HTTP call fails / returns non-200.
"""

from __future__ import annotations

from typing import Any, Optional

from lib.log import get_logger
from lib.http_client import http_post

from .base import TokenCounter
from .config import API_TIMEOUT

logger = get_logger(__name__)


def _resolve_anthropic_count_url(base_url: str) -> Optional[str]:
    """Derive the Anthropic count_tokens URL from a provider ``base_url``."""
    if not base_url:
        return None
    b = base_url.rstrip('/')

    # YourProvider-style gateway: /openai/native → /anthropic/v1
    if '/openai/native' in b:
        root = b.split('/openai/native', 1)[0]
        return f'{root}/anthropic/v1/messages/count_tokens'

    # Native anthropic endpoint
    if 'anthropic.com' in b:
        return f'{b}/messages/count_tokens' if b.endswith('/v1') else \
               f'{b}/v1/messages/count_tokens'

    # Anthropic-compat prefix already in URL
    if '/anthropic' in b:
        return f'{b}/messages/count_tokens' if b.endswith('/v1') else \
               f'{b}/v1/messages/count_tokens'

    return None


def _build_anthropic_count_body(messages, *, model, system=None, tools=None) -> dict:
    """Shape OpenAI-style messages → Anthropic count_tokens body.

    Anthropic requires alternating user/assistant turns; we collapse
    consecutive same-role messages and guarantee the message list
    starts and ends with a user turn.
    """
    anth_msgs = []
    for msg in messages or ():
        role = msg.get('role')
        if role not in ('user', 'assistant'):
            continue

        content = msg.get('content')
        if isinstance(content, str):
            txt = content
        elif isinstance(content, list):
            parts = []
            for blk in content:
                if not isinstance(blk, dict):
                    continue
                btype = blk.get('type')
                if btype in ('text', 'input_text', 'output_text'):
                    parts.append(blk.get('text') or '')
                elif btype == 'image_url':
                    parts.append('[image]')
                elif btype == 'tool_result':
                    sub = blk.get('content')
                    if isinstance(sub, str):
                        parts.append(sub)
                    elif isinstance(sub, list):
                        for sb in sub:
                            if isinstance(sb, dict) and isinstance(sb.get('text'), str):
                                parts.append(sb['text'])
            txt = '\n\n'.join(parts)
        else:
            txt = ''

        for tc in msg.get('tool_calls') or ():
            fn = tc.get('function') or {}
            txt += f"\n[tool_call {fn.get('name','?')} {fn.get('arguments','')}]"

        if not txt.strip():
            continue
        anth_msgs.append({'role': role, 'content': txt})

    # Collapse consecutive same-role
    collapsed: list[dict] = []
    for m in anth_msgs:
        if collapsed and collapsed[-1]['role'] == m['role']:
            collapsed[-1]['content'] += '\n\n' + m['content']
        else:
            collapsed.append(m)

    if collapsed and collapsed[0]['role'] != 'user':
        collapsed.insert(0, {'role': 'user', 'content': '(conversation prefix)'})
    if collapsed and collapsed[-1]['role'] != 'user':
        collapsed.append({'role': 'user', 'content': '继续'})

    body: dict = {'model': model, 'messages': collapsed}
    if system:
        body['system'] = system if isinstance(system, str) else \
            '\n\n'.join(b.get('text', '') for b in system if isinstance(b, dict))
    if tools:
        body['tools'] = tools
    return body


class AnthropicAPICounter(TokenCounter):
    """Exact upstream count for Claude models."""

    name = 'anthropic_api'
    confidence = 'exact'
    needs_network = True

    def supports(self, model: str) -> bool:
        m = (model or '').lower()
        return 'claude' in m or 'anthropic' in m

    def count(self, messages: list, *, model: str,
              system: Any = None, tools: Any = None,
              api_base_url: Optional[str] = None,
              api_key: Optional[str] = None,
              **kwargs) -> Optional[int]:
        if not api_base_url or not api_key:
            return None
        url = _resolve_anthropic_count_url(api_base_url)
        if not url:
            return None

        try:
            import requests  # noqa: F401  — availability probe (raises ImportError if absent)
            from lib.proxy import proxies_for as _proxies_for  # noqa: F401
        except ImportError as e:
            logger.warning('[TokenCounter] requests / lib.proxy import failed: %s', e)
            return None

        body = _build_anthropic_count_body(messages, model=model, system=system, tools=tools)
        headers = {
            'Authorization': f'Bearer {api_key}',
            'anthropic-version': '2023-06-01',
            'Content-Type': 'application/json',
        }
        try:
            r = http_post(url, json=body, headers=headers,
                              timeout=(10, API_TIMEOUT),
                              verify=True)
            if r.status_code != 200:
                logger.warning('[TokenCounter] Anthropic count_tokens HTTP %d: %.200s',
                               r.status_code, r.text)
                return None
            data = r.json()
            n = data.get('input_tokens')
            if isinstance(n, int) and n >= 0:
                return n
            logger.warning('[TokenCounter] Anthropic response missing input_tokens: %r',
                           data)
            return None
        except Exception as e:
            logger.warning('[TokenCounter] Anthropic count_tokens exception: %s', e)
            return None


__all__ = ['AnthropicAPICounter']
