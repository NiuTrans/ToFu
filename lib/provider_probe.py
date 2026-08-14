"""Access-matrix cell-probe engine — server-owned background reachability test.

Moved out of ``routes/config.py`` (2026-06). Sends a tiny real request to
every (key × wire-id) SLOT of a provider and validates its output payload.
The probed (key, id) set is the dispatcher's own slot set
each key's pool resolved through ``resolve_request_ids`` (an explicit
``request_ids`` pool, possibly replaced per key by ``key_access``, else
``[model_id] + aliases``) — because each wire id on a gateway can route to
a genuinely DIFFERENT upstream model, and a (key, id) pair the dispatcher
never routes is not worth a verdict.

The probe is a long-running fan-out: it runs in a background thread and its
progress is persisted to disk (``data/config/probe_cache/``) as a secret-free
snapshot, so closing Settings or restarting the server doesn't lose progress.

``routes/config.py`` re-exports every public name here under its legacy
private alias (``_probe_one_cell``, ``_probe_cell_multi``,
``_run_cell_probe_task``, ``_probe_cache_path``, ``_probe_cell_key``,
``_persist_probe_task``, ``_public_probe_snapshot``, ``CELL_PROBE_TASKS``,
``CELL_PROBE_LOCK``, ``_time``) so existing call sites and tests keep working.

NOTE for tests: ``probe_cell_multi`` / ``run_cell_probe_task`` call
``probe_one_cell`` THROUGH THIS MODULE'S global, so
``mock.patch.object(lib.provider_probe, 'probe_one_cell', ...)`` (or patching
the re-exported name) takes effect. Same for ``probe_cache_path``.
"""

import hashlib
import io
import json
import re
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed

from lib.config_dir import config_path as _config_path
from lib.json_store import read_json, write_json_atomic  # noqa: F401  (read_json re-used by routes)
from lib.log import get_logger
from lib.model_info.capability_taxonomy import is_chat_model as _is_chat_model

# Verdict for cells whose capabilities have no probe surface we implement
# (anything outside image_gen / embedding / transcription). Chat-probing a
# non-chat model is a guaranteed false positive (the Meituan gateway's Java
# router hits ``Random.nextInt(0)`` → "bound must be positive" when the
# chat-binding candidate list is empty), so unknown non-chat cells are
# skipped rather than chat-probed. Known non-chat modalities are probed via
# their REAL endpoint — see nonchat_probe_fn().
SKIPPED = 'skipped'

# Public snapshot contract. Version 2 is the first version in which ``ok``
# means a semantically valid response payload was observed. Version 1 treated
# HTTP 400 as success and treated any HTTP 200 body as success, which produced
# contradictory UI such as a green pip whose tooltip said ``HTTP 400``.
PROBE_SCHEMA_VERSION = 2

_HTTP_STATUS_RE = re.compile(r'\bHTTP\s+(\d{3})\b', re.IGNORECASE)
_CHAT_PROBE_PROMPT = 'Reply with exactly OK.'
_CHAT_PROBE_MAX_TOKENS = 16
_UNSET = object()

# Image cells generate one real (tiny) billed image per probe, so they get a
# single attempt regardless of the matrix attempts selector — multiplying a
# billed generation to filter a rare transient 429 is a bad trade.
_IMAGE_PROBE_MIN_TIMEOUT = 60  # generation routinely takes 10-40s

logger = get_logger(__name__)


