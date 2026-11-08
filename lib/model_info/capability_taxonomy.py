"""lib/model_info/capability_taxonomy.py — capability classification SSOT.

Single source of truth for "which capability tags mean this model is / isn't
a chat model" and for the task family (*kind*) each model belongs to. Before
this module the classification was duplicated in FIVE places (dispatcher +
pricing + transcription + 6 frontend files) with SUBTLY different sets — the
frontend forgot ``transcription`` entirely, which is why the
``Doubao-Seed-ASR-2.0`` speech-to-text model leaked into the chat preset
dropdown despite the backend correctly excluding it.

Capability tags are a flat vocabulary that mixes four different things:
input modalities (``vision``, ``video``, ``audio_chat``), features
(``thinking``), cost tiers (``cheap``) and task families (``image_gen``,
``video_gen``, ``tts`` …). :data:`MODEL_KINDS` names the task families
explicitly. A kind is DERIVED from capabilities (:func:`model_kind`), never
stored, and decides which picker shows a model, which endpoint executes it,
which facts describe it (a token context window versus aspect ratios,
durations or voices) and which media price units it may declare.

Two DIFFERENT exclusion sets live here (they are genuinely not the same set —
see each constant's docstring for why):

  * :data:`CHAT_EXCLUDED_CAPS`         — the FRONTEND-filter set: every task
    capability of a non-chat kind. A model is hidden from chat pickers when
    its caps intersect this. Does NOT include ``audio_chat`` because an
    ``audio_chat`` omni model IS a chat model that happens to accept audio
    input — hiding it would take a legit chat model off the picker.

  * :data:`DISPATCHER_NON_CHAT_CAPS`   — the BACKEND-dispatcher set, applied
    to operational capabilities after pricing-only tags are removed. Includes
    ``audio_chat`` because
    a slot whose caps are EXCLUSIVELY ``{audio_chat}`` (no ``text``) has no
    text-chat surface and should be excluded. Omni chat models normally carry
    ``{text, audio_chat, ...}`` so they are NOT a subset and remain
    chat-compatible — this is exactly the dispatcher's intent.

Public helper :func:`is_chat_model` implements the FRONTEND semantics: any
non-empty intersection with :data:`CHAT_EXCLUDED_CAPS` → not-a-chat-model,
which is the same statement as ``model_kind(caps) == 'chat'``.
The dispatcher continues to use its stricter ``issubset`` check because its
job is different (never dispatch a caps-only-non-chat slot for a chat op).

:data:`KNOWN_CAPABILITIES` is the ordered authority of every known
capability tag; capability-toggle UIs render it instead of hardcoding
their own list. The rationale for kinds and media price units lives in
docs/MODEL_REGISTRATION.md ("Model kinds and media facts").
"""

from __future__ import annotations

from typing import Iterable

# ══════════════════════════════════════════════════════════════
#  MEDIA_PRICE_UNITS — the non-token billing units a media kind may declare
# ══════════════════════════════════════════════════════════════
# Token prices stay in ``pricing`` (``per_million_tokens``). Media models are
# often billed per generated image, per second of generated video/audio, per
# generation request (one song or clip), per minute of transcribed audio or
# per million synthesized characters; those rates live in a Model's
# ``media_pricing`` and use exactly these units (contract enum:
# contracts/model_routing_v2.schema.json ``mediaPriceRate.unit``).
MEDIA_PRICE_UNITS: tuple[str, ...] = (
    'per_image',
    'per_second',
    'per_request',
    'per_minute',
    'per_million_characters',
)

