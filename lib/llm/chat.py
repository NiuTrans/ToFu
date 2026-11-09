# HOT_PATH
"""Complete-text chat completion (JSON or collected subscription SSE).

Public API:
  - chat(messages, model=None, ...) → (content_text, usage_dict)

The package imports this definition eagerly to keep its same-name export stable.
Provider and HTTP dependencies load at invocation. The local http_post entry
preserves the transport fault-injection seam while deferring the HTTP import.
"""

import json
import re
import uuid

import lib as _lib
from lib.log import get_logger
from lib.llm.diagnostic_timing import current_timing, timed_call, timed_stage, timing_stage
from lib.llm.nonstream_budget import budgeted_nonstream_chat

logger = get_logger(__name__)


def http_post(*args, **kwargs):
    """Delegate lazily to the shared HTTP owner, retaining transport injection."""
    from lib.http_transport.client import http_post as send
    return send(*args, **kwargs)


def _post_admitted_request(url, headers, body, timeout, oauth):
    """Send the sealed body through the shared subscription-aware transport."""
    from lib.llm.request import transport_body_arguments
    return http_post(url, headers=headers,
                     **transport_body_arguments(body, transport='requests'),
                     timeout=timeout, network_scope='subscription' if oauth else None)


def _attempt_headers(*, api_protocol, api_key, extra_headers, oauth, base_url,
                     inference_profile, cache_affinity_key, trace_id, body):
    """Headers for one physical attempt: protocol credentials, caller extras,
    the prompt-cache affinity carrier, the trace id and the Responses
    multi-agent beta marker."""
    from lib.llm._transport import headers
    from lib.llm.cache_affinity import merge_cache_affinity_headers
    if api_protocol == 'anthropic':
        from lib.llm.anthropic_outbound import anthropic_headers
        from lib.llm.catpaw_gateway import catpaw_headers, is_catpaw_base_url
        hdrs = anthropic_headers(api_key, extra_headers)
        if oauth == 'claude':
            hdrs.pop('Authorization', None)
        elif is_catpaw_base_url(base_url):
            hdrs.update(catpaw_headers())
    else:
        hdrs = headers()
        if api_key:
            hdrs['Authorization'] = f'Bearer {api_key}'
        if extra_headers:
            hdrs.update(extra_headers)
    merge_cache_affinity_headers(
        hdrs, inference_profile, protocol=api_protocol, oauth=oauth,
        key=cache_affinity_key)
    hdrs['M-TraceId'] = trace_id
    if (api_protocol == 'responses' and isinstance(body.get('multi_agent'), dict)
            and body['multi_agent'].get('enabled')):
        beta_key = next((key for key in hdrs
                         if key.lower() == 'openai-beta'),
                        'OpenAI-Beta')
        beta = str(hdrs.get(beta_key) or '')
        marker = 'responses_multi_agent=v1'
        if marker not in beta:
            hdrs[beta_key] = f'{beta},{marker}' if beta else marker
    return hdrs


