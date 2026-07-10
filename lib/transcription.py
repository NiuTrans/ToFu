"""lib/transcription.py — provider-agnostic speech-to-text (STT / ASR).

Turns an uploaded audio blob into text by routing through the existing
``llm_dispatch`` slot pool. The design mirrors how vision is modelled
(``lib/model_info.model_supports_vision``): a *capability* on a slot, not a
hard-coded vendor branch.

Two transcription mechanisms (both provider-agnostic)
-----------------------------------------------------
There are two DIFFERENT wire shapes for turning audio into text, and a slot
declares which one it speaks via its capability:

1. ``transcription`` — the dedicated Whisper-style endpoint: a
   ``multipart/form-data`` ``POST /v1/audio/transcriptions`` carrying the raw
   audio (OpenAI ``gpt-4o-transcribe``, Groq ``whisper-large-v3-turbo``). The
   chat dispatch layer (``dispatch_chat`` / ``dispatch_stream``) drives the
   chat-completions JSON+SSE shape and CANNOT carry this multipart body, so we
   reuse only its SLOT SELECTION and issue the POST ourselves
   (:func:`_post_to_provider`).

2. ``audio_chat`` — omni CHAT models that accept audio INLINE as an
   ``input_audio`` content-part in a normal ``/chat/completions`` request and
   reply with the transcript (Meituan gateway's ``gemini-3-flash-preview``,
   ``LongCat-Flash-Omni``). These do NOT expose ``/audio/transcriptions`` at
   all — tagging them ``transcription`` would POST to a 404. For these we build
   the ``input_audio`` messages and route through :func:`dispatch_chat`
   (:func:`_transcribe_via_chat`).

There are deliberately NO ``if provider == 'openai'`` branches (CLAUDE.md §3.5):
the capability on the slot — not the vendor — selects the mechanism.

Graceful disable
----------------
When no transcription-capable slot is configured, :func:`transcription_available`
returns ``False`` and the route reports the feature as disabled (the frontend
hides the mic button) rather than erroring — no vendor is assumed to exist.

LLM correction pass
-------------------
The optional "cheap LLM fixes ASR errors" step is implemented here as
:func:`maybe_correct`, gated by :func:`correction_enabled` (env
``TOFU_ASR_CORRECTION``, default OFF). Published generative-error-correction
results show a *zero-shot* correction pass can INCREASE word-error-rate, and
``gpt-4o-transcribe`` already corrects internally — so this is deliberately
NOT wired into the MVP transcribe path. It stays a measured, opt-in follow-up.
"""

from __future__ import annotations

import io
import os
import struct
from dataclasses import dataclass

from lib.log import audit_log, get_logger

logger = get_logger(__name__)

__all__ = [
    'TRANSCRIPTION_CAP',
    'AUDIO_CHAT_CAP',
    'TRANSCRIPTION_CAPS',
    'TranscriptionError',
    'TranscriptionResult',
    'transcription_available',
    'list_transcription_models',
    'transcribe',
    'allowed_audio_upload',
    'audio_byte_cap',
    'inline_audio_byte_cap',
    'silence_rms_floor',
    'silence_peak_floor',
    'max_chars_per_second',
    'correction_enabled',
    'maybe_correct',
]

# The capability tags a slot may carry to be a transcription target. Modelled
# exactly like 'vision' — see lib/model_info.model_supports_vision.
#   TRANSCRIPTION_CAP → dedicated multipart /audio/transcriptions endpoint.
#   AUDIO_CHAT_CAP    → omni chat model, audio sent inline as an input_audio
#                       content-part through /chat/completions.
TRANSCRIPTION_CAP = 'transcription'
AUDIO_CHAT_CAP = 'audio_chat'
TRANSCRIPTION_CAPS = frozenset({TRANSCRIPTION_CAP, AUDIO_CHAT_CAP})

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


