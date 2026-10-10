"""Installed operation bindings, independent of advertised model capabilities.

The capability taxonomy owns tag semantics; this module owns which installed
wire adapters can execute those operations. It grants no model capability or
credential authorization. All non-chat listings and execution use the same
bounded lookup, and caller protocol restrictions can only narrow it.
"""
from __future__ import annotations

from lib.model_info.capability_taxonomy import CAPABILITY_SEMANTICS

CHAT_PROTOCOLS = frozenset({'openai', 'openai_responses', 'anthropic', 'local'})
OPENAI_OPERATION_PROTOCOLS = frozenset({'openai', 'local'})
_OPERATION_PROTOCOLS = {
    'chat_completions': CHAT_PROTOCOLS,
    'images_generations': OPENAI_OPERATION_PROTOCOLS,
    'embeddings': OPENAI_OPERATION_PROTOCOLS,
    'audio_transcriptions': OPENAI_OPERATION_PROTOCOLS,
    'audio_speech': OPENAI_OPERATION_PROTOCOLS,
}


def protocols_for_capability(capability: str) -> frozenset[str]:
    """Return installed bindings, not a prediction about a remote endpoint."""
    operation = (CAPABILITY_SEMANTICS.get(capability) or {}).get('endpoint')
    return _OPERATION_PROTOCOLS.get(operation, frozenset())


def has_operation_binding(capability: str, protocol: str) -> bool:
    return protocol in protocols_for_capability(capability)
