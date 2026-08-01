"""lib/llm/responses_outbound/ — OpenAI Responses API boundary layer.

The third wire protocol, sibling to ``anthropic_outbound/``: the canonical
OpenAI Chat Completions body is converted at the HTTP boundary, and the
Responses SSE stream is translated back into OpenAI chunks — nothing
upstream of the boundary knows the protocol exists.

Sub-modules:
  _url             — ``responses_url(base_url)``
  _to_responses    — ``openai_body_to_responses(body, profile, stream)``
                     (+ ``RESPONSES_PROFILES``: ``default`` for generic
                     providers like DeepSeek, ``codex`` for the ChatGPT
                     subscription backend)
  _sse             — ``ResponsesSSETranslator`` (plugs into SSEAccumulator)
  _from_responses  — ``responses_response_to_openai(data)`` (non-stream)

Extracted from ``lib/oauth/codex.py`` (2026-07-31, epic pt_b7a29ea7);
``lib.oauth.codex`` re-exports the converter + translator under their
legacy names so the Codex OAuth path rides this same layer.
"""

from lib.llm.responses_outbound._from_responses import (
    responses_response_to_openai,
)
from lib.llm.responses_outbound._sse import ResponsesSSETranslator
from lib.llm.responses_outbound._to_responses import (
    RESPONSES_PROFILES,
    openai_body_to_responses,
)
from lib.llm.responses_outbound._url import responses_url

__all__ = [
    'RESPONSES_PROFILES',
    'ResponsesSSETranslator',
    'openai_body_to_responses',
    'responses_response_to_openai',
    'responses_url',
]