def probe_one_cell(base_url, api_key, model_id, extra_headers, timeout,
                   protocol='openai', oauth='', adapter=None):
    """Send a minimal completion to test one (key, model) pair.

    Returns one of: 'ok', 'rate_limited', 'unauthorized', 'not_found',
    'unavailable', 'error' plus a short human-readable detail string.

    ``ok`` is intentionally strict: the endpoint must return 2xx *and* a
    protocol-valid response containing generated output. Merely reaching the
    gateway is not enough. In particular, HTTP 400 is ``bad_request`` even if
    it proves a route exists — the app cannot consume a request the provider
    rejects, so painting that cell green would be a false promise.

    ``protocol='anthropic'`` probes the Anthropic Messages API
    (``POST /v1/messages`` with ``x-api-key`` + ``anthropic-version``)
    instead of OpenAI Chat Completions. ``protocol='responses'`` probes the
    Responses API (``POST …/responses``). The status→verdict table is
    identical for all protocols.

    ``oauth`` (``'claude'``/``'codex'``) marks a SUBSCRIPTION provider whose
    configured api_key is the 'oauth-managed' SENTINEL — probing with it
    literally would return a 401 and wrongly flag a working subscription as
    recommend-disable. We resolve the live per-request token via
    :func:`lib.oauth.outbound.resolve_oauth_request` and send it as
    ``x-api-key`` (Anthropic's 2026 block rejects ``Authorization: Bearer``
    for subscription tokens). A provider with no usable token is reported as
    the NEUTRAL verdict ``'not_logged_in'`` — never a model fault, never
    recommend-disable.
    """
    from lib.http_client import http_post

    # ── Subscription-ADAPTER providers (E4) ─────────────────────────────
    # The provider IS a CLIProxyAPI sidecar on the user's desktop agent;
    # its base_url is loopback-ON-THE-AGENT. Probe the cell through the
    # bridge relay with the provider's api_key (the adapter key, minted
    # server-side) and classify the status exactly like a direct probe.
    if adapter:
        import json as _json
        from urllib.parse import urlparse as _urlparse
        from lib.desktop import adapter as _ad
        from lib.desktop.egress import EgressUnavailable as _EU
        url = base_url.rstrip('/') + '/chat/completions'
        _pu = _urlparse(url)
        path = _pu.path + (('?' + _pu.query) if _pu.query else '')
        headers = {'Authorization': 'Bearer %s' % api_key} if api_key else {}
        if extra_headers:
            headers.update(extra_headers)
        payload = {
            'model': model_id,
            'messages': [{'role': 'user', 'content': _CHAT_PROBE_PROMPT}],
            'max_tokens': _CHAT_PROBE_MAX_TOKENS,
            'stream': False,
        }
        try:
            resp = _ad.relay_http(
                adapter.get('agent_id', ''), int(adapter.get('port') or 0),
                path, method='POST', headers=headers,
                body=_json.dumps(payload).encode(), timeout=timeout)
        except _EU as e:
            logger.debug('[CellProbe] %s @ %s desktop egress unavailable: %s',
                         model_id, base_url, e)
            return 'unavailable', str(e)[:160]
        except Exception as e:
            logger.warning('[CellProbe] %s @ %s adapter relay error: %s',
                           model_id, base_url, e)
            return 'unavailable', 'network: %s' % str(e)[:120]
        code = resp.status_code
        try:
            body = resp.text[:65536]
        except (UnicodeDecodeError, ValueError, OSError, AttributeError) as e:
            logger.debug('[CellProbe] %s @ %s: could not read response body: %s',
                         model_id, base_url, e)
            body = ''
        return _classify_verified(
            code, body, validate=lambda parsed, raw, raw_bytes:
            _validate_chat_response(parsed, raw, raw_bytes, 'openai'),
            proof='generated text', surface='/chat/completions',
            secret_values=(api_key,))

    # ── Subscription (OAuth) providers ──────────────────────────────────
    if oauth:
        if oauth == 'codex':
            # S4: the desktop-egress stream makes a REAL codex probe possible
            # (1-token Responses API request, classified by status). Before
            # S3 there was no streaming path, so this returned SKIPPED.
            from lib.oauth.codex import (
                codex_get_valid_token, codex_translate_request)
            from lib.oauth.token_store import load_token
            token = codex_get_valid_token()
            if not token:
                return 'not_logged_in', ('Codex subscription not logged in '
                                         '(no valid OAuth token)')
            stored = load_token('codex') or {}
            account_id = stored.get('account_id', '')
            import uuid as _uuid
            hdrs = {
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {token}',
                'OpenAI-Beta': 'responses=experimental',
                'originator': 'codex-tui',
                # Single line on purpose: the drift guard
                # (test_oauth_cloaking_drift) greps this literal verbatim.
                'User-Agent': 'codex-tui/0.146.0 (Mac OS 26.5.0; arm64) iTerm.app/3.6.10 (codex-tui; 0.146.0)',
                'session_id': _uuid.uuid4().hex,
            }
            if account_id:
                hdrs['chatgpt-account-id'] = account_id
            body = codex_translate_request({
                'model': model_id,
                'messages': [{'role': 'user', 'content': _CHAT_PROBE_PROMPT}],
                'max_tokens': _CHAT_PROBE_MAX_TOKENS,
                'stream': True,
            })
            url = f'{base_url.rstrip("/")}/responses'
            import json as _json
            from lib.desktop import egress as _eg
            try:
                route = _eg.route_request(url, user_id='')
                if route == 'direct':
                    resp = http_post(url, json=body, headers=hdrs,
                                     timeout=timeout)
                    code = resp.status_code
                    try:
                        resp_body = resp.text[:65536]
                    except (UnicodeDecodeError, ValueError, OSError) as e:
                        logger.debug('[CellProbe] codex %s: could not read response body: %s',
                                     model_id, e)
                        resp_body = ''
                else:
                    reader = _eg.open_stream(url, method='POST', headers=hdrs,
                                             body=_json.dumps(body).encode(),
                                             agent_id=route)
                    code = reader.status_code
                    resp_body = reader.read_all_text()[:65536]
            except _eg.EgressUnavailable as e:
                logger.debug('[CellProbe] codex %s desktop egress unavailable: %s',
                             model_id, e)
                return 'unavailable', str(e)[:160]
            except Exception as e:
                logger.warning('[CellProbe] codex %s network error: %s',
                               model_id, e)
                return 'unavailable', 'network: %s' % str(e)[:120]
            return _classify_verified(
                code, resp_body,
                validate=lambda parsed, raw, raw_bytes:
                _validate_chat_response(parsed, raw, raw_bytes, 'responses'),
                proof='generated text', surface='/responses',
                secret_values=(token, account_id))
        from lib.llm.anthropic_outbound import anthropic_messages_url
        from lib.oauth.outbound import claude_oauth_url, resolve_oauth_request
        payload = {
            'model': model_id,
            'messages': [{'role': 'user', 'content': _CHAT_PROBE_PROMPT}],
            'max_tokens': _CHAT_PROBE_MAX_TOKENS,
        }
        try:
            token, hdrs, body = resolve_oauth_request(oauth, payload,
                                                      extra_headers)
        except RuntimeError as e:
            # Not logged in / refresh failed — a SESSION state, not a model
            # fault. Distinct from an authenticated 401.
            logger.info('[CellProbe] %s @ %s oauth token unavailable: %s',
                        model_id, base_url, e)
            return 'not_logged_in', str(e)[:160]
        url = claude_oauth_url(anthropic_messages_url(base_url))
        headers = {
            'Content-Type': 'application/json',
            'anthropic-version': '2023-06-01',
        }
        headers.update(hdrs or {})          # beta flags, x-app, User-Agent
        headers.pop('Authorization', None)  # subscription tokens ride x-api-key
        headers['x-api-key'] = token
        payload = body                      # resolve prepends the identity block
        import json as _json
        from lib.desktop import egress as _eg
        try:
            route = _eg.route_request(url, user_id='')
        except _eg.EgressUnavailable as e:
            logger.debug('[CellProbe] %s @ %s egress route unavailable: %s',
                         model_id, base_url, e)
            return 'unavailable', str(e)[:160]
        try:
            if route == 'direct':
                resp = http_post(url, json=payload, headers=headers,
                                 timeout=timeout)
            else:
                resp = _eg.egress_http(url, method='POST', headers=headers,
                                       body=_json.dumps(payload).encode(),
                                       timeout=timeout, user_id='')
        except _eg.EgressUnavailable as e:
            logger.debug('[CellProbe] %s @ %s desktop egress unavailable: %s',
                         model_id, base_url, e)
            return 'unavailable', str(e)[:160]
        except Exception as e:
            logger.warning('[CellProbe] %s @ %s network error: %s',
                           model_id, base_url, e)
            return 'unavailable', 'network: %s' % str(e)[:120]
        code = resp.status_code
        try:
            resp_body = resp.text[:65536]
        except (UnicodeDecodeError, ValueError, OSError) as e:
            logger.debug('[CellProbe] %s @ %s: could not read response body: %s',
                         model_id, base_url, e)
            resp_body = ''
        return _classify_verified(
            code, resp_body,
            validate=lambda parsed, raw, raw_bytes:
            _validate_chat_response(parsed, raw, raw_bytes, 'anthropic'),
            proof='generated text', surface='/v1/messages',
            secret_values=(token,))

    # Keep the output budget tiny but large enough for reasoning-capable
    # providers to produce visible text. A successful HTTP envelope without
    # generated content is deliberately not sufficient evidence.
    if protocol == 'responses':
        # Responses API providers (DeepSeek …) — minimal stateless payload.
        from lib.llm.responses_outbound import (
            openai_body_to_responses, responses_url,
        )
        url = responses_url(base_url)
        headers = {'Authorization': 'Bearer %s' % api_key} if api_key else {}
        if extra_headers:
            headers.update(extra_headers)
        payload, _rev = openai_body_to_responses(
            {'model': model_id,
             'messages': [{'role': 'user', 'content': _CHAT_PROBE_PROMPT}],
             'max_tokens': _CHAT_PROBE_MAX_TOKENS},
            profile='default', stream=False)
    elif protocol == 'anthropic':
        from lib.llm.anthropic_outbound import (
            anthropic_headers, anthropic_messages_url,
        )
        url = anthropic_messages_url(base_url)
        headers = anthropic_headers(api_key, extra_headers)
        payload = {
            'model': model_id,
            'messages': [{'role': 'user', 'content': _CHAT_PROBE_PROMPT}],
            'max_tokens': _CHAT_PROBE_MAX_TOKENS,
        }
    else:
        url = base_url.rstrip('/') + '/chat/completions'
        headers = {'Authorization': 'Bearer %s' % api_key} if api_key else {}
        if extra_headers:
            headers.update(extra_headers)
        payload = {
            'model': model_id,
            'messages': [{'role': 'user', 'content': _CHAT_PROBE_PROMPT}],
            'max_tokens': _CHAT_PROBE_MAX_TOKENS,
            'stream': False,
        }
    try:
        resp = http_post(url, json=payload, headers=headers, timeout=timeout)
    except Exception as e:
        logger.warning('[CellProbe] %s @ %s network error: %s', model_id, base_url, e)
        return 'unavailable', 'network: %s' % str(e)[:120]

    code = resp.status_code
    try:
        body = resp.text[:65536]
    except (UnicodeDecodeError, ValueError, OSError) as e:
        logger.debug('[CellProbe] %s @ %s: could not read response body: %s',
                     model_id, base_url, e)
        body = ''
    return _classify_verified(
        code, body,
        validate=lambda parsed, raw, raw_bytes:
        _validate_chat_response(parsed, raw, raw_bytes, protocol),
        proof='generated text',
        surface=('/responses' if protocol == 'responses'
                 else '/v1/messages' if protocol == 'anthropic'
                 else '/chat/completions'),
        secret_values=(api_key,))


