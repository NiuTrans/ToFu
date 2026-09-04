"""lib/model_info/capability_taxonomy.py — capability classification SSOT.

Single source of truth for "which capability tags mean this model is / isn't
a chat model". Before this module the classification was duplicated in FIVE
places (dispatcher + pricing + transcription + 6 frontend files) with SUBTLY
different sets — the frontend forgot ``transcription`` entirely, which is why
the ``Doubao-Seed-ASR-2.0`` speech-to-text model leaked into the chat preset
dropdown despite the backend correctly excluding it.

Two DIFFERENT sets both live here (they are genuinely not the same set — see
each constant's docstring for why):

  * :data:`CHAT_EXCLUDED_CAPS`         — the FRONTEND-filter set. A model is
    hidden from chat pickers when its caps intersect this. Does NOT include
    ``audio_chat`` because an ``audio_chat`` omni model IS a chat model that
    happens to accept audio input — hiding it would take a legit chat model
    off the picker.

  * :data:`DISPATCHER_NON_CHAT_CAPS`   — the BACKEND-dispatcher set, applied
    to operational capabilities after pricing-only tags are removed. Includes
    ``audio_chat`` because
    a slot whose caps are EXCLUSIVELY ``{audio_chat}`` (no ``text``) has no
    text-chat surface and should be excluded. Omni chat models normally carry
    ``{text, audio_chat, ...}`` so they are NOT a subset and remain
    chat-compatible — this is exactly the dispatcher's intent.

Public helper :func:`is_chat_model` implements the FRONTEND semantics: any
non-empty intersection with :data:`CHAT_EXCLUDED_CAPS` → not-a-chat-model.
The dispatcher continues to use its stricter ``issubset`` check because its
job is different (never dispatch a caps-only-non-chat slot for a chat op).

:data:`KNOWN_CAPABILITIES` is the ordered authority of every known
capability tag; capability-toggle UIs render it instead of hardcoding
their own list.

See docs/adr/2026-07-24-capability-taxonomy.md (if present) for the full
rationale; the short version is in :func:`is_chat_model`'s docstring.
"""

from __future__ import annotations

from typing import Iterable

# ══════════════════════════════════════════════════════════════
#  CHAT_EXCLUDED_CAPS — frontend chat-picker exclusion set
# ══════════════════════════════════════════════════════════════
# A cap in this set means "if a model has this, don't offer it in a chat
# picker at all". This is the set the frontend uses to filter the model
# dropdown, the paper-reader model list, the settings-visibility list, etc.
#
# It deliberately does NOT include 'audio_chat': an audio_chat model is a
# chat model that just happens to accept audio as an input part; it still
# replies with text in a normal /chat/completions turn. Hiding it would take
# a real chat model off the picker.
#
# Model examples:
#   image_gen     — dall-e-3, gemini-3-pro-image-preview (POST /images/*)
#   embedding     — text-embedding-3-large            (POST /embeddings)
#   transcription — Doubao-Seed-ASR-2.0, whisper-1    (POST /audio/transcriptions)
#   tts           — tts-1-hd, gpt-4o-mini-tts         (POST /audio/speech)
CHAT_EXCLUDED_CAPS: frozenset[str] = frozenset({
    'image_gen',
    'embedding',
    'transcription',
    'tts',
})

# ══════════════════════════════════════════════════════════════
#  DISPATCHER_NON_CHAT_CAPS — backend dispatcher subset check
# ══════════════════════════════════════════════════════════════
# A slot is treated as NOT chat-compatible when its non-pricing capabilities
# are a subset of this set (see LLMDispatcher._is_chat_compatible). Includes 'audio_chat'
# on purpose: a slot carrying ONLY {audio_chat} (no text) has no text-chat
# surface. Real omni chat slots carry {text, audio_chat, ...} — NOT a subset
# — so they remain chat-eligible, which is what we want.
#
# NB: This is DIFFERENT from CHAT_EXCLUDED_CAPS by exactly {'audio_chat'} —
# the difference is intentional and is explicitly encoded here so future
# readers don't have to reconstruct "why two sets?".
DISPATCHER_NON_CHAT_CAPS: frozenset[str] = CHAT_EXCLUDED_CAPS | {'audio_chat'}

# ══════════════════════════════════════════════════════════════
#  KNOWN_CAPABILITIES — canonical ORDERED list of every capability tag
# ══════════════════════════════════════════════════════════════
# The one authority for "which capability tags exist, and in what order a
# capability-toggle UI renders them". Published via taxonomy_payload() as
# ``known_capabilities``; the browser's typed owner projects it onto
# runtimeScope and the settings toggle grids (model edit form, key×model
# access-matrix cell editor) render it — before this list existed each grid
# hardcoded its own copy of this array, so adding a cap meant editing N
# frontend literals (and forgetting one left the new cap untoggleable).
#
# Order is presentation-stable: it is the order the settings UI has always
# shown (chat-ish tags first, non-chat last). Reorder only intentionally —
# every client re-renders in this order.
#
# Invariant (pinned by tests/test_capability_taxonomy_parity.py):
# ``set(KNOWN_CAPABILITIES) == set(CAPABILITY_SEMANTICS)`` — a tag may not
# exist in one without the other.
KNOWN_CAPABILITIES: tuple[str, ...] = (
    'text',
    'vision',
    'video',
    'thinking',
    'cheap',
    'image_gen',
    'embedding',
    'transcription',
    'tts',
    'audio_chat',
)