def audio_byte_cap() -> int:
    """Max accepted audio upload size in bytes.

    Defaults to 25 MiB — the ceiling most hosted transcription APIs enforce
    on ``/audio/transcriptions`` (OpenAI, Groq). Override per-deployment with
    ``TOFU_AUDIO_MAX_BYTES``.
    """
    try:
        v = int(os.environ.get('TOFU_AUDIO_MAX_BYTES', '') or 0)
        if v > 0:
            return v
    except (ValueError, TypeError) as e:
        logger.debug('[STT] bad TOFU_AUDIO_MAX_BYTES, using default: %s', e)
    return 25 * 1024 * 1024


def max_audio_duration_s() -> float:
    """Max accepted audio duration in seconds (best-effort; see :func:`_probe_duration_s`).

    Defaults to 600 s (10 min). Override with ``TOFU_AUDIO_MAX_DURATION_S``.
    A duration guard only bites for formats whose length we can cheaply read
    from a header (WAV); for opus/mp3 the byte cap is the effective bound.
    """
    try:
        v = float(os.environ.get('TOFU_AUDIO_MAX_DURATION_S', '') or 0)
        if v > 0:
            return v
    except (ValueError, TypeError) as e:
        logger.debug('[STT] bad TOFU_AUDIO_MAX_DURATION_S, using default: %s', e)
    return 600.0


def inline_audio_byte_cap() -> int:
    """Max RAW audio size (bytes) for the inline chat-audio (``audio_chat``) path.

    Tighter than :func:`audio_byte_cap` on purpose: the ``audio_chat`` mechanism
    base64-encodes the audio INTO the chat request JSON, which inflates the
    payload ~33% and consumes model context. Gemini documents a 20 MB inline
    request ceiling, so we cap the RAW bytes at 20 MB (post-encode stays within
    that ceiling headroom for the surrounding JSON). Override with
    ``TOFU_INLINE_AUDIO_MAX_BYTES``.
    """
    try:
        v = int(os.environ.get('TOFU_INLINE_AUDIO_MAX_BYTES', '') or 0)
        if v > 0:
            return v
    except (ValueError, TypeError) as e:
        logger.debug('[STT] bad TOFU_INLINE_AUDIO_MAX_BYTES, using default: %s', e)
    return 20 * 1024 * 1024


# Accepted upload formats. Keys are lowercase extensions; the values are the
# canonical MIME types the standard transcription endpoints accept. A browser
# MediaRecorder produces webm/opus or ogg/opus; native recorders produce
# m4a/mp4/wav; users may drop mp3/flac files.
_ALLOWED_AUDIO: dict[str, str] = {
    'webm': 'audio/webm',
    'ogg': 'audio/ogg',
    'oga': 'audio/ogg',
    'wav': 'audio/wav',
    'mp3': 'audio/mpeg',
    'mpga': 'audio/mpeg',
    'mpeg': 'audio/mpeg',
    'm4a': 'audio/mp4',
    'mp4': 'audio/mp4',
    'flac': 'audio/flac',
}


def allowed_audio_upload(filename: str, content_type: str | None = None) -> str | None:
    """Return the canonical MIME type for an accepted audio upload, else None.

    Acceptance is driven by the file EXTENSION (authoritative — the browser's
    reported ``content_type`` is advisory and often blank on drops). Returns
    ``None`` when the extension is not in the allow-list, which the route maps
    to HTTP 400.
    """
    if not filename:
        return None
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    return _ALLOWED_AUDIO.get(ext)


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


# ── Slot selection (reuses the dispatch slot pool — NO chat picker) ─────

def _transcription_slots() -> list:
    """Return the configured transcription-capable slots (best score first).

    Scans ``dispatcher.slots`` for EITHER transcription capability
    (``transcription`` = multipart endpoint, or ``audio_chat`` = inline chat
    audio), exactly as ``model_supports_vision`` scans for ``vision``.
    OAuth-subscription slots are excluded: the Claude/Codex subscription
    endpoints expose neither audio path. Sorted by the slot's live ``score()``
    (lower = better) so a healthy, fast key is preferred and a cooled-down one
    drops to the back.
    """
    try:
        from lib.llm_dispatch.factory import get_dispatcher
        dispatcher = get_dispatcher()
        dispatcher.initialize()
    except Exception as e:
        logger.warning('[STT] dispatcher unavailable: %s', e)
        return []
    slots = [s for s in dispatcher.slots
             if (s.capabilities & TRANSCRIPTION_CAPS) and not s.oauth]
    slots.sort(key=lambda s: s.score())
    return slots


