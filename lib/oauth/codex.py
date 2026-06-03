"""lib/oauth/codex.py — OpenAI Codex (ChatGPT Plus) OAuth PKCE authentication.

OAuth flow is identical to Claude, but the API usage is different:
  • URL: chatgpt.com/backend-api/codex/responses (NOT api.openai.com/v1)
  • Format: Responses API (NOT Chat Completions)
  • Requires request/response format translation

The translator converts between Chat Completions ↔ Responses API formats.
"""

import base64
import json
import time
import uuid

import requests

from lib.log import get_logger
from lib.oauth.pkce import generate_pkce_codes
from lib.oauth.token_store import load_token, save_token
from lib.http_client import http_post

logger = get_logger(__name__)

__all__ = [
    'CODEX_OAUTH_CONFIG',
    'codex_build_auth_url',
    'codex_exchange_code',
    'codex_refresh_token',
    'codex_get_valid_token',
    'codex_translate_request',
    'codex_translate_sse_event',
]

# ══════════════════════════════════════════════════════════
#  OAuth Configuration Constants
#  (from CLIProxyAPI v6.9.10 / OpenAI Codex CLI official)
# ══════════════════════════════════════════════════════════

CODEX_OAUTH_CONFIG = {
    'auth_url': 'https://auth.openai.com/oauth/authorize',
    'token_url': 'https://auth.openai.com/oauth/token',
    'client_id': 'app_EMoamEEZ73f0CkXaXp7hrann',
    'callback_port': 1455,
    'redirect_uri': 'http://localhost:1455/auth/callback',
    'scope': 'openid email profile offline_access',
    'provider': 'codex',
    'api_base': 'https://chatgpt.com/backend-api/codex',
}

_TOKEN_REFRESH_BUFFER = 300  # 5 minutes


def codex_build_auth_url() -> dict:
    """Build the Codex OAuth authorization URL with PKCE.

    Returns:
        dict with 'auth_url', 'state', 'pkce', 'callback_port', 'provider'.
    """
    pkce = generate_pkce_codes()
    state = uuid.uuid4().hex

    params = {
        'response_type': 'code',
        'client_id': CODEX_OAUTH_CONFIG['client_id'],
        'redirect_uri': CODEX_OAUTH_CONFIG['redirect_uri'],
        'scope': CODEX_OAUTH_CONFIG['scope'],
        'state': state,
        'code_challenge': pkce['code_challenge'],
        'code_challenge_method': 'S256',
        'audience': 'https://api.openai.com/v1',
    }

    query = '&'.join(f'{k}={requests.utils.quote(str(v), safe="")}' for k, v in params.items())
    auth_url = f"{CODEX_OAUTH_CONFIG['auth_url']}?{query}"

    logger.info('[Codex OAuth] Built auth URL (state=%s)', state[:8])
    return {
        'auth_url': auth_url,
        'state': state,
        'pkce': pkce,
        'callback_port': CODEX_OAUTH_CONFIG['callback_port'],
        'provider': 'codex',
    }


def codex_exchange_code(code: str, pkce_verifier: str) -> dict | None:
    """Exchange authorization code for Codex tokens.

    Args:
        code: Authorization code from OAuth callback.
        pkce_verifier: PKCE code verifier.

    Returns:
        Token dict or None on failure.
    """
    payload = {
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': CODEX_OAUTH_CONFIG['redirect_uri'],
        'client_id': CODEX_OAUTH_CONFIG['client_id'],
        'code_verifier': pkce_verifier,
    }

    try:
        token_url = CODEX_OAUTH_CONFIG['token_url']
        resp = http_post(
            token_url,
            data=payload,
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            timeout=30,
        )

        if resp.status_code != 200:
            logger.error('[Codex OAuth] Token exchange failed (HTTP %d): %.500s',
                         resp.status_code, resp.text)
            return None

        data = resp.json()
        access_token = data.get('access_token', '')
        refresh_token = data.get('refresh_token', '')
        id_token = data.get('id_token', '')
        expires_in = data.get('expires_in', 3600)

        if not access_token:
            logger.error('[Codex OAuth] No access_token in response')
            return None

        # Parse JWT to get account info
        email, account_id = _parse_jwt_claims(id_token)

        token_data = {
            'type': 'codex',
            'access_token': access_token,
            'refresh_token': refresh_token,
            'id_token': id_token,
            'account_id': account_id,
            'email': email,
            'expire': time.time() + expires_in,
            'expires_in': expires_in,
        }

        save_token('codex', token_data)
        logger.info('[Codex OAuth] Token exchange successful (email=%s, account=%s, expires_in=%ds)',
                     email, account_id[:8] if account_id else '?', expires_in)
        return token_data

    except Exception as e:
        logger.error('[Codex OAuth] Token exchange error: %s', e, exc_info=True)
        return None


