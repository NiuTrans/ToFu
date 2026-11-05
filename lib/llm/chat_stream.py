"""Complete-text projection of subscription Responses streams.

Entry: complete_subscription_stream, called by chat. Body construction, OAuth,
transport, retries, cancellation and budget settlement retain their existing
owners; this adapter only projects a verified provider result to chat's tuple.
"""


def complete_subscription_stream(
        messages, *, model, max_tokens, temperature, thinking_enabled,
        thinking_depth, preset, effort, extra, thinking_format, inference_profile,
        context_window, knowledge_model, provider_id, route_output_limit_key,
        responses_feature_profile, model_budget_admit, timeout, max_retries,
        **transport):
    from lib.llm.body import build_body
    from lib.llm.request import request_preparation
    from lib.llm.stream import stream_chat
    from lib.llm.stream_result import ensure_provider_stream_result

    with request_preparation():
        body = build_body(
            model, messages, max_tokens=max_tokens, temperature=temperature,
            thinking_enabled=thinking_enabled, thinking_depth=thinking_depth,
            preset=preset, effort=effort, extra=extra, stream=True,
            thinking_format=thinking_format, inference_profile=inference_profile,
            context_window=context_window, knowledge_model=knowledge_model,
            provider_id=provider_id)
        body['_responses_feature_profile'] = responses_feature_profile or 'compatible'
        if route_output_limit_key:
            body['_route_output_limit_key'] = route_output_limit_key
        if model_budget_admit is not None:
            body['_model_budget_admit'] = model_budget_admit

    result = ensure_provider_stream_result(stream_chat(
        body, max_retries=max_retries, attempt_timeout=timeout,
        first_byte_timeout=timeout, **transport)).require_verified(
            context='subscription complete-text response')
    message = result.message
    usage = dict(result.usage or {})
    usage['finish_reason'] = result.finish_reason
    if message.get('tool_calls'):
        usage['_tool_calls'] = message['tool_calls']
    reasoning = message.get('reasoning_content') or message.get('thinking')
    if isinstance(reasoning, str) and reasoning:
        usage['_reasoning_content'] = reasoning
    return message.get('content') or '', usage