# ══════════════════════════════════════════════════════════════
#  MODEL_KINDS — ordered task families
# ══════════════════════════════════════════════════════════════
# Each kind: {
#   'id':                stable kind identifier (also the browser label key),
#   'capabilities':      task capabilities placing a model in this kind
#                        (empty only for 'chat', the default kind),
#   'output_modality':   what the kind produces,
#   'endpoint':          the API surface that executes it,
#   'media_price_units': MEDIA_PRICE_UNITS this kind may declare in
#                        ``media_pricing``. A kind is a *media kind* exactly
#                        when this is non-empty: its facts are media facts
#                        (media_profile/media_pricing), not a token context.
# }
# Order is both presentation order and precedence: a model carrying task
# capabilities of several kinds belongs to the FIRST non-chat kind listed.
# Reorder only intentionally — every client groups in this order.
MODEL_KINDS: tuple[dict[str, object], ...] = (
    {'id': 'chat', 'capabilities': (),
     'output_modality': 'text', 'endpoint': 'chat_completions',
     'media_price_units': ()},
    {'id': 'image_generation', 'capabilities': ('image_gen',),
     'output_modality': 'image', 'endpoint': 'images_generations',
     'media_price_units': ('per_image',)},
    {'id': 'video_generation', 'capabilities': ('video_gen',),
     'output_modality': 'video', 'endpoint': 'video_generations',
     'media_price_units': ('per_second', 'per_request')},
    {'id': 'audio_generation', 'capabilities': ('audio_gen',),
     'output_modality': 'audio', 'endpoint': 'audio_generations',
     'media_price_units': ('per_second', 'per_request')},
    {'id': 'speech_synthesis', 'capabilities': ('tts',),
     'output_modality': 'audio', 'endpoint': 'audio_speech',
     'media_price_units': ('per_million_characters', 'per_second')},
    {'id': 'speech_recognition', 'capabilities': ('transcription',),
     'output_modality': 'text', 'endpoint': 'audio_transcriptions',
     'media_price_units': ('per_minute',)},
    {'id': 'embedding', 'capabilities': ('embedding',),
     'output_modality': 'vector', 'endpoint': 'embeddings',
     'media_price_units': ()},
    {'id': 'reranking', 'capabilities': ('reranking',),
     'output_modality': 'score', 'endpoint': 'rerank',
     'media_price_units': ()},
)

CHAT_KIND = 'chat'

_KINDS_BY_ID: dict[str, dict[str, object]] = {
    str(kind['id']): kind for kind in MODEL_KINDS}

# ══════════════════════════════════════════════════════════════
#  CHAT_EXCLUDED_CAPS — frontend chat-picker exclusion set
# ══════════════════════════════════════════════════════════════
# A cap in this set means "if a model has this, don't offer it in a chat
# picker at all". This is the set the frontend uses to filter the model
# dropdown, the paper-reader model list, the settings-visibility list, etc.
# It is derived from MODEL_KINDS: every task capability of a non-chat kind.
#
# It deliberately does NOT include 'audio_chat': an audio_chat model is a
# chat model that just happens to accept audio as an input part; it still
# replies with text in a normal /chat/completions turn. Hiding it would take
# a real chat model off the picker.
#
# Model examples:
#   image_gen     — dall-e-3, gemini-3-pro-image-preview (POST /images/*)
#   video_gen     — sora-2, veo-3.1-generate-preview     (async video jobs)
#   audio_gen     — lyria-002                            (music / sound)
#   embedding     — text-embedding-3-large            (POST /embeddings)
#   reranking     — Qwen3-Reranker-0.6B               (POST /rerank)
#   transcription — Doubao-Seed-ASR-2.0, whisper-1    (POST /audio/transcriptions)
#   tts           — tts-1-hd, gpt-4o-mini-tts         (POST /audio/speech)
CHAT_EXCLUDED_CAPS: frozenset[str] = frozenset(
    str(capability)
    for kind in MODEL_KINDS if kind['id'] != CHAT_KIND
    for capability in kind['capabilities']  # type: ignore[union-attr]
)

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
# Order is presentation-stable: chat-ish tags first, task tags after, each
# media generation tag beside its sibling. Reorder only intentionally —
# every client re-renders in this order.
#
# Invariant (pinned by tests/model_routing/test_capability_taxonomy_parity.py):
# ``set(KNOWN_CAPABILITIES) == set(CAPABILITY_SEMANTICS)`` — a tag may not
# exist in one without the other.
KNOWN_CAPABILITIES: tuple[str, ...] = (
    'text',
    'vision',
    'video',
    'thinking',
    'cheap',
    'image_gen',
    'video_gen',
    'embedding',
    'reranking',
    'transcription',
    'tts',
    'audio_gen',
    'audio_chat',
)