def codex_refresh_token(refresh_tok: str = None) -> dict | None:
    """Refresh the Codex access token.

    Args:
        refresh_tok: Refresh token. If None, loads from stored token.

    Returns:
        Updated token dict or None.
    """
    if not refresh_tok:
        stored = load_token('codex')
        if not stored:
            logger.warning('[Codex OAuth] No stored token to refresh')
            return None
        refresh_tok = stored.get('refresh_token', '')

    if not refresh_tok:
        logger.warning('[Codex OAuth] No refresh token available')
        return None

    payload = {
        'grant_type': 'refresh_token',
        'refresh_token': refresh_tok,
        'client_id': CODEX_OAUTH_CONFIG['client_id'],
    }

    for attempt in range(3):
        try:
            token_url = CODEX_OAUTH_CONFIG['token_url']
            resp = http_post(
                token_url,
                data=payload,
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
                timeout=30,
            )

            if resp.status_code != 200:
                logger.warning('[Codex OAuth] Refresh failed (HTTP %d, attempt %d): %.300s',
                               resp.status_code, attempt + 1, resp.text)
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                return None

            data = resp.json()
            access_token = data.get('access_token', '')
            new_refresh = data.get('refresh_token', refresh_tok)
            id_token = data.get('id_token', '')
            expires_in = data.get('expires_in', 3600)

            if not access_token:
                logger.error('[Codex OAuth] No access_token in refresh response')
                return None

            email, account_id = _parse_jwt_claims(id_token)

            stored = load_token('codex') or {}
            stored.update({
                'access_token': access_token,
                'refresh_token': new_refresh,
                'id_token': id_token,
                'account_id': account_id or stored.get('account_id', ''),
                'email': email or stored.get('email', ''),
                'expire': time.time() + expires_in,
                'expires_in': expires_in,
                'last_refresh': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            })
            save_token('codex', stored)
            logger.info('[Codex OAuth] Token refreshed (expires_in=%ds)', expires_in)
            return stored

        except Exception as e:
            logger.warning('[Codex OAuth] Refresh error (attempt %d): %s', attempt + 1, e)
            if attempt < 2:
                time.sleep(2 ** attempt)

    return None


def codex_get_valid_token() -> str | None:
    """Get a valid Codex access token, refreshing if needed."""
    stored = load_token('codex')
    if not stored:
        return None

    access_token = stored.get('access_token', '')
    expire = stored.get('expire', 0)

    if not access_token:
        return None

    if time.time() > expire - _TOKEN_REFRESH_BUFFER:
        logger.info('[Codex OAuth] Token expiring soon, refreshing…')
        refreshed = codex_refresh_token(stored.get('refresh_token', ''))
        if refreshed:
            return refreshed.get('access_token')
        logger.warning('[Codex OAuth] Refresh failed, using potentially expired token')

    return access_token


# ══════════════════════════════════════════════════════════
#  Request Translator: Chat Completions → Responses API
#  Based on CLIProxyAPI v6.9.10 translator
# ══════════════════════════════════════════════════════════