# Body markers that mean "this gateway has NO route for the model" — as
# opposed to "route exists but the tiny probe shape was rejected". They ride
# any status: the Meituan AIGC gateway answers a missing route with HTTP 400
# stage=validation and a CHINESE message ("不支持的模型类型(model=…)"), which
# the status ladder alone misreads as reachable (2026-08-04 opus-5 incident —
# vertex./aws. cells showed green while every real chat call 400'd).
_ROUTE_MISSING_MARKERS = (
    'model_not_found', 'does not exist', 'no such model',
    'unsupported model', 'unsupported_model', 'model not supported',
    'invalid model name',
    '不支持的模型类型',
)


def _redact_probe_text(body, secret_values=()):
    text = str(body or '')
    for secret in secret_values or ():
        secret = str(secret or '')
        if len(secret) >= 6:
            text = text.replace(secret, '***')
    # Provider errors sometimes echo credential-like application ids even
    # when the exact value is not available at cache-normalization time.
    text = re.sub(
        r'(?i)(\bapp(?:lication)?[ _-]?id\b\s*[:=]?\s*)[A-Za-z0-9._-]{6,}',
        r'\1***', text)
    text = re.sub(r'(?i)(\bbearer\s+)[A-Za-z0-9._~+/-]{6,}', r'\1***', text)
    return text


def _classify_status(code: int, body: str):
    """Map an HTTP status (+body excerpt) to a (verdict, detail) pair.

    This only classifies the HTTP envelope. Callers must additionally validate
    the payload before exposing ``ok``. The routing-rejection body sniff runs
    first so gateways that encode a missing model as HTTP 200/400 still cannot
    read as available.
    """
    body = _redact_probe_text(body)
    lower = body.lower()
    if any(m in lower for m in _ROUTE_MISSING_MARKERS):
        return 'not_found', 'HTTP %d %.120s' % (code, body)
    if 200 <= code < 300:
        return 'ok', 'HTTP %d' % code
    if code in (400, 409, 415, 422):
        return 'bad_request', 'HTTP %d %.160s' % (code, body)
    if code == 429 or code == 402:
        return 'rate_limited', 'HTTP %d %.120s' % (code, body)
    if code in (401, 403):
        return 'unauthorized', 'HTTP %d %.120s' % (code, body)
    if code == 404:
        return 'not_found', 'HTTP %d %.120s' % (code, body)
    if code in (500, 502, 503, 504, 529):
        return 'unavailable', 'HTTP %d %.120s' % (code, body)
    return 'error', 'HTTP %d %.120s' % (code, body)


def _json_or_none(body: str):
    try:
        return json.loads(body)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.debug('[CellProbe] response is not JSON: %s', exc)
        return None


