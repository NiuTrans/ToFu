"""lib/llm_sanitize/_fields.py — API field filtering helpers.

Defines the OpenAI-compatible message field allow-list and the helpers that
strip frontend metadata / tool_calls before a request is sent.
"""

import copy

from lib.log import get_logger

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════
#  Allowed API fields
# ══════════════════════════════════════════════════════════

# Fields that are valid in OpenAI-compatible chat/completions API messages.
# Everything else is frontend/display metadata and must be stripped to avoid
# bloating the request body (toolRounds alone can be >1 MB).
_API_MESSAGE_FIELDS = frozenset({
    'role', 'content', 'name',              # standard OpenAI
    'tool_calls', 'tool_call_id',           # tool use
    'reasoning_content',                    # thinking models (vendor extension)
    'thinking_signature',                   # Claude extended-thinking block signature
                                            # — needed on Continue replay so the
                                            # Anthropic proxy can re-attach a signed
                                            # thinking block to the assistant turn.
    'reasoning_details',                    # OpenRouter-style reasoning array — the
                                            # OpenAI-compat shape the sankuai gateway
                                            # uses to round-trip a signed Claude
                                            # thinking block (reconstructed in build_body).
    'cache_control',                        # Anthropic prompt caching
})


# Producer-owned private fields that explain an intentional same-role seam.
# They are NOT provider fields.  The opt-in field-strip path collapses them to
# one short-lived boolean so the final structural merge can classify the
# ORIGINAL adjacency after frontend metadata has been removed.  The merge
# consumes and deletes this hint on every output path.
_SAME_ROLE_SEAM_SOURCE_FIELDS = frozenset({
    '_contextComposer',
    '_isMeta',
    '_isVuDirective',
    '_isObjectiveAnchor',
})
_SAME_ROLE_SEAM_HINT_FIELD = '_tofuSameRoleSeam'


def _strip_non_api_fields(
    messages: list,
    *,
    carry_same_role_seam_hints: bool = False,
) -> list:
    """Return a new message list with only API-relevant fields.

    Strips frontend metadata (toolRounds, thinking, translatedContent,
    apiRounds, toolSummary, usage, timestamp, images, originalContent, …)
    that inflate the JSON body sent to the LLM gateway.

    Does NOT mutate the original messages — nested mutable values (``content``
    block lists, ``tool_calls``, …) are DEEP-copied, not shared by reference.
    This isolation matters because downstream build_body steps mutate the
    cleaned messages in place — ``_validate_image_blocks`` /
    ``_downscale_oversized_images`` rewrite ``block['image_url']['url']`` and
    ``_inject_gemini_thought_signatures`` writes into ``tool_calls[0]``. With a
    shallow copy those writes would leak back into the caller's persistent
    messages, changing the prefix bytes on the next round (a prompt-cache miss
    for image conversations). Immutable scalars (role/name strings, and the big
    base64 payloads inside content — deepcopy returns immutables as-is) are not
    duplicated, so the copy stays cheap on the hot path.

    ``carry_same_role_seam_hints`` is reserved for the two final wire builders.
    It reduces known producer markers to one transient boolean used only by
    ``_merge_consecutive_same_role``.  The default remains a strict API-field
    projection, and the transient hint is never part of
    ``_API_MESSAGE_FIELDS``.
    """
    cleaned = []
    stripped_keys = set()
    dropped_messages = 0
    for msg in messages:
        if not isinstance(msg, dict):
            dropped_messages += 1
            continue
        intentional_same_role_seam = (
            carry_same_role_seam_hints
            and any(msg.get(field)
                    for field in _SAME_ROLE_SEAM_SOURCE_FIELDS)
        )
        clean = {}
        for k, v in msg.items():
            if k in _API_MESSAGE_FIELDS:
                clean[k] = copy.deepcopy(v) if isinstance(v, (list, dict)) else v
            else:
                stripped_keys.add(k)
        if intentional_same_role_seam:
            clean[_SAME_ROLE_SEAM_HINT_FIELD] = True
        cleaned.append(clean)
    if stripped_keys:
        logger.debug('[build_body] Stripped non-API fields from %d messages: %s',
                     len(messages), ', '.join(sorted(stripped_keys)))
    if dropped_messages:
        logger.warning(
            '[build_body] Dropped %d malformed non-object message carrier(s) '
            'before provider serialization', dropped_messages)
    return cleaned


def _strip_tool_calls(msg: dict) -> dict:
    """Return a copy of an assistant message with ``tool_calls`` removed but
    every other field preserved.

    Critically keeps ``reasoning_content`` / ``thinking_signature`` /
    ``reasoning_details`` so that Claude/DeepSeek extended-thinking replay can
    still re-attach a signed thinking block. Rebuilding as a bare
    ``{'role': 'assistant', 'content': ...}`` (the previous behaviour) dropped
    those fields and triggered Anthropic HTTP 400 on the next turn.
    """
    return {k: v for k, v in msg.items() if k != 'tool_calls'}