def codex_translate_request(body: dict) -> dict:
    """Translate Chat Completions request body to Responses API format.

    Args:
        body: Standard OpenAI Chat Completions request body.

    Returns:
        Responses API request body for chatgpt.com/backend-api/codex/responses.
    """
    out = {
        'instructions': '',
        'stream': True,
        'store': False,
        'model': body.get('model', ''),
        'parallel_tool_calls': True,
        'reasoning': {
            'effort': body.get('reasoning_effort', 'medium'),
            'summary': 'auto',
        },
        'include': ['reasoning.encrypted_content'],
    }

    # NOTE: Codex does NOT support temperature, top_p, max_tokens — omit them

    # ── Convert messages[] → input[] ──
    messages = body.get('messages', [])
    input_items = []

    for msg in messages:
        role = msg.get('role', '')
        content = msg.get('content', '')

        if role == 'tool':
            # Tool result → function_call_output
            input_items.append({
                'type': 'function_call_output',
                'call_id': msg.get('tool_call_id', ''),
                'output': content if isinstance(content, str) else json.dumps(content),
            })
            continue

        # Regular message
        resp_role = 'developer' if role == 'system' else role
        content_parts = []

        if isinstance(content, str) and content:
            part_type = 'output_text' if role == 'assistant' else 'input_text'
            content_parts.append({'type': part_type, 'text': content})
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get('type', '')
                if btype == 'text':
                    part_type = 'output_text' if role == 'assistant' else 'input_text'
                    content_parts.append({'type': part_type, 'text': block.get('text', '')})
                elif btype == 'image_url' and role == 'user':
                    url = block.get('image_url', {}).get('url', '')
                    if url:
                        content_parts.append({'type': 'input_image', 'image_url': url})

        # Don't emit empty assistant messages when only tool_calls present
        if role != 'assistant' or content_parts:
            input_items.append({
                'type': 'message',
                'role': resp_role,
                'content': content_parts,
            })

        # Handle tool_calls on assistant messages → top-level function_call items
        tool_calls = msg.get('tool_calls', [])
        for tc in tool_calls:
            if tc.get('type') == 'function':
                func = tc.get('function', {})
                name = func.get('name', '')
                # Shorten long MCP tool names (Codex limit: 64 chars)
                if len(name) > 64:
                    name = name[:64]
                input_items.append({
                    'type': 'function_call',
                    'call_id': tc.get('id', ''),
                    'name': name,
                    'arguments': func.get('arguments', '{}'),
                })

    out['input'] = input_items

    # ── Convert tools[] (flatten function wrapper) ──
    tools = body.get('tools', [])
    if tools:
        resp_tools = []
        for tool in tools:
            if tool.get('type') != 'function':
                resp_tools.append(tool)
                continue
            func = tool.get('function', {})
            name = func.get('name', '')
            if len(name) > 64:
                name = name[:64]
            t = {'type': 'function', 'name': name}
            if func.get('description'):
                t['description'] = func['description']
            if func.get('parameters'):
                t['parameters'] = func['parameters']
            if func.get('strict') is not None:
                t['strict'] = func['strict']
            resp_tools.append(t)
        out['tools'] = resp_tools

    # ── Map tool_choice ──
    tc = body.get('tool_choice')
    if tc:
        if isinstance(tc, str):
            out['tool_choice'] = tc
        elif isinstance(tc, dict) and tc.get('type') == 'function':
            name = tc.get('function', {}).get('name', '')
            out['tool_choice'] = {'type': 'function', 'name': name}

    return out


# ══════════════════════════════════════════════════════════
#  Response Translator: Responses API SSE → Chat Completions SSE
#  Converts streaming events from chatgpt.com back to standard format
# ══════════════════════════════════════════════════════════