def _content_text(value) -> str:
    """Extract user-visible text from a known response content value."""
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, list):
        return ''
    out = []
    for part in value:
        if isinstance(part, str):
            out.append(part)
            continue
        if not isinstance(part, dict):
            continue
        if part.get('type') in ('text', 'output_text'):
            text = part.get('text')
            if isinstance(text, str):
                out.append(text)
    return ''.join(out).strip()


def _chat_json_text(parsed, protocol: str) -> str:
    """Extract generated text from OpenAI, Anthropic, or Responses JSON."""
    if not isinstance(parsed, dict):
        return ''

    if protocol == 'anthropic':
        text = _content_text(parsed.get('content'))
        if text:
            return text

    if protocol == 'responses':
        text = _content_text(parsed.get('output_text'))
        if text:
            return text
        output = parsed.get('output')
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                text = _content_text(item.get('content'))
                if text:
                    return text

    choices = parsed.get('choices')
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            msg = choice.get('message')
            if isinstance(msg, dict):
                text = _content_text(msg.get('content'))
                if text:
                    return text
            text = _content_text(choice.get('text'))
            if text:
                return text
    return ''


def _sse_generated_text(raw: str) -> str:
    """Extract a text delta from a Responses/Anthropic SSE transcript."""
    for line in (raw or '').splitlines():
        if not line.startswith('data:'):
            continue
        payload = line[5:].strip()
        if not payload or payload == '[DONE]':
            continue
        event = _json_or_none(payload)
        if not isinstance(event, dict):
            continue
        # Responses API: response.output_text.delta; Anthropic Messages:
        # content_block_delta with delta.text. Some compatible gateways put a
        # plain string directly in ``delta``.
        delta = event.get('delta')
        if isinstance(delta, str) and delta.strip():
            return delta.strip()
        if isinstance(delta, dict):
            text = delta.get('text')
            if isinstance(text, str) and text.strip():
                return text.strip()
        text = _chat_json_text(event, 'responses')
        if text:
            return text
        response = event.get('response')
        text = _chat_json_text(response, 'responses')
        if text:
            return text
    return ''


def _validate_chat_response(parsed, raw, _raw_bytes, protocol):
    if _chat_json_text(parsed, protocol):
        return None
    if _sse_generated_text(raw):
        return None
    if parsed is None:
        return 'non-JSON/non-SSE response'
    return 'no generated text in response'


def _classify_verified(code, body, *, validate, proof, surface,
                       raw_bytes=None, parsed=_UNSET, secret_values=()):
    """Require both a successful HTTP envelope and semantic output proof."""
    body = _redact_probe_text(body, secret_values)
    status, detail = _classify_status(code, body)
    if status != 'ok':
        return status, detail
    if parsed is _UNSET:
        parsed = _json_or_none(body)
    reason = validate(parsed, body, raw_bytes)
    if reason:
        return 'invalid_response', (
            'HTTP %d via %s — invalid response (%s) %.120s'
            % (code, surface, reason, body))
    return 'ok', 'HTTP %d · verified %s via %s' % (code, proof, surface)