# ══════════════════════════════════════════════════════════════
#  Per-capability semantic descriptor (published to the API)
# ══════════════════════════════════════════════════════════════
# {cap: {'role': 'chat' | 'multimodal-input' | 'non-chat',
#        'endpoint': 'chat_completions' | 'audio_transcriptions'
#                    | 'images_generations' | 'embeddings',
#        'in_chat_picker': bool,   # matches CHAT_EXCLUDED_CAPS complement
#        'is_dispatch_chat': bool} # matches DISPATCHER_NON_CHAT_CAPS complement
# Downstream code should NOT branch on this dict directly — use the two
# frozensets above. This descriptor exists purely so foreign frontends and
# docs can render "what does this cap mean" without hardcoding the answer.
CAPABILITY_SEMANTICS: dict[str, dict[str, object]] = {
    'text': {
        'role': 'chat', 'endpoint': 'chat_completions',
        'in_chat_picker': True, 'is_dispatch_chat': True},
    'vision': {
        'role': 'multimodal-input', 'endpoint': 'chat_completions',
        'in_chat_picker': True, 'is_dispatch_chat': True},
    'video': {
        'role': 'multimodal-input', 'endpoint': 'chat_completions',
        'in_chat_picker': True, 'is_dispatch_chat': True},
    'thinking': {
        'role': 'chat', 'endpoint': 'chat_completions',
        'in_chat_picker': True, 'is_dispatch_chat': True},
    'cheap': {
        'role': 'chat', 'endpoint': 'chat_completions',
        'in_chat_picker': True, 'is_dispatch_chat': True},
    'audio_chat': {
        'role': 'multimodal-input', 'endpoint': 'chat_completions',
        'in_chat_picker': True, 'is_dispatch_chat': True},
    'image_gen': {
        'role': 'non-chat', 'endpoint': 'images_generations',
        'in_chat_picker': False, 'is_dispatch_chat': False},
    'embedding': {
        'role': 'non-chat', 'endpoint': 'embeddings',
        'in_chat_picker': False, 'is_dispatch_chat': False},
    'transcription': {
        'role': 'non-chat', 'endpoint': 'audio_transcriptions',
        'in_chat_picker': False, 'is_dispatch_chat': False},
    'tts': {
        'role': 'non-chat', 'endpoint': 'audio_speech',
        'in_chat_picker': False, 'is_dispatch_chat': False},
}


def is_chat_model(caps: Iterable[str] | None) -> bool:
    """Return True when a model's capability list belongs in a chat picker.

    Implements the FRONTEND semantics: a model is chat-eligible for the UI
    dropdown iff none of its caps are in :data:`CHAT_EXCLUDED_CAPS`. An empty
    or missing caps list is treated as a chat model (matches the frontend's
    existing ``caps || ['text']`` default).

    This is NOT the same as the dispatcher's ``_is_chat_compatible`` check
    (which first removes pricing-only tags, then uses
    ``issubset(DISPATCHER_NON_CHAT_CAPS)``). See the module docstring for why
    the two differ.
    """
    if not caps:
        return True
    for c in caps:
        if c in CHAT_EXCLUDED_CAPS:
            return False
    return True


def taxonomy_payload() -> dict:
    """Return the taxonomy in a JSON-friendly shape for API surfaces.

    Consumed by ``/api/v1/capabilities`` and ``/api/v1/server-config`` so a
    foreign frontend can filter chat pickers correctly without hardcoding
    the exclusion set. Frontend :func:`window.isChatModel` reads this at
    boot and falls back to a hardcoded literal only when the endpoint is
    unreachable.
 ``known_capabilities`` feeds the settings capability-toggle
    grids via the typed owner's ``getKnownCapabilities()``.
    """
    return {
        'chat_excluded_caps': sorted(CHAT_EXCLUDED_CAPS),
        'dispatcher_non_chat_caps': sorted(DISPATCHER_NON_CHAT_CAPS),
        'known_capabilities': list(KNOWN_CAPABILITIES),
        'capability_semantics': {
            cap: dict(desc) for cap, desc in CAPABILITY_SEMANTICS.items()
        },
    }


__all__ = [
    'CHAT_EXCLUDED_CAPS',
    'DISPATCHER_NON_CHAT_CAPS',
    'KNOWN_CAPABILITIES',
    'CAPABILITY_SEMANTICS',
    'is_chat_model',
    'taxonomy_payload',
]