class CodexSSETranslator:
    """Stateful translator for Codex Responses API SSE → Chat Completions format.

    Usage::

        translator = CodexSSETranslator(model='gpt-5.2-codex')
        for raw_line in sse_stream:
            for translated_line in translator.translate(raw_line):
                yield translated_line
    """

    def __init__(self, model: str = ''):
        self.model = model
        self._tool_calls = {}  # index → {id, name, arguments}
        self._tc_index = 0
        self._finished = False

    def translate(self, raw_line: str) -> list[str]:
        """Translate a single SSE line from Codex format to Chat Completions.

        Args:
            raw_line: Raw SSE data line (after "data: " prefix).

        Returns:
            List of translated SSE data strings (may be 0, 1, or multiple).
        """
        if not raw_line or raw_line.strip() == '[DONE]':
            return ['[DONE]']

        try:
            event = json.loads(raw_line)
        except (json.JSONDecodeError, TypeError) as e:
            logger.debug('[Codex SSE] Unparseable line: %.200s — %s', raw_line, e)
            return []

        event_type = event.get('type', '')
        results = []

        if event_type == 'response.output_text.delta':
            # Text content delta
            delta_text = event.get('delta', '')
            if delta_text:
                results.append(self._make_chunk(
                    delta={'role': 'assistant', 'content': delta_text}
                ))

        elif event_type == 'response.reasoning_summary_text.delta':
            # Reasoning/thinking content delta
            delta_text = event.get('delta', '')
            if delta_text:
                results.append(self._make_chunk(
                    delta={'role': 'assistant', 'reasoning_content': delta_text}
                ))

        elif event_type == 'response.output_item.added':
            # New output item — could be function_call
            item = event.get('item', {})
            if item.get('type') == 'function_call':
                idx = self._tc_index
                self._tool_calls[idx] = {
                    'id': item.get('call_id', ''),
                    'name': item.get('name', ''),
                    'arguments': '',
                }
                results.append(self._make_chunk(
                    delta={
                        'role': 'assistant',
                        'tool_calls': [{
                            'index': idx,
                            'id': item.get('call_id', ''),
                            'type': 'function',
                            'function': {
                                'name': item.get('name', ''),
                                'arguments': '',
                            },
                        }],
                    }
                ))
                self._tc_index += 1

        elif event_type == 'response.function_call_arguments.delta':
            # Tool call arguments delta
            delta_args = event.get('delta', '')
            if delta_args and self._tool_calls:
                idx = self._tc_index - 1  # current tool call
                if idx in self._tool_calls:
                    self._tool_calls[idx]['arguments'] += delta_args
                    results.append(self._make_chunk(
                        delta={
                            'tool_calls': [{
                                'index': idx,
                                'function': {'arguments': delta_args},
                            }],
                        }
                    ))

        elif event_type == 'response.completed':
            # Stream complete
            resp = event.get('response', {})
            finish_reason = 'stop'
            if resp.get('output', []):
                for item in resp['output']:
                    if item.get('type') == 'function_call':
                        finish_reason = 'tool_calls'
                        break

            usage = resp.get('usage', {})
            chunk = self._make_chunk(
                delta={},
                finish_reason=finish_reason,
                usage={
                    'prompt_tokens': usage.get('input_tokens', 0),
                    'completion_tokens': usage.get('output_tokens', 0),
                    'total_tokens': usage.get('total_tokens',
                                              usage.get('input_tokens', 0) + usage.get('output_tokens', 0)),
                } if usage else None,
            )
            results.append(chunk)
            self._finished = True

        # Ignore other event types (response.created, response.in_progress, etc.)
        return results

    def _make_chunk(self, delta: dict, finish_reason: str = None,
                    usage: dict = None) -> str:
        """Build a Chat Completions SSE chunk."""
        chunk = {
            'id': 'chatcmpl-codex',
            'object': 'chat.completion.chunk',
            'created': int(time.time()),
            'model': self.model,
            'choices': [{
                'index': 0,
                'delta': delta,
                'finish_reason': finish_reason,
            }],
        }
        if usage:
            chunk['usage'] = usage
        return json.dumps(chunk, ensure_ascii=False)


def codex_translate_sse_event(raw_line: str, translator: CodexSSETranslator) -> list[str]:
    """Convenience wrapper around CodexSSETranslator.translate().

    Args:
        raw_line: Raw SSE data line.
        translator: Stateful translator instance.

    Returns:
        List of translated SSE data strings.
    """
    return translator.translate(raw_line)


# ── Internal helpers ──

def _parse_jwt_claims(id_token: str) -> tuple[str, str]:
    """Parse JWT ID token to extract email and account_id.

    Returns:
        (email, account_id) tuple.
    """
    if not id_token:
        return '', ''
    try:
        parts = id_token.split('.')
        if len(parts) < 2:
            return '', ''
        payload = parts[1] + '=' * (4 - len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        email = claims.get('email', '')
        # OpenAI stores account info in custom claim
        auth_info = claims.get('https://api.openai.com/auth', {})
        account_id = auth_info.get('chatgpt_account_id', claims.get('sub', ''))
        return email, account_id
    except Exception as e:
        logger.debug('[Codex OAuth] Failed to parse JWT: %s', e)
        return '', ''