# ══════════════════════════════════════════════════════════════
#  Per-capability semantic descriptor (published to the API)
# ══════════════════════════════════════════════════════════════
# {cap: {'role': 'chat' | 'multimodal-input' | 'non-chat',
#        'endpoint': the executing API surface (see MODEL_KINDS),
#        'in_chat_picker': bool,   # matches CHAT_EXCLUDED_CAPS complement
#        'is_dispatch_chat': bool} # matches DISPATCHER_NON_CHAT_CAPS complement
# Downstream code should NOT branch on this dict directly — use the two
# frozensets above or model_kind(). This descriptor exists purely so foreign
# frontends and docs can render "what does this cap mean" without hardcoding
# the answer. Note 'video' is video INPUT; video OUTPUT is 'video_gen'.
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
    'video_gen': {
        'role': 'non-chat', 'endpoint': 'video_generations',
        'in_chat_picker': False, 'is_dispatch_chat': False},
    'audio_gen': {
        'role': 'non-chat', 'endpoint': 'audio_generations',
        'in_chat_picker': False, 'is_dispatch_chat': False},
    'embedding': {
        'role': 'non-chat', 'endpoint': 'embeddings',
        'in_chat_picker': False, 'is_dispatch_chat': False},
    'reranking': {
        'role': 'non-chat', 'endpoint': 'rerank',
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


def model_kind(caps: Iterable[str] | None) -> str:
    """Return the task family (a :data:`MODEL_KINDS` id) of a capability list.

    The first non-chat kind (in :data:`MODEL_KINDS` order) whose task
    capability is present wins; otherwise the model is a ``'chat'`` model.
    Input-modality, feature and pricing tags never change the kind, so
    ``model_kind(caps) == 'chat'`` iff :func:`is_chat_model` is True.
    """
    present = {str(cap) for cap in (caps or ())}
    for kind in MODEL_KINDS:
        if present.intersection(kind['capabilities']):  # type: ignore[arg-type]
            return str(kind['id'])
    return CHAT_KIND


def is_media_kind(kind_id: str) -> bool:
    """True when *kind_id* is described by media facts (media price units)."""
    kind = _KINDS_BY_ID.get(kind_id)
    return bool(kind and kind['media_price_units'])


def media_price_units_for(caps: Iterable[str] | None) -> frozenset[str]:
    """Media price units a model with *caps* may declare in ``media_pricing``.

    The union over every kind whose task capability is present, so a model
    that both synthesizes and transcribes speech may price both. Chat,
    embedding and reranking models declare none: their prices are tokens.
    """
    present = {str(cap) for cap in (caps or ())}
    units: set[str] = set()
    for kind in MODEL_KINDS:
        if present.intersection(kind['capabilities']):  # type: ignore[arg-type]
            units.update(kind['media_price_units'])  # type: ignore[arg-type]
    return frozenset(units)


def taxonomy_payload() -> dict:
    """Return the taxonomy in a JSON-friendly shape for API surfaces.

    Consumed by ``/api/v1/capabilities`` and ``/api/v1/server-config`` so a
    foreign frontend can filter chat pickers correctly without hardcoding
    the exclusion set. Frontend :func:`window.isChatModel` reads this at
    boot and falls back to a hardcoded literal only when the endpoint is
    unreachable.
    ``known_capabilities`` feeds the settings capability-toggle
    grids via the typed owner's ``getKnownCapabilities()``; ``model_kinds``
    feeds the typed owner's ``modelKind()`` / ``getModelKinds()``.
    """
    return {
        'chat_excluded_caps': sorted(CHAT_EXCLUDED_CAPS),
        'dispatcher_non_chat_caps': sorted(DISPATCHER_NON_CHAT_CAPS),
        'known_capabilities': list(KNOWN_CAPABILITIES),
        'capability_semantics': {
            cap: dict(desc) for cap, desc in CAPABILITY_SEMANTICS.items()
        },
        'model_kinds': [
            {
                'id': kind['id'],
                'capabilities': list(kind['capabilities']),  # type: ignore[arg-type]
                'output_modality': kind['output_modality'],
                'endpoint': kind['endpoint'],
                'media_price_units': list(kind['media_price_units']),  # type: ignore[arg-type]
            }
            for kind in MODEL_KINDS
        ],
        'media_price_units': list(MEDIA_PRICE_UNITS),
    }


__all__ = [
    'CHAT_EXCLUDED_CAPS',
    'CHAT_KIND',
    'DISPATCHER_NON_CHAT_CAPS',
    'KNOWN_CAPABILITIES',
    'CAPABILITY_SEMANTICS',
    'MEDIA_PRICE_UNITS',
    'MODEL_KINDS',
    'is_chat_model',
    'is_media_kind',
    'media_price_units_for',
    'model_kind',
    'taxonomy_payload',
]