def _post_and_classify(url, headers, timeout, *, json_body=None, files=None,
                       data=None, validate=None, surface='', proof='output',
                       secret_values=()):
    """POST one modality probe and classify the answer.

    Shared tail for every non-chat probe: same transport error handling and
    status→verdict ladder as :func:`probe_one_cell`, plus an optional
    ``validate(parsed_json, raw_body, raw_bytes)`` hook that inspects a 2xx
    response's shape (None = verified). A 2xx whose shape fails validation is
    ``invalid_response``: the endpoint answered, but not in a dialect the app
    can consume.
    ``surface`` names the endpoint in the ok detail (e.g. '/embeddings').
    """
    from lib.http_client import http_post

    try:
        resp = http_post(url, json=json_body, files=files, data=data,
                         headers=headers, timeout=timeout)
    except Exception as e:
        logger.warning('[CellProbe] %s network error: %s', surface or url, e)
        return 'unavailable', 'network: %s' % str(e)[:120]
    code = resp.status_code
    try:
        body = resp.text[:65536]
    except (UnicodeDecodeError, ValueError, OSError) as e:
        logger.debug('[CellProbe] %s: could not read response body: %s',
                     surface or url, e)
        body = ''
    raw_bytes = getattr(resp, 'content', None)
    if not isinstance(raw_bytes, bytes):
        raw_bytes = None
    try:
        parsed = resp.json()
    except (AttributeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.debug('[CellProbe] response.json unavailable; parsing body: %s', exc)
        parsed = _json_or_none(body)
    return _classify_verified(
        code, body, validate=validate or (lambda parsed, raw, raw_b: None),
        proof=proof, surface=surface or url, raw_bytes=raw_bytes,
        parsed=parsed, secret_values=secret_values)


# ── Non-chat modality probes ──────────────────────────────────────────

_SILENCE_WAV: bytes | None = None


def _silence_wav_bytes(duration_s: float = 0.3, rate: int = 16000) -> bytes:
    """A minimal valid PCM WAV of digital silence (16-bit mono).

    A transcription endpoint must accept it and answer 200 with an empty (or
    whitespace) transcript — enough to prove the (key, model) routing works
    without sending real speech. ~10 KB at the defaults.
    """
    global _SILENCE_WAV
    if _SILENCE_WAV is None:
        pcm = b'\x00\x00' * int(duration_s * rate)
        hdr = (b'RIFF' + (36 + len(pcm)).to_bytes(4, 'little') + b'WAVE'
               + b'fmt ' + (16).to_bytes(4, 'little')
               + (1).to_bytes(2, 'little')          # PCM
               + (1).to_bytes(2, 'little')          # mono
               + rate.to_bytes(4, 'little')
               + (rate * 2).to_bytes(4, 'little')   # byte rate
               + (2).to_bytes(2, 'little')          # block align
               + (16).to_bytes(2, 'little')         # bits/sample
               + b'data' + len(pcm).to_bytes(4, 'little'))
        _SILENCE_WAV = hdr + pcm
    return _SILENCE_WAV


def _auth_headers(api_key, extra_headers):
    headers = {'Authorization': 'Bearer %s' % api_key} if api_key else {}
    if extra_headers:
        headers.update(extra_headers)
    return headers


def _validate_embedding(parsed, _raw, _raw_bytes):
    try:
        emb = (parsed or {}).get('data', [{}])[0].get('embedding')
    except (AttributeError, IndexError, TypeError) as _e:
        logger.debug('validate embedding: missing attribute/short/malformed/unexpected type (%s)', _e)
        return 'no data[0].embedding'
    return None if emb else 'empty embedding vector'


def _validate_transcription(parsed, _raw, _raw_bytes):
    if not isinstance(parsed, dict):
        return 'non-JSON response'
    return None if isinstance(parsed.get('text'), str) else 'missing text field'


def _validate_images_api(parsed, _raw, _raw_bytes):
    try:
        item = (parsed or {}).get('data', [{}])[0]
    except (AttributeError, IndexError, TypeError) as _e:
        logger.debug('validate images api: missing attribute/short/malformed/unexpected type (%s)', _e)
        return 'no data[0]'
    if item.get('b64_json') or item.get('url'):
        return None
    return 'no image payload (b64_json/url)'


def _validate_image_chat(parsed, _raw, _raw_bytes):
    try:
        content = (parsed or {})['choices'][0]['message'].get('content')
    except (AttributeError, IndexError, KeyError, TypeError) as _e:
        logger.debug('validate image chat: missing attribute/short/malformed/missing key/unexpected type (%s)', _e)
        return 'no choices[0].message.content'
    if isinstance(content, str):
        return None if content.strip() else 'empty text content'
    if isinstance(content, list):
        return None if content else 'empty content parts'
    return 'unexpected content type %s' % type(content).__name__


def probe_embedding_cell(base_url, api_key, model_id, extra_headers, timeout,
                         protocol='openai'):
    """Probe an embedding model via ``POST /embeddings`` (one short input).

    Same (base_url, api_key, model_id, extra_headers, timeout, protocol)
    signature as :func:`probe_one_cell` so ``probe_cell_multi`` can drive it;
    ``protocol`` is accepted and ignored (embeddings are OpenAI-shaped here).
    """
    url = base_url.rstrip('/') + '/embeddings'
    return _post_and_classify(
        url, _auth_headers(api_key, extra_headers), timeout,
        json_body={'model': model_id, 'input': 'ping'},
        validate=_validate_embedding, surface='/embeddings',
        proof='embedding vector', secret_values=(api_key,))


def probe_transcription_cell(base_url, api_key, model_id, extra_headers,
                             timeout, protocol='openai'):
    """Probe an ASR model via multipart ``POST /audio/transcriptions``
    carrying a 0.3s silence WAV (no real speech leaves the box)."""
    url = base_url.rstrip('/') + '/audio/transcriptions'
    files = {'file': ('probe.wav', io.BytesIO(_silence_wav_bytes()), 'audio/wav')}
    data = {'model': model_id, 'response_format': 'json'}
    return _post_and_classify(
        url, _auth_headers(api_key, extra_headers), timeout,
        files=files, data=data,
        validate=_validate_transcription, surface='/audio/transcriptions',
        proof='transcript payload', secret_values=(api_key,))


def probe_image_cell(base_url, api_key, model_id, extra_headers, timeout,
                     protocol='openai'):
    """Probe an image_gen model by generating one tiny image — the only
    definitive test, and it bills ~1 generation.

    Surface mirrors the app's own image path (:func:`lib.image_gen._slots`):
    ``openai_image`` slots POST ``/images/generations``; everything else
    POSTs ``/chat/completions`` with ``modalities: ['TEXT','IMAGE']``
    (gemini-style). Image models rejected by a plain chat probe (the
    'bound must be positive' incident) answer here because the gateway
    routes modalities-carrying requests to the image binding.
    """
    headers = _auth_headers(api_key, extra_headers)
    if protocol == 'openai_image':
        url = base_url.rstrip('/') + '/images/generations'
        payload = {'model': model_id,
                   'prompt': 'a single small red dot on a white background',
                   'n': 1}
        return _post_and_classify(url, headers, timeout, json_body=payload,
                                  validate=_validate_images_api,
                                  surface='/images/generations',
                                  proof='image payload',
                                  secret_values=(api_key,))
    url = base_url.rstrip('/') + '/chat/completions'
    payload = {
        'model': model_id,
        'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': 'Draw a single small red dot.'}]}],
        'modalities': ['TEXT', 'IMAGE'],
        'stream': False,
    }
    return _post_and_classify(url, headers, timeout, json_body=payload,
                              validate=_validate_image_chat,
                              surface='image chat', proof='image payload',
                              secret_values=(api_key,))


def _validate_tts(_parsed, raw, raw_bytes):
    # /audio/speech answers with raw audio bytes (no JSON envelope). The
    # _post_and_classify harness hands us resp.text (decoded, best-effort)
    # — the container magic survives as ASCII/latin-1 chars.
    data = raw_bytes
    if not data and raw:
        data = raw.encode('latin-1', errors='ignore')
    if not data:
        return 'empty body'
    if data[:4] == b'RIFF' or data[:3] == b'ID3' or data[:4] in (b'fLaC', b'OggS'):
        return None
    if data[0] == 0xff:  # MP3 frame sync
        return None
    return 'non-audio payload'


def probe_tts_cell(base_url, api_key, model_id, extra_headers, timeout,
                   protocol='openai'):
    """Probe a TTS model via ``POST /audio/speech`` (one word of input).

    Voice resolution mirrors the app's own path (lib.tts.default_voice):
    a configured deployment voice wins, otherwise the documented fallback
    is used — a provider that rejects the voice answers 400, which the
    classifier still counts as reachable (routing proven).
    """
    from lib.tts import default_voice
    url = base_url.rstrip('/') + '/audio/speech'
    payload = {'model': model_id, 'input': 'ping',
               'voice': default_voice(), 'response_format': 'wav'}
    return _post_and_classify(
        url, _auth_headers(api_key, extra_headers), timeout,
        json_body=payload, validate=_validate_tts, surface='/audio/speech',
        proof='audio bytes', secret_values=(api_key,))