def _slot_audio_mode(slot) -> str:
    """Return which transcription mechanism a slot speaks: 'endpoint' | 'chat'.

    A slot carrying ``transcription`` uses the dedicated multipart endpoint; a
    slot carrying ONLY ``audio_chat`` uses the inline chat-audio path. When a
    slot somehow carries BOTH, the dedicated endpoint wins (it's purpose-built
    for transcription and cheaper than a full omni chat turn).
    """
    if TRANSCRIPTION_CAP in slot.capabilities:
        return 'endpoint'
    return 'chat'


def transcription_available() -> bool:
    """True when at least one transcription-capable slot is configured.

    The route uses this to disable the feature gracefully (mic button hidden)
    instead of assuming any particular vendor exists.
    """
    return bool(_transcription_slots())


def list_transcription_models() -> list[dict]:
    """Return ``[{model, provider_id}]`` for configured transcription slots.

    Deduplicated by ``(model, provider_id)`` in preference order. Used by the
    capabilities surface so the frontend can decide whether to show the mic.
    """
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for s in _transcription_slots():
        key = (s.model, s.provider_id or 'default')
        if key in seen:
            continue
        seen.add(key)
        out.append({'model': s.model, 'provider_id': s.provider_id or 'default',
                    'mode': _slot_audio_mode(s)})
    return out


# ── Duration probe (best-effort) ────────────────────────────────────────

def _probe_duration_s(audio_bytes: bytes, mime: str) -> float | None:
    """Return the audio duration in seconds when cheaply knowable, else None.

    Only a WAV/RIFF header is parsed (no decode, no third-party dep): the
    'data' chunk size divided by the byte-rate from the 'fmt ' chunk. For
    compressed formats (webm/opus, mp3, m4a, flac) the length cannot be read
    without decoding, so this returns ``None`` and the byte cap is the only
    bound — callers must treat ``None`` as "unknown, allow".
    """
    if not audio_bytes or len(audio_bytes) < 44:
        return None
    if audio_bytes[:4] != b'RIFF' or audio_bytes[8:12] != b'WAVE':
        return None
    try:
        byte_rate = None
        data_size = None
        pos = 12
        n = len(audio_bytes)
        while pos + 8 <= n:
            chunk_id = audio_bytes[pos:pos + 4]
            (chunk_size,) = struct.unpack('<I', audio_bytes[pos + 4:pos + 8])
            body = pos + 8
            if chunk_id == b'fmt ' and body + 16 <= n:
                # byte_rate is at offset 8 within the fmt body (little-endian).
                (byte_rate,) = struct.unpack('<I', audio_bytes[body + 8:body + 12])
            elif chunk_id == b'data':
                data_size = chunk_size
                break
            pos = body + chunk_size + (chunk_size & 1)  # chunks are word-aligned
        if byte_rate and data_size and byte_rate > 0:
            return data_size / float(byte_rate)
    except Exception as e:
        logger.debug('[STT] WAV duration probe failed: %s', e)
    return None


# ── Silence / energy gate (root-cause guard, best-effort) ───────────────

def silence_rms_floor() -> float:
    """RMS amplitude (0..1) at/below which a WAV clip is treated as silent.

    Default 0.006 (~-44 dBFS) — deliberately CONSERVATIVE: pure/near-silence
    is caught, but a quiet-but-real utterance (which always carries higher rms
    AND clear peaks) is NOT dropped. Override with ``TOFU_AUDIO_SILENCE_RMS``.
    """
    try:
        v = float(os.environ.get('TOFU_AUDIO_SILENCE_RMS', '') or 0)
        if v > 0:
            return v
    except (ValueError, TypeError) as e:
        logger.debug('[STT] bad TOFU_AUDIO_SILENCE_RMS, using default: %s', e)
    return 0.006


