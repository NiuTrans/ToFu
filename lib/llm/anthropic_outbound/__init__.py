# HOT_PATH
"""Outbound Anthropic Messages API adapter.

The rest of Tofu speaks OpenAI Chat Completions internally. Some gateways
(e.g. the sankuai AIGC gateway's Claude Code app) only accept the
**Anthropic Messages API** (``POST /v1/messages``) for certain models.
This module translates a fully-built OpenAI request body into an Anthropic
request, and translates the Anthropic response / SSE stream back into the
OpenAI shape the rest of the pipeline already understands.

Direction: Tofu-as-Anthropic-CLIENT (outbound). This is the inverse of
``lib/compat/anthropic.py`` (Tofu-as-Anthropic-SERVER, inbound).

Public API:
  - anthropic_messages_url(base_url) -> str
  - anthropic_headers(api_key, extra_headers=None) -> dict
  - openai_body_to_anthropic(body) -> dict
  - anthropic_response_to_openai(data) -> dict   (non-streaming)
  - AnthropicSSETranslator                        (streaming)

This ``__init__`` is a pure re-export facade — all implementations live in
the sub-modules (``_url`` / ``_to_anthropic`` / ``_from_anthropic`` /
``_sse``). Every ``from lib.llm.anthropic_outbound import X`` continues to
resolve byte-identically.
"""

from lib.log import get_logger

logger = get_logger(__name__)


# ── Endpoint / header helpers ────────────────────────────────────────────────
from lib.llm.anthropic_outbound._url import (  # noqa: E402,F401
    ANTHROPIC_VERSION,
    anthropic_messages_url,
    anthropic_headers,
)

# ── Outbound: OpenAI → Anthropic ─────────────────────────────────────────────
from lib.llm.anthropic_outbound._to_anthropic import (  # noqa: E402,F401
    _media_type_and_data,
    _image_block,
    _convert_content_blocks,
    _convert_tools,
    _assistant_blocks,
    openai_body_to_anthropic,
)

# ── Inbound: Anthropic → OpenAI (non-streaming) ──────────────────────────────
from lib.llm.anthropic_outbound._from_anthropic import (  # noqa: E402,F401
    _STOP_REASON_MAP,
    _blocks_to_openai_message,
    _convert_usage,
    anthropic_response_to_openai,
)

# ── Streaming translator ─────────────────────────────────────────────────────
from lib.llm.anthropic_outbound._sse import (  # noqa: E402,F401
    AnthropicSSETranslator,
)


__all__ = [
    'ANTHROPIC_VERSION',
    'anthropic_messages_url',
    'anthropic_headers',
    'openai_body_to_anthropic',
    'anthropic_response_to_openai',
    'AnthropicSSETranslator',
]
