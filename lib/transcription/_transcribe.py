"""lib/transcription/_transcribe.py — the provider seams and transcribe() entry.

Holds the two provider mechanisms as isolated, monkeypatchable seams
(:func:`_post_to_provider` for the multipart endpoint, :func:`_transcribe_via_chat`
for inline chat-audio) plus the public :func:`transcribe` orchestrator, the
error/result types, and the omni-chat instruction constant.

Facade-aware dispatch
---------------------
Tests (and callers) monkeypatch names on the ``lib.transcription`` PACKAGE —
e.g. ``monkeypatch.setattr(tr, '_post_to_provider', ...)``. To honor those
patches, :func:`transcribe` resolves its swappable dependencies through the
package module (``_facade``) rather than binding them at import time. This keeps
the split byte-behaviour-identical to the original single-module facade.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from lib.log import get_logger

from lib.transcription._audio import (
    _ALLOWED_AUDIO,
    _audio_format_token,
    allowed_audio_upload,
)
from lib.transcription._config import (
    AUDIO_CHAT_CAP,
    TRANSCRIPTION_CAP,
)

logger = get_logger(__name__)

# Default instruction that steers an omni chat model to emit ONLY the verbatim
# transcript (no preamble, no answer). The silence clause is a SECOND layer
# behind the energy gate (:func:`_probe_wav_level`): a generative chat model
# obeys "return empty on silence" unreliably, so the gate is the real fix and
# this only backstops non-WAV / unmeasurable input. Overridable via env.
_AUDIO_CHAT_INSTRUCTION = (
    'Transcribe the audio to text verbatim. Output ONLY the transcript with no '
    'commentary, no preamble, no translation, and no answer to its content. '
    'If the audio contains no discernible speech — it is silence, music, '
    'noise, or otherwise has nothing spoken — output an empty response and '
    'nothing else. Never invent or guess words that were not clearly spoken.')


# ── Errors ────────────────────────────────────────────────────────────

class TranscriptionError(Exception):
    """A transcription failure carrying the HTTP status the route should emit.

    ``status`` is the HTTP code (503 when no model is configured, 502 on an
    upstream provider failure, 400 for a payload problem). ``detail`` is the
    human-readable message safe to surface to the caller.
    """

    def __init__(self, detail: str, *, status: int = 502):
        super().__init__(detail)
        self.detail = detail
        self.status = status


@dataclass
class TranscriptionResult:
    """Outcome of a successful transcription."""

    text: str
    model: str
    provider_id: str
    duration_s: float | None = None


# ── Provider POST (isolated seam — monkeypatched in tests) ──────────────

def _post_to_provider(slot, audio_bytes: bytes, filename: str, mime: str,
                      *, language: str | None, prompt: str | None) -> str:
    """POST the audio to a slot's ``/audio/transcriptions`` endpoint; return text.

    Isolated so tests can stub the network entirely by monkeypatching
    ``lib.transcription._post_to_provider``. Uses the unified ``http_post``
    (proxy-aware). Raises :class:`TranscriptionError` on any transport or
    HTTP-status failure.
    """
    from lib.http_client import http_post

    base = (slot.base_url or '').rstrip('/')
    if not base:
        raise TranscriptionError('No base URL configured for transcription slot',
                                 status=503)
    url = f'{base}/audio/transcriptions'

    headers = {}
    if slot.api_key:
        headers['Authorization'] = f'Bearer {slot.api_key}'
    if slot.extra_headers:
        headers.update(slot.extra_headers)
    # NOTE: do NOT set Content-Type — requests derives the multipart boundary
    # from the ``files=`` argument automatically.

    files = {'file': (filename, io.BytesIO(audio_bytes), mime)}
    data = {'model': slot.model, 'response_format': 'json'}
    if language:
        data['language'] = language
    if prompt:
        data['prompt'] = prompt

    try:
        resp = http_post(url, files=files, data=data, headers=headers, timeout=120)
    except Exception as e:
        raise TranscriptionError(f'Transcription request failed: {e}',
                                 status=502) from e
    if resp.status_code != 200:
        body = (resp.text or '')[:300]
        raise TranscriptionError(
            f'Transcription provider returned HTTP {resp.status_code}: {body}',
            status=502)
    try:
        payload = resp.json()
    except Exception as e:
        raise TranscriptionError(f'Transcription response was not JSON: {e}',
                                 status=502) from e
    # The standard shape is {"text": "..."}; verbose_json nests it the same way.
    text = (payload or {}).get('text', '')
    if not isinstance(text, str):
        text = str(text or '')
    return text.strip()


def _transcribe_via_chat(slot, audio_bytes: bytes, mime: str,
                         *, language: str | None, prompt: str | None) -> str:
    """Transcribe by sending the audio INLINE to an omni chat model.

    Builds an OpenAI ``input_audio`` content-part carrying the base64 audio plus
    a verbatim-transcribe instruction, and routes it through
    :func:`dispatch_chat` pinned to this slot's model (``strict_model=True`` so
    the dispatcher never silently swaps to a text-only model that would reject
    the audio part). Returns the reply text.

    Isolated as a seam (like :func:`_post_to_provider`) so tests can stub the
    chat dispatch entirely. Raises :class:`TranscriptionError` on failure.
    """
    import base64

    from lib.llm_dispatch import dispatch_chat

    b64 = base64.b64encode(audio_bytes).decode('ascii')
    fmt = _audio_format_token(mime)
    instruction = _AUDIO_CHAT_INSTRUCTION
    if language:
        instruction += f' The spoken language is {language}.'
    if prompt:
        instruction += f' Context terms: {prompt}'
    messages = [{
        'role': 'user',
        'content': [
            {'type': 'input_audio',
             'input_audio': {'data': b64, 'format': fmt}},
            {'type': 'text', 'text': instruction},
        ],
    }]
    try:
        content, _usage = dispatch_chat(
            messages, capability=AUDIO_CHAT_CAP, prefer_model=slot.model,
            strict_model=True, temperature=0, max_tokens=4096,
            log_prefix='[STT-chat]')
    except Exception as e:
        raise TranscriptionError(f'Inline audio-chat transcription failed: {e}',
                                 status=502) from e
    return (content or '').strip()


# ── Public entry point ──────────────────────────────────────────────────

def transcribe(audio_bytes: bytes, filename: str,
               content_type: str | None = None, *,
               language: str | None = None,
               prompt: str | None = None,
               owner_user_id: int | None = None,
               tenant_id: str | None = None) -> TranscriptionResult:
    """Transcribe an audio blob to text via a configured transcription slot.

    Args:
        audio_bytes: The raw audio payload.
        filename: Original filename (its extension drives MIME acceptance).
        content_type: Advisory browser-reported MIME (not trusted for gating).
        language: Optional ISO-639-1 hint forwarded to the provider.
        prompt: Optional biasing prompt (domain terms / proper nouns) forwarded
            to providers that support it (e.g. gpt-4o-transcribe).

    Returns:
        A :class:`TranscriptionResult`.

    Raises:
        TranscriptionError: 503 when no transcription model is configured, 400
            for a payload problem (empty / oversize / unsupported / too long),
            502 on an upstream provider failure. The route maps ``.status``.
    """
    # Resolve swappable dependencies through the PACKAGE so test monkeypatches
    # on ``lib.transcription.<name>`` take effect (see module docstring).
    from lib import transcription as _facade

    mime = allowed_audio_upload(filename, content_type)
    if mime is None:
        raise TranscriptionError(
            f'Unsupported audio format: {filename!r}. Accepted: '
            + ', '.join(sorted(_ALLOWED_AUDIO)), status=400)
    if not audio_bytes:
        raise TranscriptionError('Empty audio upload', status=400)
    # The endpoint path accepts up to the (larger) multipart ceiling; a chat
    # path bumps the RAW-byte limit down because base64 inflates the payload.
    # Reject only when the blob exceeds the LARGER cap here; the tighter inline
    # cap is enforced per-slot below once we know the chosen slot's mode.
    endpoint_cap = _facade.audio_byte_cap()
    inline_cap = _facade.inline_audio_byte_cap()
    if len(audio_bytes) > max(endpoint_cap, inline_cap):
        raise TranscriptionError(
            f'Audio too large (max {max(endpoint_cap, inline_cap) // (1024 * 1024)} MB)',
            status=400)

    duration_s = _facade._probe_duration_s(audio_bytes, mime)
    if duration_s is not None:
        max_dur = _facade.max_audio_duration_s()
        if duration_s > max_dur:
            raise TranscriptionError(
                f'Audio too long ({duration_s:.0f}s, max {max_dur:.0f}s)',
                status=400)

    # ── Silence gate (root-cause guard) ──
    # A generative audio_chat model invents confident text from silence/noise
    # instead of returning empty. When the clip is measurably silent (WAV PCM
    # only), short-circuit to an empty transcript WITHOUT dispatching — this
    # kills the silence→hallucination path at the source AND saves a billed
    # call. Placed before slot selection on purpose: a silent clip has nothing
    # to transcribe regardless of which slot (or none) is configured, so the
    # empty result is the correct UX even when no model exists. Unmeasurable
    # (compressed) input is treated as "unknown, allow" and falls through.
    if _facade._is_silent_wav(audio_bytes, mime):
        logger.info('[STT] silence gate: %d bytes (%s, %.2fs) below energy '
                    'floor — returning empty (no model call)',
                    len(audio_bytes), mime,
                    duration_s if duration_s is not None else -1.0)
        _facade.audit_log('audio_transcribe', model='silence-gate',
                          provider_id='local', mode='silence_gate',
                          bytes=len(audio_bytes), mime=mime,
                          duration_s=(round(duration_s, 1) if duration_s is not None else None),
                          text_len=0, silence_gated=True)
        return TranscriptionResult(text='', model='silence-gate',
                                   provider_id='local', duration_s=duration_s)

    # Validation and the silence gate deliberately precede repository access:
    # malformed/silent input must not depend on model availability or mint a
    # request-scoped slot group.  Once validated, owner-aware callers route
    # through an isolated v2 group and recurse into the provider-only path.
    if owner_user_id is not None:
        from lib.llm_dispatch.provider_pin import provider_pin
        from lib.model_routing import (
            ModelRoutingError,
            ModelRoutingRepository,
            OPENAI_CHAT_COMPATIBLE_PROTOCOLS,
            OPENAI_COMPATIBLE_PROTOCOLS,
            OwnerBoundary,
            dispose_routed_slot_group,
            mint_capability_slot_group,
        )

        repository = ModelRoutingRepository()
        boundary = OwnerBoundary.create(owner_user_id, tenant_id)
        route_group = None
        last_route_error = None
        for capability in (TRANSCRIPTION_CAP, AUDIO_CHAT_CAP):
            try:
                required_protocols = (
                    OPENAI_COMPATIBLE_PROTOCOLS
                    if capability == TRANSCRIPTION_CAP
                    else OPENAI_CHAT_COMPATIBLE_PROTOCOLS
                )
                _model, route_group = mint_capability_slot_group(
                    repository,
                    boundary,
                    capability,
                    required_protocols=required_protocols,
                    owner_tag=f'audio-transcribe:{owner_user_id}',
                )
                break
            except ModelRoutingError as exc:
                last_route_error = exc
                if exc.kind != 'model_route_unavailable':
                    raise TranscriptionError(str(exc), status=503) from exc
        if route_group is None:
            raise TranscriptionError(
                str(last_route_error or 'No transcription model is configured'),
                status=503,
            )
        try:
            with provider_pin(route_group.pin_id):
                return transcribe(
                    audio_bytes,
                    filename,
                    content_type,
                    language=language,
                    prompt=prompt,
                )
        finally:
            dispose_routed_slot_group(route_group)

    slots = _facade._transcription_slots()
    if not slots:
        raise TranscriptionError(
            'No transcription model is configured. Add a provider whose model '
            f'carries the {TRANSCRIPTION_CAP!r} or {AUDIO_CHAT_CAP!r} '
            'capability.', status=503)

    last_err: TranscriptionError | None = None
    for slot in slots:
        mode = _facade._slot_audio_mode(slot)
        # Per-mode size guard: the inline chat path base64-inflates the audio
        # into the request, so it enforces the tighter inline cap.
        if mode == 'chat' and len(audio_bytes) > inline_cap:
            last_err = TranscriptionError(
                f'Audio too large for inline transcription '
                f'(max {inline_cap // (1024 * 1024)} MB)', status=400)
            logger.warning('[STT] slot %s:%s skipped — %d bytes over inline cap',
                           slot.key_name, slot.model, len(audio_bytes))
            continue
        if mode == 'endpoint' and len(audio_bytes) > endpoint_cap:
            last_err = TranscriptionError(
                f'Audio too large (max {endpoint_cap // (1024 * 1024)} MB)',
                status=400)
            continue
        try:
            if mode == 'chat':
                text = _facade._transcribe_via_chat(slot, audio_bytes, mime,
                                                    language=language, prompt=prompt)
            else:
                text = _facade._post_to_provider(slot, audio_bytes, filename, mime,
                                                 language=language, prompt=prompt)
            text = (text or '').strip()
            # Fail-safe Chinese-variant normalization: ASR models transcribe
            # Mandarin into Traditional by default, so convert to the configured
            # variant (Simplified by default). Never raises / never degrades the
            # text — see lib.transcription._zh.
            text = _facade.normalize_zh_variant(text)
        except TranscriptionError as e:
            last_err = e
            logger.warning('[STT] slot %s:%s failed (%s) — trying next',
                           slot.key_name, slot.model, e.detail)
            continue
        suspected = _facade._suspect_hallucination(text, duration_s)
        if suspected:
            logger.warning('[STT] implausible transcript: %d chars for %.2fs '
                           '(%.0f chars/s > %.0f cap) via %s:%s [%s] — flagged '
                           'suspected_hallucination, NOT dropped',
                           len(text), duration_s, len(text) / duration_s,
                           _facade.max_chars_per_second(), slot.key_name, slot.model,
                           mode)
        logical_model = getattr(slot, 'logical_model', '') or slot.model
        public_provider_id = (getattr(slot, 'routing_provider_id', '')
                              or slot.provider_id or 'default')
        _facade.audit_log('audio_transcribe',
                          model=logical_model, provider_id=public_provider_id,
                          mode=mode, bytes=len(audio_bytes), mime=mime,
                          duration_s=(round(duration_s, 1) if duration_s is not None else None),
                          text_len=len(text), suspected_hallucination=suspected)
        logger.info('[STT] transcribed %d bytes (%s) via %s:%s [%s] → %d chars',
                    len(audio_bytes), mime, slot.key_name, slot.model, mode,
                    len(text))
        return TranscriptionResult(
            text=text, model=logical_model,
            provider_id=public_provider_id, duration_s=duration_s)

    raise last_err or TranscriptionError('All transcription slots failed',
                                         status=502)