def silence_peak_floor() -> float:
    """Peak amplitude (0..1) below which — together with the RMS floor — a WAV
    clip is treated as silent.

    Default 0.03 (~-30 dBFS). The gate requires BOTH rms < :func:`silence_rms_floor`
    AND peak < this: real speech, even quiet, has transient peaks well above
    this, so requiring both makes a false-positive drop of genuine audio very
    unlikely. Override with ``TOFU_AUDIO_SILENCE_PEAK``.
    """
    try:
        v = float(os.environ.get('TOFU_AUDIO_SILENCE_PEAK', '') or 0)
        if v > 0:
            return v
    except (ValueError, TypeError) as e:
        logger.debug('[STT] bad TOFU_AUDIO_SILENCE_PEAK, using default: %s', e)
    return 0.03


def _probe_wav_level(audio_bytes: bytes, mime: str) -> tuple[float, float] | None:
    """Return ``(rms, peak)`` amplitude in 0..1 for a PCM WAV, else ``None``.

    Reads the RIFF/``fmt ``/``data`` chunks (no third-party dep) and computes
    the RMS and peak of the 16-bit PCM samples normalized to 0..1. Returns
    ``None`` — meaning "unknown, allow" (same convention as
    :func:`_probe_duration_s`) — for anything we cannot cheaply measure:
    non-WAV/compressed input, a non-16-bit sample format, or a parse failure.
    Only 16-bit PCM (the format the frontend transcode emits, and the common
    WAV encoding) is measured; other bit depths return ``None``.
    """
    if not audio_bytes or len(audio_bytes) < 44:
        return None
    if audio_bytes[:4] != b'RIFF' or audio_bytes[8:12] != b'WAVE':
        return None
    try:
        bits = None
        audio_format = None
        data = None
        pos = 12
        n = len(audio_bytes)
        while pos + 8 <= n:
            chunk_id = audio_bytes[pos:pos + 4]
            (chunk_size,) = struct.unpack('<I', audio_bytes[pos + 4:pos + 8])
            body = pos + 8
            if chunk_id == b'fmt ' and body + 16 <= n:
                (audio_format,) = struct.unpack('<H', audio_bytes[body:body + 2])
                (bits,) = struct.unpack('<H', audio_bytes[body + 14:body + 16])
            elif chunk_id == b'data':
                data = audio_bytes[body:body + chunk_size]
                break
            pos = body + chunk_size + (chunk_size & 1)  # chunks are word-aligned
        # Only linear 16-bit PCM (audio_format == 1) is measurable here.
        if data is None or bits != 16 or (audio_format not in (None, 1)):
            return None
        # Trim to a whole number of 16-bit samples.
        usable = len(data) - (len(data) % 2)
        if usable < 2:
            return 0.0, 0.0
        import array
        samples = array.array('h')
        samples.frombytes(data[:usable])
        if not samples:
            return 0.0, 0.0
        peak = 0
        sq_sum = 0.0
        for s in samples:
            a = s if s >= 0 else -s
            if a > peak:
                peak = a
            sq_sum += float(s) * float(s)
        norm = 32768.0
        rms = (sq_sum / len(samples)) ** 0.5 / norm
        return rms, peak / norm
    except Exception as e:
        logger.debug('[STT] WAV level probe failed: %s', e)
        return None


def _is_silent_wav(audio_bytes: bytes, mime: str) -> bool:
    """True when a WAV clip is measurably below BOTH the RMS and peak floors.

    Compressed / unmeasurable input returns ``False`` (unknown → allow) so the
    gate only ever SHORT-CIRCUITS input it is confident is silent.
    """
    level = _probe_wav_level(audio_bytes, mime)
    if level is None:
        return False
    rms, peak = level
    return rms < silence_rms_floor() and peak < silence_peak_floor()


# ── Hallucination heuristic (FLAG only — never drops) ───────────────────