def nonchat_probe_fn(caps):
    """Return the modality probe function for a non-chat caps list.

    Priority when a model carries several non-chat caps: image_gen >
    transcription > embedding. Returns None for capabilities with no
    implemented surface (caller keeps the 'skipped' verdict).
    """
    if not caps:
        return None
    if 'image_gen' in caps:
        return probe_image_cell
    if 'transcription' in caps:
        return probe_transcription_cell
    if 'tts' in caps:
        return probe_tts_cell
    if 'embedding' in caps:
        return probe_embedding_cell
    return None


# probe_fn → probe_surface stamp. Every cell records WHICH surface produced
# its verdict so the frontend can tell a FRESH modality verdict (e.g. an
# image-surface not_found — meaningful, must reach the user) from a STALE
# chat-probe false positive on a non-chat model (must heal on ingest).
# Cells without the stamp (pre-stamp snapshots) or stamped 'chat' are the
# stale kind; 'none' marks a skipped (unprobed) cell.
_PROBE_SURFACE_NAMES = {
    probe_image_cell: 'image',
    probe_transcription_cell: 'transcription',
    probe_tts_cell: 'tts',
    probe_embedding_cell: 'embedding',
}


# Verdicts that warrant a retry (could be a transient blip), versus ones
# that are definitive on the first attempt (no point re-asking).
_PROBE_TRANSIENT = {'rate_limited', 'unavailable', 'error'}
_PROBE_DEFINITIVE = {
    'unauthorized', 'not_found', 'bad_request', 'invalid_response',
}


def probe_cell_multi(base_url, api_key, model_id, extra_headers, timeout,
                     attempts=3, retry_delay=0.8, protocol='openai',
                     probe_fn=None, oauth='', adapter=None):
    """Probe a cell up to ``attempts`` times to filter out FALSE 429s.

    ``probe_fn`` defaults to :func:`probe_one_cell` (chat surface); the
    modality probes share its signature so the same multi-attempt policy
    drives them unchanged. ``oauth`` is forwarded to the chat surface only —
    modality probes keep the 6-arg signature.
    """
    attempts = max(1, int(attempts))
    fn = probe_fn or probe_one_cell
    last_status, last_detail = 'error', ''
    for i in range(attempts):
        # Call through the module global so tests can patch probe_one_cell.
        if probe_fn is None:
            status, detail = fn(base_url, api_key, model_id, extra_headers,
                                timeout, protocol, oauth=oauth,
                                **({'adapter': adapter} if adapter else {}))
        else:
            status, detail = fn(base_url, api_key, model_id, extra_headers,
                                timeout, protocol)
        if status == 'ok':
            note = '' if i == 0 else ' (ok on attempt %d/%d)' % (i + 1, attempts)
            return 'ok', detail + note
        # A session verdict is final — retrying can't conjure a login.
        if status in _PROBE_DEFINITIVE or status in ('not_logged_in', SKIPPED):
            return status, detail
        last_status, last_detail = status, detail
        if i < attempts - 1:
            _time.sleep(retry_delay)
    suffix = ' (%d/%d attempts failed)' % (attempts, attempts) if attempts > 1 else ''
    return last_status, '%.120s%s' % (last_detail, suffix)


# ══════════════════════════════════════════════════════
#  Background task state
# ══════════════════════════════════════════════════════
CELL_PROBE_TASKS: dict = {}
CELL_PROBE_LOCK = threading.Lock()
_PROBE_DISABLE_STATUSES = {
    'rate_limited', 'unauthorized', 'not_found', 'unavailable',
    'bad_request', 'invalid_response', 'error',
}

_PROOF_BY_SURFACE = {
    'chat': 'generated_text',
    'image': 'image_payload',
    'transcription': 'transcript_payload',
    'tts': 'audio_bytes',
    'embedding': 'embedding_vector',
}


def _http_status_from_detail(detail) -> int | None:
    match = _HTTP_STATUS_RE.search(str(detail or ''))
    return int(match.group(1)) if match else None


def normalize_probe_snapshot(snapshot: dict) -> dict:
    """Upgrade old probe snapshots to the strict v2 public contract.

    The migration is deliberately read-time and non-destructive: persisted
    files can be shared with an older process during a rolling deployment.
    Legacy ``ok + HTTP 400`` cells become ``bad_request`` immediately; legacy
    chat ``HTTP 200`` cells become neutral ``unverified`` until re-tested,
    because version 1 never inspected their response payload. Existing
    non-chat HTTP 200 cells did have modality shape validation, so their proof
    can be inferred safely from ``probe_surface``.
    """
    if not isinstance(snapshot, dict):
        return snapshot
    try:
        version = int(snapshot.get('probe_schema_version') or 1)
    except (TypeError, ValueError) as exc:
        logger.debug('[CellProbe] invalid probe schema version: %s', exc)
        version = 1
    out = dict(snapshot)
    cells_out = {}
    raw_cells = snapshot.get('cells')
    if not isinstance(raw_cells, dict):
        raw_cells = {}
    for key, original in raw_cells.items():
        if not isinstance(original, dict):
            continue
        cell = dict(original)
        cell['detail'] = _redact_probe_text(cell.get('detail'))
        status = str(cell.get('status') or '')
        http_status = cell.get('http_status')
        if not isinstance(http_status, int):
            http_status = _http_status_from_detail(cell.get('detail'))
        if http_status is not None:
            cell['http_status'] = http_status

        if status == 'ok' and http_status is not None and http_status >= 400:
            status = ('bad_request' if http_status in (400, 409, 415, 422)
                      else 'error')
            cell['status'] = status
            cell['recommend_disable'] = True
            cell.pop('proof', None)
            cell['detail'] = ('%s — legacy false-positive corrected; provider '
                              'rejected the probe request' %
                              (cell.get('detail') or 'HTTP %d' % http_status))
        elif status == 'ok' and not cell.get('proof'):
            surface = str(cell.get('probe_surface') or 'chat')
            if version < PROBE_SCHEMA_VERSION and surface in ('chat', ''):
                cell['status'] = 'unverified'
                cell['recommend_disable'] = False
                cell['detail'] = ('%s — legacy result did not validate generated '
                                  'content; re-test required' %
                                  (cell.get('detail') or 'HTTP 2xx'))
            else:
                proof = _PROOF_BY_SURFACE.get(surface)
                if proof:
                    cell['proof'] = proof
        cells_out[key] = cell

    out['cells'] = cells_out
    out['probe_schema_version'] = PROBE_SCHEMA_VERSION
    # Summaries are a view over cells, never trusted from an older schema.
    n_ok = sum(1 for c in cells_out.values() if c.get('status') == 'ok')
    n_disable = sum(1 for c in cells_out.values()
                    if c.get('recommend_disable') is True)
    n_skipped = sum(1 for c in cells_out.values()
                    if c.get('status') == SKIPPED)
    n_neutral = sum(1 for c in cells_out.values()
                    if c.get('status') in ('not_logged_in', 'unverified'))
    out['summary'] = {
        'ok': n_ok,
        'disable': n_disable,
        'skipped': n_skipped,
        'neutral': n_neutral,
        'failed': max(0, len(cells_out) - n_ok - n_skipped - n_neutral),
    }
    return out


