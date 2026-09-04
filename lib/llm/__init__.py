"""lib/llm/ — LLM API client package.

Sub-modules:
  _transport    — Retry config, HTTP helpers, sleep utilities
  diagnostics   — Raw SSE diagnostic dumper (ring buffer + transcript)
  body          — Model-aware request body construction
  cache         — Anthropic prompt caching (cache breakpoints)
  chat          — Non-streaming chat completion
  stream        — Streaming chat completion with SSE parsing

All public symbols are re-exported here for convenience::

    from lib.llm import build_body, stream_chat, chat, add_cache_breakpoints
"""

from importlib import import_module

__all__ = [
    # body
    'build_body',
    '_validate_image_blocks',
    '_downscale_oversized_images',
    '_strip_trailing_assistant_for_claude',
    # cache
    'add_cache_breakpoints',
    '_gateway_honors_cache_markers',
    # chat
    'chat',
    # stream
    'stream_chat',
    'async_stream_chat',
    'ProviderStreamResult',
    'ProviderStreamState',
    'UnverifiedProviderStreamError',
    'ensure_provider_stream_result',
    'require_verified_provider_stream_result',
    # transport
    'MAX_STREAM_RETRIES',
    'RETRY_BACKOFF_BASE',
    'RETRY_BACKOFF_MAX',
    'RETRY_JITTER',
    'retry_wait',
    'abortable_sleep',
    # diagnostics
    'RawSSEDumper',
    # errors (re-exported)
    'AbortedError',
    'BadRequestError',
    'ContentFilterError',
    'ContextCompactionError',
    'EndpointUnreachableError',
    'InvalidImageError',
    'ModelLimitError',
    'PermissionError_',
    'PromptTooLongError',
    'RateLimitError',
    'RequestScopedError',
    'RetryableAPIError',
    'StreamOnlyError',
    '_classify_http_error',
    # model detection (re-exported)
    'is_claude', 'is_claude_opus_47', 'is_doubao', 'is_ernie',
    'is_gemini', 'is_glm', 'is_glm53', 'is_gpt', 'is_gpt5', 'is_gpt_56',
    'is_kimi', 'is_kimi_k3', 'is_longcat', 'is_minimax', 'is_qwen',
    'model_supports_vision',
    'gemini_reasoning_effort', 'glm_reasoning_effort', 'gpt_reasoning_effort',
    'kimi_k3_reasoning_effort',
    '_clamp_max_tokens',
    # sanitization (re-exported)
    '_drop_empty_assistant_messages',
    '_fix_empty_user_messages',
    '_fix_orphaned_tool_calls', '_fix_tool_call_adjacency',
    '_fix_tool_call_wire_shape',
    '_merge_consecutive_same_role', '_sanitize_gateway_content',
    '_sanitize_messages', '_strip_non_api_fields',
]


# A focused child import such as ``lib.llm.stream_result`` is used by verdict
# and wire-contract code that must not initialize HTTP transports. Preserve the
# historical package facade while resolving each symbol from its owner only on
# first access.
_EXPORT_MODULES = {
    # Body/cache and request entry points.
    'build_body': 'lib.llm.body',
    '_validate_image_blocks': 'lib.llm.body',
    '_downscale_oversized_images': 'lib.llm.body',
    '_strip_trailing_assistant_for_claude': 'lib.llm.body',
    'add_cache_breakpoints': 'lib.llm.cache',
    '_gateway_honors_cache_markers': 'lib.llm.cache',
    'chat': 'lib.llm.chat',
    'stream_chat': 'lib.llm.stream',
    'async_stream_chat': 'lib.llm.astream',
    'RawSSEDumper': 'lib.llm.diagnostics',
    # Typed stream settlement.
    'ProviderStreamResult': 'lib.llm.stream_result',
    'ProviderStreamState': 'lib.llm.stream_result',
    'UnverifiedProviderStreamError': 'lib.llm.stream_result',
    'ensure_provider_stream_result': 'lib.llm.stream_result',
    'require_verified_provider_stream_result': 'lib.llm.stream_result',
    # Transport policy.
    'MAX_STREAM_RETRIES': 'lib.llm._transport',
    'RETRY_BACKOFF_BASE': 'lib.llm._transport',
    'RETRY_BACKOFF_MAX': 'lib.llm._transport',
    'RETRY_JITTER': 'lib.llm._transport',
    'retry_wait': 'lib.llm._transport',
    'abortable_sleep': 'lib.llm._transport',
    # Error vocabulary.
    'AbortedError': 'lib.llm_errors',
    'BadRequestError': 'lib.llm_errors',
    'ContentFilterError': 'lib.llm_errors',
    'ContextCompactionError': 'lib.llm_errors',
    'EndpointUnreachableError': 'lib.llm_errors',
    'InvalidImageError': 'lib.llm_errors',
    'ModelLimitError': 'lib.llm_errors',
    'PermissionError_': 'lib.llm_errors',
    'PromptTooLongError': 'lib.llm_errors',
    'RateLimitError': 'lib.llm_errors',
    'RequestScopedError': 'lib.llm_errors',
    'RetryableAPIError': 'lib.llm_errors',
    'StreamOnlyError': 'lib.llm_errors',
    '_classify_http_error': 'lib.llm_errors',
    # Model metadata.
    '_clamp_max_tokens': 'lib.model_info',
    'gemini_reasoning_effort': 'lib.model_info',
    'glm_reasoning_effort': 'lib.model_info',
    'gpt_reasoning_effort': 'lib.model_info',
    'is_claude': 'lib.model_info',
    'is_claude_opus_47': 'lib.model_info',
    'is_doubao': 'lib.model_info',
    'is_ernie': 'lib.model_info',
    'is_gemini': 'lib.model_info',
    'is_glm': 'lib.model_info',
    'is_glm53': 'lib.model_info',
    'is_gpt': 'lib.model_info',
    'is_gpt5': 'lib.model_info',
    'is_gpt_56': 'lib.model_info',
    'is_kimi': 'lib.model_info',
    'is_kimi_k3': 'lib.model_info',
    'is_longcat': 'lib.model_info',
    'is_minimax': 'lib.model_info',
    'is_qwen': 'lib.model_info',
    'kimi_k3_reasoning_effort': 'lib.model_info',
    'model_supports_vision': 'lib.model_info',
    # Canonical message sanitization.
    '_drop_empty_assistant_messages': 'lib.llm_sanitize',
    '_fix_empty_user_messages': 'lib.llm_sanitize',
    '_fix_orphaned_tool_calls': 'lib.llm_sanitize',
    '_fix_tool_call_adjacency': 'lib.llm_sanitize',
    '_fix_tool_call_wire_shape': 'lib.llm_sanitize',
    '_merge_consecutive_same_role': 'lib.llm_sanitize',
    '_sanitize_gateway_content': 'lib.llm_sanitize',
    '_sanitize_messages': 'lib.llm_sanitize',
    '_strip_non_api_fields': 'lib.llm_sanitize',
}


def __getattr__(name: str):
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()).union(__all__))