def max_chars_per_second() -> float:
    """chars/sec above which a transcript is FLAGGED as implausibly dense.

    Default 25 cps — fast English tops ~17 cps and CJK runs FEWER chars/sec,
    so 25 flags only clear fabrication (e.g. 1809 chars from a 1.5s clip ≈
    1200 cps) while never touching dense real speech. This is a diagnostic
    FLAG, not a rejection: a legitimately fast or dense speaker must not be
    silently discarded on the ratio alone. Override ``TOFU_AUDIO_MAX_CPS``.
    """
    try:
        v = float(os.environ.get('TOFU_AUDIO_MAX_CPS', '') or 0)
        if v > 0:
            return v
    except (ValueError, TypeError) as e:
        logger.debug('[STT] bad TOFU_AUDIO_MAX_CPS, using default: %s', e)
    return 25.0


def _suspect_hallucination(text: str, duration_s: float | None) -> bool:
    """True when ``text`` is implausibly long for a known ``duration_s``.

    Only fires when the duration is known (WAV) and positive; returns ``False``
    for unknown-duration (compressed) input so we never flag on a guess.
    """
    if not text or not duration_s or duration_s <= 0:
        return False
    return (len(text) / duration_s) > max_chars_per_second()


# ── Provider POST (isolated seam — monkeypatched in tests) ──────────────

def _post_to_provider(slot, audio_bytes: bytes, filename: str, mime: str,
                      *, language: str | None, prompt: str | None) -> str:
    """POST the audio to a slot's ``/audio/transcriptions`` endpoint; return text.

    Isolated so tests can stub the network entirely by monkeypatching
    ``lib.transcription._post_to_provider``. Uses the unified ``http_post``
    (proxy-aware). Raises :class:`TranscriptionError` on any transport or
    HTTP-status failure.
    """
    import lib as _lib
    from lib.http_client import http_post

    base = (slot.base_url or getattr(_lib, 'LLM_BASE_URL', '') or '').rstrip('/')
    if not base:
        raise TranscriptionError('No base URL configured for transcription slot',
                                 status=503)
    url = f'{base}/audio/transcriptions'

    headers = {'Authorization': f'Bearer {slot.api_key}'}
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


# Map a canonical audio MIME to the ``format`` token the OpenAI ``input_audio``
# content-part expects (e.g. 'wav', 'mp3', 'webm'). The API keys on this short
# token, not the full MIME.
_MIME_TO_FORMAT: dict[str, str] = {
    'audio/webm': 'webm',
    'audio/ogg': 'ogg',
    'audio/wav': 'wav',
    'audio/mpeg': 'mp3',
    'audio/mp4': 'm4a',
    'audio/flac': 'flac',
}