def build_probe_work(provider: dict, models: list, api_keys: list) -> list:
    """Build the probe work list: one item per (key × wire id) SLOT —
    exactly the pairs the dispatcher can route.

    Each item is ``(key_idx, api_key, root_id, wire_id, caps, base_url,
    protocol)``. ``root_id`` is the LOGICAL model id (row grouping +
    recommend-disable attribution); ``wire_id`` is what actually goes on
    the wire for THAT key, resolved per key through the dispatcher's own
    contract (:func:`lib.llm_dispatch.model_entry.resolve_request_ids`)
    — never re-derived here, so the probe can never test a different id
    set than the dispatcher sends:

      * an explicit ``request_ids`` pool wins (a ``key_access`` cell
        REPLACES the pool for its key), so the logical ``model_id`` of an
        explicit-pool entry — a preset-facing identity no real request
        carries — is never probed;
      * ``disabled_ids`` is popped before resolving: a disabled id still
        routes the moment the user re-enables it, so it keeps its verdict
        (the matrix shows the pip on its toggleable row).

    The last two tuple items are resolved PER MODEL via
    :func:`lib.llm_dispatch.provider_face.resolve_face`, because one account
    can expose several wire faces — probing a Claude cell on the OpenAI face
    of a dual-face gateway returns a false ``not_found`` and the matrix then
    recommends disabling a model that works.

    THE SEAM: the route and the tests both call this, so a regression in the
    resolution is visible to a test rather than hiding behind a re-implemented
    copy of the loop.

    A REFUSED entry (Claude with no anthropic face on a dual-face gateway)
    still gets probed on the provider default: the matrix reports
    reachability, and the refusal itself is surfaced by the dispatcher.
    """
    from lib.llm_dispatch.model_entry import resolve_request_ids
    from lib.llm_dispatch.provider_face import resolve_face

    base_url = provider.get('base_url') or ''
    protocol = (provider.get('protocol') or 'openai') or 'openai'
    work = []
    for key_idx, api_key in enumerate(api_keys or []):
        for m in (models or []):
            root = (m.get('model_id') or '').strip()
            if not root:
                continue
            caps = m.get('capabilities') or []
            face = resolve_face(provider, m)
            cell_url = face.base_url if (face.ok and face.base_url) else base_url
            cell_proto = (face.protocol if face.ok else protocol) or 'openai'
            cell = (m.get('key_access') or {}).get(str(key_idx))
            probe_cell = dict(cell) if isinstance(cell, dict) else None
            if probe_cell:
                probe_cell.pop('disabled_ids', None)
            for mid in resolve_request_ids(m, probe_cell):
                work.append((key_idx, api_key, root, mid, caps,
                             cell_url, cell_proto))
    return work


def probe_cache_path(provider_id: str) -> str:
    """Disk path for a provider's persisted probe snapshot."""
    safe = hashlib.sha1((provider_id or '').encode('utf-8')).hexdigest()[:16]
    return _config_path('probe_cache', '%s.json' % safe)


def probe_cell_key(key_idx, model_id) -> str:
    return '%s::%s' % (key_idx, model_id)


def persist_probe_task(task: dict):
    """Atomically write a public (key-free) snapshot of the task to disk."""
    try:
        write_json_atomic(probe_cache_path(task['provider_id']),
                          public_probe_snapshot(task), fsync=False)
    except Exception as e:
        logger.warning('[CellProbe] persist failed for %s: %s',
                       task.get('provider_id'), e)


def _recount_summary(task: dict):
    """Recompute task['summary'] over ALL current cells.

    Scoped probes (only=key/model) seed the task with the persisted snapshot's
    other cells so their result MERGES into the full grid; the summary must
    therefore reflect the merged set from the very first poll, not just the
    cells completed in this run."""
    cells = task['cells']
    n_ok = sum(1 for c in cells.values() if c.get('status') == 'ok')
    n_disable = sum(1 for c in cells.values()
                    if c.get('recommend_disable') is True)
    n_skipped = sum(1 for c in cells.values()
                    if c.get('status') == SKIPPED)
    n_neutral = sum(1 for c in cells.values()
                    if c.get('status') in ('not_logged_in', 'unverified'))
    task['summary'] = {
        'ok': n_ok,
        'disable': n_disable,
        'skipped': n_skipped,
        'neutral': n_neutral,
        'failed': max(0, len(cells) - n_ok - n_skipped - n_neutral),
    }


def public_probe_snapshot(task: dict) -> dict:
    """The serialisable, secret-free view of a probe task (for poll + disk)."""
    return normalize_probe_snapshot({
        'probe_schema_version': PROBE_SCHEMA_VERSION,
        'provider_id': task['provider_id'],
        'status': task['status'],
        'started_at': task['started_at'],
        'finished_at': task['finished_at'],
        'total': task['total'],
        'done_count': task['done_count'],
        'attempts': task.get('attempts', 1),
        'cells': task['cells'],
        'summary': task['summary'],
        'error': task['error'],
    })


