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

from lib.llm._transport import (
    MAX_STREAM_RETRIES,
    RETRY_BACKOFF_BASE,
    RETRY_BACKOFF_MAX,
    RETRY_JITTER,
    abortable_sleep,
    retry_wait,
)
from lib.llm.body import (
    _downscale_oversized_images,
    _strip_trailing_assistant_for_claude,
    _validate_image_blocks,
    build_body,
)
from lib.llm.cache import (
    _gateway_honors_cache_markers,
    add_cache_breakpoints,
)
from lib.llm.chat import chat
from lib.llm.diagnostics import RawSSEDumper
from lib.llm.stream import stream_chat
from lib.llm.astream import async_stream_chat

# Re-export errors
from lib.llm_errors import (  # noqa: F401
    AbortedError,
    ContentFilterError,
    EndpointUnreachableError,
    InvalidImageError,
    ModelLimitError,
    PermissionError_,
    PromptTooLongError,
    RateLimitError,
    RetryableAPIError,
    StreamOnlyError,
    _classify_http_error,
)

# Re-export model detection from lib.model_info
from lib.model_info import (  # noqa: F401
    _clamp_max_tokens,
    gemini_reasoning_effort,
    is_claude,
    is_claude_opus_47,
    is_doubao,
    is_ernie,
    is_gemini,
    is_glm,
    is_gpt,
    is_kimi,
    is_longcat,
    is_minimax,
    is_qwen,
    model_supports_vision,
)

# Re-export sanitization from lib.llm_sanitize
from lib.llm_sanitize import (  # noqa: F401
    _fix_empty_user_messages,
    _fix_orphaned_tool_calls,
    _fix_tool_call_adjacency,
    _merge_consecutive_same_role,
    _sanitize_gateway_content,
    _sanitize_messages,
    _strip_non_api_fields,
)

# Backward compat alias
_RawSSEDumper = RawSSEDumper

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
    # transport
    'MAX_STREAM_RETRIES',
    'RETRY_BACKOFF_BASE',
    'RETRY_BACKOFF_MAX',
    'RETRY_JITTER',
    'retry_wait',
    'abortable_sleep',
    # diagnostics
    'RawSSEDumper',
    '_RawSSEDumper',
    # errors (re-exported)
    'AbortedError',
    'ContentFilterError',
    'EndpointUnreachableError',
    'InvalidImageError',
    'ModelLimitError',
    'PermissionError_',
    'PromptTooLongError',
    'RateLimitError',
    'RetryableAPIError',
    'StreamOnlyError',
    '_classify_http_error',
    # model detection (re-exported)
    'is_claude', 'is_claude_opus_47', 'is_doubao', 'is_ernie',
    'is_gemini', 'is_glm', 'is_gpt', 'is_kimi', 'is_longcat',
    'is_minimax', 'is_qwen', 'model_supports_vision',
    'gemini_reasoning_effort',
    '_clamp_max_tokens',
    # sanitization (re-exported)
    '_fix_empty_user_messages',
    '_fix_orphaned_tool_calls', '_fix_tool_call_adjacency',
    '_merge_consecutive_same_role', '_sanitize_gateway_content',
    '_sanitize_messages', '_strip_non_api_fields',
]