@budgeted_nonstream_chat
@timed_stage('chat_total')
def chat(messages, model=None, *, max_tokens=4096, temperature=0,
         thinking_enabled=None, preset='low', effort=None, extra=None,
         thinking_depth=None,
         timeout=None, log_prefix='', api_key=None, base_url=None,
         extra_headers=None, max_retries=None, _limit_retry=False,
         thinking_format='', provider_id='', api_protocol='openai', oauth='',
         adapter=None, responses_feature_profile='', owner_user_id=None,
         inference_profile=None, context_window=0, model_budget_admit=None, _budget_scope=None,
         abort_check=None, _repair_state=None, knowledge_model='',
         route_output_limit_key=''):
    """Complete-text chat completion, collecting stream-only subscriptions.

    Args:
        model:        request spelling written to ``body['model']`` (the
            Deployment's wire id under dispatch).
        knowledge_model: logical Model id for model-knowledge lookups in
            ``build_body`` (clamps, vision, family tweaks, reasoning) and for
            request settlement pricing; defaults to ``model``.
        route_output_limit_key: Deployment route identity that keys learned
            request omissions; dispatch passes the key the stream adapter
            stamps, so both paths share facts. '' falls back to provider +
            wire model.
        api_key:      optional API key override (from dispatch slot).
        base_url:     optional base URL override.
        extra_headers: optional dict of additional headers.
        max_retries:  override retry count (default: MAX_STREAM_RETRIES).
        timeout:      READ timeout in seconds. ``None`` (the default) means
            no read timeout — a slow completion is waited out rather than
            truncated. The connect phase stays bounded by CONNECT_TIMEOUT
            so a dead host still fails over. An explicit budget makes its
            expiry the caller's outcome: exhaustion by read timeout logs
            WARNING without a traceback; other exhaustion stays ERROR.

    Returns:
        (content_text: str, usage_dict: dict)

    Raises:
        RateLimitError, PermissionError_, ContentFilterError,
        RetryableAPIError, PromptTooLongError, Exception
    """
    # A complete-text API need not use a nonstream wire request. Subscription
    # Responses endpoints require streaming; retain the selected route and let
    # the existing stream owner verify its terminal evidence before returning.
    if api_protocol == 'responses' and oauth == 'codex':
        from lib.llm.chat_stream import complete_subscription_stream
        return complete_subscription_stream(
            messages, model=model or _lib.LLM_MODEL,
            max_tokens=max_tokens, temperature=temperature,
            thinking_enabled=thinking_enabled, thinking_depth=thinking_depth,
            preset=preset, effort=effort, extra=extra,
            thinking_format=thinking_format, inference_profile=inference_profile,
            context_window=context_window, knowledge_model=knowledge_model,
            provider_id=provider_id, route_output_limit_key=route_output_limit_key,
            responses_feature_profile=responses_feature_profile,
            model_budget_admit=model_budget_admit,
            api_key=api_key, base_url=base_url, extra_headers=extra_headers,
            api_protocol=api_protocol, oauth=oauth, adapter=adapter,
            owner_user_id=owner_user_id, abort_check=abort_check,
            timeout=timeout, max_retries=max_retries, log_prefix=log_prefix)

    import requests

    # A repaired retry re-enters with the caller's own inputs; OAuth below
    # rebinds these names to resolved credentials and identity messages.
    _call_messages, _call_api_key, _call_extra_headers = (
        messages, api_key, extra_headers)

    from lib.llm._transport import (
        CONNECT_TIMEOUT,
        MAX_STREAM_RETRIES,
        abortable_sleep,
        chat_url,
        retry_wait,
        transport_owner_scope,
    )
    from lib.llm.body import build_body
    from lib.llm.cache import add_cache_breakpoints
    from lib.llm.llm_errors import (
        _ERR_BODY_LIMIT,
        ContentFilterError,
        EndpointUnreachableError,
        InvalidImageError,
        PermissionError_,
        PromptTooLongError,
        RateLimitError,
        RequestTransportError,
        REQUEST_REPAIR_ERRORS,
        ResponseReadTimeoutError,
        StreamOnlyError,
        SubscriptionConnectionError,
        _RETRYABLE,
        _classify_http_error,
        _has_outbound_credential,
        decode_error_body,
        provider_error_http_status,
    )
    from lib.cost import canonicalize_usage_cache_keys
    from lib.model_info import (
        _learn_model_limit,
        _parse_token_limit_from_error,
        is_claude,
    )
    from lib.subscription_quota import USAGE_QUOTA_KEY, record_codex_quota
    from lib.llm.request import request_preparation
    with request_preparation():
        from lib.llm.request import require_wire_protocol
        api_protocol = require_wire_protocol(api_protocol)
        model = model or _lib.LLM_MODEL
        _owner_scope = transport_owner_scope(owner_user_id)
        _anthropic = (api_protocol == 'anthropic')
        _responses = (api_protocol == 'responses')
        if _responses:
            from lib.llm.responses_outbound import responses_url
            url = responses_url(base_url)
        elif _anthropic:
            from lib.llm.anthropic_outbound import anthropic_messages_url
            url = anthropic_messages_url(base_url)
            if oauth == 'claude':
                from lib.oauth.outbound import claude_oauth_url
                url = claude_oauth_url(url)
        else:
            url = f'{base_url.rstrip("/")}/chat/completions' if base_url else chat_url()

        # Subscription-OAuth slot: swap in a live token + client-identity headers
        # (+ Claude identity system block) before the body is built/translated.
        if oauth:
            from lib.oauth.outbound import resolve_oauth_request
            _oauth_body_seed = {'messages': messages}
            api_key, extra_headers, _oauth_body_seed = resolve_oauth_request(
                oauth, _oauth_body_seed, extra_headers, user_id=_owner_scope)
            messages = _oauth_body_seed['messages']

        body = timed_call('request_build', build_body,
            model, messages,
            max_tokens=max_tokens,
            temperature=temperature,
            thinking_enabled=thinking_enabled,
            thinking_depth=thinking_depth,
            preset=preset, effort=effort,
            stream=False,
            # Explicit effort is a depth, not the legacy preset named 'low'.
            extra=extra,
            thinking_format=thinking_format,
            inference_profile=inference_profile,
            provider_id=provider_id, context_window=context_window,
            knowledge_model=knowledge_model,
        )
        if _responses:
            body['_responses_feature_profile'] = (
                responses_feature_profile or 'compatible')
        if route_output_limit_key:
            body['_route_output_limit_key'] = route_output_limit_key

        # The sync transport must share the exact final tool-schema preflight used
        # by sync/async streaming. Otherwise background probes, compaction, and
        # other non-stream callers can still send a Kimi-invalid catalog even when
        # ordinary turns are repaired.
        from lib.tools.gateway import preflight_wire_tool_body
        preflight_wire_tool_body(body, log_prefix=log_prefix)

        # Learned restrict-only facts shape the canonical body before protocol
        # translation, exactly like the streaming pre-send hook.
        from lib.llm.request_repair import (
            RequestRepairState, apply_learned_request_facts,
            mark_status_line_refusal, repair_rejected_request)
        if _repair_state is None:
            _repair_state = RequestRepairState()
        apply_learned_request_facts(
            body, provider_id=provider_id, repair_state=_repair_state)
        _canonical_body = body
        _repair_reason = ''

        # Cache breakpoints + extended-TTL beta header
        _task_id_for_latch = body.get('_task_id', '')
        from lib.llm.cache_affinity import cache_affinity_key
        _cache_affinity_key = cache_affinity_key(body)
        add_cache_breakpoints(body, log_prefix, api_protocol=api_protocol)
        body.pop('_task_id', None)

        if is_claude(body.get('model', '')):
            if _task_id_for_latch:
                from lib.tasks_pkg.cache_tracking._ttl import latch_extended_ttl
                _use_ext_ttl = latch_extended_ttl(_task_id_for_latch)
            else:
                _use_ext_ttl = getattr(_lib, 'CACHE_EXTENDED_TTL', False)
            if _use_ext_ttl:
                if extra_headers is None:
                    extra_headers = {}
                else:
                    extra_headers = dict(extra_headers)
                _existing_beta = extra_headers.get('anthropic-beta', '')
                _ttl_beta = 'extended-cache-ttl-2025-04-11'
                if _ttl_beta not in _existing_beta:
                    if _existing_beta:
                        extra_headers['anthropic-beta'] = f'{_existing_beta},{_ttl_beta}'
                    else:
                        extra_headers['anthropic-beta'] = _ttl_beta

        from lib.llm.body._admission import enforce_body_input_limit
        from lib.llm.request import (
            detach_input, semantic_request_id, seal_request)
        detach_input(body)
        # Its encode also proves the detached input is valid JSON before counting.
        _semantic_id = semantic_request_id(body)
        _input_tokens = timed_call('input_admission', enforce_body_input_limit,
            body, model=model, provider_id=provider_id, context_window=context_window)

        _reasoning_resolution = dict(body.get('_reasoning_resolution') or {})
        _cloak_reverse = None
        _resp_reverse, _resp_replay = None, False
        if _responses:
            from lib.llm.responses_outbound import openai_body_to_responses, responses_reasoning_replay
            _resp_profile = 'codex' if oauth == 'codex' else 'default'
            _resp_replay = responses_reasoning_replay(body, profile=_resp_profile)
            body, _resp_reverse = openai_body_to_responses(body, profile=_resp_profile, stream=False)
        elif _anthropic:
            from lib.llm.anthropic_outbound import openai_body_to_anthropic
            from lib.llm.catpaw_gateway import apply_catpaw_shape, is_catpaw_base_url
            body = openai_body_to_anthropic(body)
            if oauth == 'claude':
                from lib.oauth.outbound import apply_claude_cloak
                body, _cloak_reverse = apply_claude_cloak(body)
            elif is_catpaw_base_url(base_url):
                body = apply_catpaw_shape(body)

        else:
            from lib.llm.openai_outbound import openai_body_to_chat
            body = openai_body_to_chat(body)

        if _anthropic:
            from lib.llm.request import recount_anthropic_input
            _input_tokens = recount_anthropic_input(
                body, model=model, provider_id=provider_id)

        body = timed_call('request_seal', seal_request,
            body, model=model, provider_id=provider_id, protocol=api_protocol,
            endpoint=url, input_tokens=_input_tokens, semantic_id=_semantic_id,
            logical_model=knowledge_model or model)

    if log_prefix:
        logger.debug('%s POST %s model=%s msgs=%d', log_prefix, url, model, len(messages))

    # Desktop-egress routing (S3): whitelisted hosts go through the user's
    # desktop agent when the server's own egress is blocked (cached probe).
    # SKIPPED for adapter providers (E4): the marker pins the request to the
    # bridge loopback relay — the server can never reach agent loopback
    # directly, so there is no route to probe.
    _adapter = adapter if isinstance(adapter, dict) and adapter else None
    _egress_route = None
    retries = MAX_STREAM_RETRIES if max_retries is None else max_retries
    resp = None
    resp_trace = ''
    trace_id = ''
    def _budget_send(send, *args, **kwargs):
        with timing_stage('provider_send', model=model, provider_id=provider_id,
                protocol=api_protocol, trace_id=trace_id,
                transport='adapter' if _adapter else 'desktop_egress'
                    if _egress_route and _egress_route != 'direct' else 'requests'):
            if _budget_scope is not None:
                return _budget_scope.send(body, send, *args, **kwargs)
            return send(*args, **kwargs)

    for attempt in range(1 + retries):
        # Every physical retry re-enters shared route admission. In particular,
        # a failed proxy must recover before another request is sent.
        if not _adapter:
            from lib.desktop import egress as _eg
            try:
                from lib.llm.egress_recovery import resolve_egress_route
                _egress_route = timed_call('egress_route', resolve_egress_route,
                    _eg, url, user_id=_owner_scope, abort_check=abort_check,
                    timeout_s=timeout)
            except _eg.EgressUnavailable as e:
                raise EndpointUnreachableError(str(e), base_url=url) from e
        if _budget_scope is not None:
            body = _budget_scope.prepare(body)
        try:
            trace_id = uuid.uuid4().hex
            hdrs = _attempt_headers(
                api_protocol=api_protocol, api_key=api_key,
                extra_headers=extra_headers, oauth=oauth, base_url=base_url,
                inference_profile=inference_profile,
                cache_affinity_key=_cache_affinity_key, trace_id=trace_id,
                body=body)
            if log_prefix:
                logger.debug('%s M-TraceId=%s', log_prefix, trace_id)
            try:
                if _adapter:
                    from urllib.parse import urlparse as _urlparse
                    from lib.desktop import adapter as _ad
                    from lib.desktop.egress import EgressUnavailable as _EU
                    _pu = _urlparse(url)
                    _relay_path = _pu.path + (
                        ('?' + _pu.query) if _pu.query else '')
                    try:
                        resp = _budget_send(_ad.relay_http,
                            _adapter.get('agent_id', ''),
                            int(_adapter.get('port') or 0),
                            _relay_path, method='POST', headers=hdrs,
                            body=body.payload,
                            timeout=min(timeout or 60, 60),
                            user_id=_owner_scope)
                    except _EU as e:
                        raise EndpointUnreachableError(
                            str(e), base_url=url) from e
                elif _egress_route and _egress_route != 'direct':
                    try:
                        resp = _budget_send(_eg.egress_http,
                            url, method='POST', headers=hdrs,
                            body=body.payload,
                            timeout=min(timeout or 60, 60),
                            user_id=_owner_scope,
                            agent_id=_egress_route)
                    except _eg.EgressUnavailable as e:
                        raise EndpointUnreachableError(
                            str(e), base_url=url) from e
                else:
                    resp = _budget_send(_post_admitted_request, url, hdrs, body,
                                        (CONNECT_TIMEOUT, timeout), oauth)
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                    RequestTransportError) as ce:
                from lib.llm._transport import classify_request_transport_error
                error = classify_request_transport_error(
                    ce, base_url=url, write_timeout=CONNECT_TIMEOUT,
                    read_timeout=timeout)
                logger.warning('%s Transport failed during %s: %s',
                               log_prefix, getattr(error, 'failure_stage', 'connect'), error)
                if error is ce:
                    raise
                raise error from ce
            resp_trace = resp.headers.get('M-TraceId', '')
            if resp_trace and resp_trace != trace_id:
                logger.debug('%s resp M-TraceId=%s', log_prefix, resp_trace)
            if resp.status_code != 200:
                err_msg = f'API HTTP {resp.status_code}: {decode_error_body(resp)[:_ERR_BODY_LIMIT]}'
                # Clamp and repair recursions share the stream loop's
                # same-request restart bound.
                _restarts_used = _repair_state.repairs + int(_limit_retry)
                if (resp.status_code == 400 and not _limit_retry
                        and _restarts_used < MAX_STREAM_RETRIES):
                    _detected_limit = _parse_token_limit_from_error(err_msg, model)
                    if _detected_limit:
                        _learn_model_limit(model, _detected_limit)
                        logger.warning('%s ⚙️ max_tokens %d exceeds %s limit %d — '
                                      'auto-learned and retrying with corrected value',
                                      log_prefix, max_tokens, model, _detected_limit)
                        if _budget_scope is not None:
                            # The status line proves no prompt ran; an unknown
                            # settlement would fence this retry under a finite budget.
                            _budget_scope.settle(refused=True)
                        content_r, usage_r = chat(
                            messages, model, max_tokens=_detected_limit,
                            temperature=temperature,
                            thinking_enabled=thinking_enabled,
                            thinking_depth=thinking_depth,
                            preset=preset, effort=effort, extra=extra,
                            timeout=timeout, log_prefix=log_prefix,
                            api_key=api_key, base_url=base_url,
                            extra_headers=extra_headers,
                            max_retries=max_retries, _limit_retry=True,
                            thinking_format=thinking_format,
                            inference_profile=inference_profile,
                            context_window=context_window,
                            provider_id=provider_id, api_protocol=api_protocol,
                            oauth=oauth, adapter=adapter,
                            responses_feature_profile=(
                                responses_feature_profile or 'compatible'),
                            owner_user_id=owner_user_id, model_budget_admit=model_budget_admit,
                            abort_check=abort_check, _repair_state=_repair_state,
                            knowledge_model=knowledge_model,
                            route_output_limit_key=route_output_limit_key)
                        usage_r['_model_limit_learned'] = {
                            'model': model,
                            'old_limit': max_tokens,
                            'new_limit': _detected_limit,
                        }
                        return content_r, usage_r
                try:
                    _classify_http_error(resp.status_code, err_msg, model,
                                         log_prefix, max_tokens=max_tokens,
                                         credential_present=(
                                             _has_outbound_credential(hdrs)))
                except REQUEST_REPAIR_ERRORS as error:
                    mark_status_line_refusal(error)
                    # The repaired retry runs after this loop so its own
                    # failures never re-enter this loop's transport handlers.
                    _repair_reason = repair_rejected_request(
                        _canonical_body, error, _repair_state,
                        provider_id=provider_id, log_prefix=log_prefix)
                    if not _repair_reason or _restarts_used >= MAX_STREAM_RETRIES:
                        raise
                except BaseException as error:
                    raise mark_status_line_refusal(error)
            break
        except (RateLimitError, PermissionError_, ContentFilterError, PromptTooLongError, StreamOnlyError, InvalidImageError, EndpointUnreachableError):
            raise
        except _RETRYABLE as e:
            if _budget_scope is not None:
                from lib.llm.llm_errors import provider_refused_request
                _budget_scope.settle(getattr(e, 'usage', None),
                                     refused=provider_refused_request(e))
            if attempt < retries:
                wait = (0.0 if isinstance(e, SubscriptionConnectionError)
                        else retry_wait(attempt))
                logger.warning('%s ⚠ Attempt %d/%d failed '
                      '(%s), retrying in %.1fs…', log_prefix, attempt + 1, 1 + retries, type(e).__name__, wait, exc_info=True)
                timed_call('transport_backoff', abortable_sleep, wait, abort_check)
            elif timeout is not None and isinstance(e, ResponseReadTimeoutError):
                logger.warning('%s ✖ Caller read budget of %gs elapsed on all %d attempts '
                               '(non-stream); surfacing to caller.', log_prefix, timeout, 1 + retries)
                raise
            else:
                logger.error('%s ✖ All %d attempts failed (non-stream).', log_prefix, 1 + retries, exc_info=True)
                raise

    assert resp is not None, 'BUG: retry loop exited without assigning resp'

    if _repair_reason:
        if _budget_scope is not None:
            _budget_scope.settle(refused=True)
        return chat(
            _call_messages, model, max_tokens=max_tokens,
            temperature=temperature, thinking_enabled=thinking_enabled,
            thinking_depth=thinking_depth, preset=preset, effort=effort,
            extra=extra, timeout=timeout, log_prefix=log_prefix,
            api_key=_call_api_key, base_url=base_url,
            extra_headers=_call_extra_headers, max_retries=max_retries,
            _limit_retry=_limit_retry, thinking_format=thinking_format,
            inference_profile=inference_profile,
            context_window=context_window, provider_id=provider_id,
            api_protocol=api_protocol, oauth=oauth, adapter=adapter,
            responses_feature_profile=responses_feature_profile,
            owner_user_id=owner_user_id, model_budget_admit=model_budget_admit,
            abort_check=abort_check, _repair_state=_repair_state,
            knowledge_model=knowledge_model,
            route_output_limit_key=route_output_limit_key)

    try:
        data = timed_call('response_parse', resp.json)
    except (ValueError, json.JSONDecodeError) as e:
        raise Exception(
            f'API returned invalid JSON (HTTP {resp.status_code}): '
            f'{decode_error_body(resp)[:_ERR_BODY_LIMIT]}'
        ) from e
    if not isinstance(data, dict):
        raise Exception(
            'API returned invalid response shape: top-level JSON must be an object')
    if _responses:
        from lib.llm.responses_outbound import responses_response_to_openai
        data = timed_call('response_normalize', responses_response_to_openai,
            data, tool_name_reverse=_resp_reverse, reasoning_replay=_resp_replay)
    elif _anthropic:
        from lib.llm.anthropic_outbound import anthropic_response_to_openai
        data = timed_call('response_normalize', anthropic_response_to_openai, data)
        if _cloak_reverse:
            from lib.oauth.outbound import restore_claude_tool_names
            for _ch in data.get('choices') or []:
                restore_claude_tool_names(
                    (_ch.get('message') or {}).get('tool_calls'), _cloak_reverse)
    if data.get('error') is not None:
        # Protocol converters use the same error envelope as HTTP failures.
        # Do not manufacture an empty assistant turn from malformed provider
        # JSON, and do not assume the provider's error field is well shaped.
        converted_error = data.get('error')
        error_message = (converted_error.get('message', '')
                         if isinstance(converted_error, dict) else '')
        if not isinstance(error_message, str) or not error_message:
            error_message = 'provider returned an invalid response'
        error_status = provider_error_http_status(converted_error) or 500
        error_body = (json.dumps({'error': converted_error}, ensure_ascii=False)
                      if isinstance(converted_error, dict) else error_message)
        _classify_http_error(error_status, error_body, model, log_prefix,
                             max_tokens=max_tokens,
                             credential_present=_has_outbound_credential(hdrs))
    choices = data.get('choices')
    if choices is None:
        choices = []
    if not isinstance(choices, list):
        raise Exception(
            'API returned invalid response shape: choices must be an array')
    if not choices:
        raise Exception(
            f'API returned no choices: {json.dumps(data)[:500]}'
        )
    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        raise Exception(
            'API returned invalid response shape: choices[0] must be an object')
    raw_message = first_choice.get('message')
    if raw_message is None:
        msg = {}
    elif isinstance(raw_message, dict):
        msg = raw_message
    else:
        raise Exception(
            'API returned invalid response shape: choices[0].message must be an object')
    raw_content = msg.get('content', '')
    if raw_content is None:
        content = ''
    elif isinstance(raw_content, str):
        content = raw_content
    else:
        raise Exception(
            'API returned invalid response shape: assistant content must be text or null')
    raw_usage = data.get('usage', {})
    if isinstance(raw_usage, dict):
        usage = raw_usage
    else:
        logger.warning('[chat] Ignoring malformed non-object usage payload')
        usage = {}
    # Stamp canonical cache keys from vendor spellings (see _sse_core note).
    canonicalize_usage_cache_keys(usage)
    usage.update(body.usage_evidence(trace_id))
    from lib.llm.request_receipt import request_parameter_receipt
    usage['_request_parameters'] = request_parameter_receipt(body)

    if _reasoning_resolution:
        usage['_reasoning_resolution'] = _reasoning_resolution

    _finish_reason = first_choice.get('finish_reason', '')
    if isinstance(_finish_reason, str) and _finish_reason:
        usage['finish_reason'] = _finish_reason
    elif _finish_reason not in ('', None):
        logger.warning('[chat] Ignoring malformed non-string finish_reason')

    # Strip MiniMax-style <think>...</think> tags
    if content and '<think>' in content:
        raw_len = len(content)
        content = re.sub(r'<think>[\s\S]*?</think>\s*', '', content).strip()
        if '<think>' in content:
            content = content[:content.index('<think>')].strip()
        if len(content) != raw_len:
            logger.debug('[chat] Stripped <think> tags from non-stream response '
                        '(%d → %d chars)', raw_len, len(content))

    _tool_calls = msg.get('tool_calls')
    if _tool_calls is not None:
        if isinstance(_tool_calls, dict):
            _tool_calls = [_tool_calls]
        elif not isinstance(_tool_calls, list):
            raise Exception(
                'API returned invalid response shape: tool_calls must be an array')
        if _tool_calls:
            usage['_tool_calls'] = _tool_calls
    # DeepSeek V4 thinking-mode tool calls require the complete reasoning
    # content to be echoed on the assistant message in every later request.
    # The streaming path already preserves this field; expose it to callers
    # of the non-streaming helper as private dispatch metadata as well.
    _reasoning_content = (msg.get('reasoning_content')
                          or msg.get('thinking')
                          or msg.get('reasoning'))
    if not _reasoning_content and isinstance(msg.get('reasoning_details'), list):
        _reasoning_parts = []
        for part in msg['reasoning_details']:
            if not isinstance(part, dict):
                continue
            _reasoning_part = part.get('thinking') or part.get('text')
            if isinstance(_reasoning_part, str):
                _reasoning_parts.append(_reasoning_part)
        _reasoning_content = ''.join(_reasoning_parts)
    if isinstance(_reasoning_content, str) and _reasoning_content:
        usage['_reasoning_content'] = _reasoning_content

    usage['trace_id'] = trace_id
    if resp_trace and resp_trace != trace_id:
        usage['resp_trace_id'] = resp_trace
    _quota_scope = ('oauth_codex' if oauth == 'codex' else
                    ('adapter:' + str(adapter.get('agent_id') or '')
                     if isinstance(adapter, dict) and adapter else 'codex'))
    usage = record_codex_quota(resp.headers, usage, cache_key=_quota_scope)
    if _adapter and isinstance(usage, dict) and USAGE_QUOTA_KEY in usage:
        from lib.desktop import adapter as _adapter_service
        _adapter_service.note_adapter_quota_observed(
            _adapter.get('agent_id'), user_id=_owner_scope)

    if log_prefix:
        tokens = usage.get('total_tokens', 0)
        logger.debug('%s Done: %d chars, ~%d tokens', log_prefix, len(content), tokens)

    if current_timing() is not None:
        # Counts reveal slow reasoning without logging any reasoning text.
        with timing_stage('response_usage', trace_id=trace_id, model=model,
                output_chars=len(content),
                reasoning_chars=len(_reasoning_content) if isinstance(_reasoning_content, str) else 0,
                output_tokens=usage.get('completion_tokens', usage.get('output_tokens')),
                reasoning_tokens=(usage.get('completion_tokens_details') or {}).get('reasoning_tokens')
                    if isinstance(usage.get('completion_tokens_details'), dict) else None):
            pass

    return content, usage