def run_cell_probe_task(task: dict, work: list, timeout: int):
    """Background worker: fan out cell probes, updating + persisting progress."""
    provider_id = task['provider_id']
    base_url = task['_base_url']
    extra_headers = task['_extra_headers']
    protocol = task.get('_protocol', 'openai')
    oauth = task.get('_oauth', '')
    adapter = task.get('_adapter') or {}
    if not adapter:
        # Fallback: derive the marker from the stored provider card (the
        # probe route predates the marker and only threads '_oauth').
        try:
            from lib import _load_server_config
            from lib.desktop.adapter import is_adapter_provider
            for _p in (_load_server_config().get('providers') or []):
                if _p.get('id') == provider_id:
                    adapter = is_adapter_provider(_p)
                    break
        except Exception as _ae:
            logger.debug('[CellProbe] adapter marker lookup failed: %s', _ae)
            adapter = {}
    attempts = task.get('attempts', 3)
    if task['cells']:
        _recount_summary(task)
    logger.info('[CellProbe] Started background probe for %s — %d cell(s) this run, '
                '%d seeded, up to %d attempt(s) each (protocol=%s)', provider_id,
                len(work), len(task['cells']), attempts, protocol)

    def _run(item):
        key_idx, api_key, root, mid = item[0], item[1], item[2], item[3]
        caps = item[4] if len(item) > 4 else None
        # Per-cell wire face (account/face separation). A work tuple may carry
        # its own (base_url, protocol) because ONE account can expose several
        # wire faces — e.g. the Meituan gateway serves Claude over
        # /v1/anthropic and everything else over /v1/openai/native with the
        # same keys. Probing a cell on the wrong face returns a false
        # 'not_found'. Older 5-tuples fall back to the task-level values.
        cell_base_url = item[5] if len(item) > 5 and item[5] else base_url
        cell_protocol = item[6] if len(item) > 6 and item[6] else protocol
        cell_attempts = attempts
        cell_timeout = timeout
        probe_fn = None
        surface = 'chat'
        if caps and not _is_chat_model(caps):
            probe_fn = nonchat_probe_fn(caps)
            if probe_fn is None:
                return {
                    'key_idx': key_idx,
                    'model_id': mid,
                    'root_model_id': root,
                    'status': SKIPPED,
                    'detail': 'non-chat model (%s) — no probe surface implemented'
                              % ','.join(caps),
                    'recommend_disable': False,
                    'probe_surface': 'none',
                    'http_status': None,
                    'proof': None,
                }
            surface = _PROBE_SURFACE_NAMES[probe_fn]
            if 'image_gen' in caps:
                # One real image is generated per attempt — a billed call, so
                # don't multiply it for transient-filtering; generation also
                # routinely outlives the chat-probe timeout.
                cell_attempts = 1
                cell_timeout = max(timeout, _IMAGE_PROBE_MIN_TIMEOUT)
        # Multi-attempt so a FALSE 429 / transient 5xx doesn't wrongly flag a
        # reachable cell. A single ok on any attempt wins.
        status, detail = probe_cell_multi(cell_base_url, api_key, mid,
                                          extra_headers,
                                          cell_timeout, attempts=cell_attempts,
                                          protocol=cell_protocol, probe_fn=probe_fn,
                                          oauth=oauth,
                                          **({'adapter': adapter} if adapter else {}))
        http_status = _http_status_from_detail(detail)
        return {
            'key_idx': key_idx,
            'model_id': mid,
            'root_model_id': root,
            'status': status,
            'detail': detail,
            'recommend_disable': status in _PROBE_DISABLE_STATUSES,
            'probe_surface': surface,
            'http_status': http_status,
            'proof': (_PROOF_BY_SURFACE.get(surface)
                      if status == 'ok' else None),
        }

    last_persist = 0.0
    try:
        workers = min(8, len(work))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='cell-probe') as pool:
            futures = [pool.submit(_run, it) for it in work]
            for fut in as_completed(futures):
                if task.get('_abort'):
                    logger.info('[CellProbe] %s aborted', provider_id)
                    break
                try:
                    cell = fut.result()
                except Exception as e:
                    logger.error('[CellProbe] cell task raised: %s', e, exc_info=True)
                    continue
                with CELL_PROBE_LOCK:
                    task['cells'][probe_cell_key(cell['key_idx'], cell['model_id'])] = cell
                    # Progress counts only THIS RUN's completions — cells
                    # seeded from the disk snapshot (scoped probe) are already
                    # done and must not inflate done/total.
                    task['done_count'] = task.get('done_count', 0) + 1
                    _recount_summary(task)
                # Throttle disk writes: at most every ~1.5s during the run.
                now = _time.monotonic()
                if now - last_persist > 1.5:
                    last_persist = now
                    persist_probe_task(task)
        with CELL_PROBE_LOCK:
            task['status'] = 'done'
            task['finished_at'] = _time.time()
        persist_probe_task(task)
        logger.info('[CellProbe] %s done: %d cells, %d flagged',
                    provider_id, task['done_count'], task['summary']['disable'])
    except Exception as e:
        logger.error('[CellProbe] background worker crashed for %s: %s',
                     provider_id, e, exc_info=True)
        with CELL_PROBE_LOCK:
            task['status'] = 'error'
            task['error'] = str(e)[:300]
            task['finished_at'] = _time.time()
        persist_probe_task(task)


__all__ = [
    'probe_one_cell', 'probe_cell_multi', 'run_cell_probe_task',
    'probe_embedding_cell', 'probe_transcription_cell', 'probe_image_cell',
    'probe_tts_cell', 'nonchat_probe_fn',
    'probe_cache_path', 'probe_cell_key', 'persist_probe_task',
    'build_probe_work',
    'public_probe_snapshot', 'normalize_probe_snapshot',
    'CELL_PROBE_TASKS', 'CELL_PROBE_LOCK', 'SKIPPED',
    'PROBE_SCHEMA_VERSION',
]