def _audio_format_token(mime: str) -> str:
    """Return the ``input_audio.format`` token for a canonical audio MIME."""
    return _MIME_TO_FORMAT.get(mime, 'wav')


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
               prompt: str | None = None) -> TranscriptionResult:
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
    endpoint_cap = audio_byte_cap()
    inline_cap = inline_audio_byte_cap()
    if len(audio_bytes) > max(endpoint_cap, inline_cap):
        raise TranscriptionError(
            f'Audio too large (max {max(endpoint_cap, inline_cap) // (1024 * 1024)} MB)',
            status=400)

    duration_s = _probe_duration_s(audio_bytes, mime)
    if duration_s is not None:
        max_dur = max_audio_duration_s()
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
    if _is_silent_wav(audio_bytes, mime):
        logger.info('[STT] silence gate: %d bytes (%s, %.2fs) below energy '
                    'floor — returning empty (no model call)',
                    len(audio_bytes), mime,
                    duration_s if duration_s is not None else -1.0)
        audit_log('audio_transcribe', model='silence-gate',
                  provider_id='local', mode='silence_gate',
                  bytes=len(audio_bytes), mime=mime,
                  duration_s=(round(duration_s, 1) if duration_s is not None else None),
                  text_len=0, silence_gated=True)
        return TranscriptionResult(text='', model='silence-gate',
                                   provider_id='local', duration_s=duration_s)

    slots = _transcription_slots()
    if not slots:
        raise TranscriptionError(
            'No transcription model is configured. Add a provider whose model '
            f'carries the {TRANSCRIPTION_CAP!r} or {AUDIO_CHAT_CAP!r} '
            'capability.', status=503)

    last_err: TranscriptionError | None = None
    for slot in slots:
        mode = _slot_audio_mode(slot)
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
                text = _transcribe_via_chat(slot, audio_bytes, mime,
                                            language=language, prompt=prompt)
            else:
                text = _post_to_provider(slot, audio_bytes, filename, mime,
                                         language=language, prompt=prompt)
            text = (text or '').strip()
        except TranscriptionError as e:
            last_err = e
            logger.warning('[STT] slot %s:%s failed (%s) — trying next',
                           slot.key_name, slot.model, e.detail)
            continue
        suspected = _suspect_hallucination(text, duration_s)
        if suspected:
            logger.warning('[STT] implausible transcript: %d chars for %.2fs '
                           '(%.0f chars/s > %.0f cap) via %s:%s [%s] — flagged '
                           'suspected_hallucination, NOT dropped',
                           len(text), duration_s, len(text) / duration_s,
                           max_chars_per_second(), slot.key_name, slot.model,
                           mode)
        audit_log('audio_transcribe',
                  model=slot.model, provider_id=slot.provider_id or 'default',
                  mode=mode, bytes=len(audio_bytes), mime=mime,
                  duration_s=(round(duration_s, 1) if duration_s is not None else None),
                  text_len=len(text), suspected_hallucination=suspected)
        logger.info('[STT] transcribed %d bytes (%s) via %s:%s [%s] → %d chars',
                    len(audio_bytes), mime, slot.key_name, slot.model, mode,
                    len(text))
        return TranscriptionResult(
            text=text, model=slot.model,
            provider_id=slot.provider_id or 'default', duration_s=duration_s)

    raise last_err or TranscriptionError('All transcription slots failed',
                                         status=502)


# ── Optional LLM correction pass (flag-gated, default OFF, NOT MVP) ─────

def correction_enabled() -> bool:
    """True when the ASR LLM-correction pass is enabled (default OFF).

    Env ``TOFU_ASR_CORRECTION`` in {1,true,yes,on}. Kept OFF by default:
    published generative-error-correction studies show a zero-shot correction
    pass can RAISE word-error-rate, and strong transcription models already
    self-correct. Turn on only after measuring a win on real samples.
    """
    return (os.environ.get('TOFU_ASR_CORRECTION', '').strip().lower()
            in ('1', 'true', 'yes', 'on'))


def maybe_correct(text: str, *, context: str | None = None) -> str:
    """Optionally clean up an ASR transcript with a cheap LLM (no-op when off).

    Returns ``text`` unchanged when :func:`correction_enabled` is False (the
    default) so callers can invoke it unconditionally once wired. When enabled,
    routes a low-temperature ``cheap``-capability chat that fixes homophones,
    punctuation, and obvious proper-noun slips WITHOUT adding content. This is
    a follow-up increment — the MVP transcribe route does NOT call it.
    """
    if not text or not correction_enabled():
        return text
    try:
        from lib.llm_dispatch import dispatch_chat
        sys_prompt = (
            'You correct raw speech-to-text transcripts. Fix homophones, '
            'punctuation, casing, and obvious proper-noun mis-hearings. Do NOT '
            'add, remove, summarize, translate, or answer content. Return ONLY '
            'the corrected transcript text.')
        user = text if not context else f'Context: {context}\n\nTranscript:\n{text}'
        corrected, _usage = dispatch_chat(
            [{'role': 'system', 'content': sys_prompt},
             {'role': 'user', 'content': user}],
            capability='cheap', temperature=0, max_tokens=2048,
            log_prefix='[STT-correct]')
        corrected = (corrected or '').strip()
        return corrected or text
    except Exception as e:
        logger.warning('[STT] correction pass failed, returning raw text: %s', e)
        return text
