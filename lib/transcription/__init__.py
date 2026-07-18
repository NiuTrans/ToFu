"""lib/transcription — provider-agnostic speech-to-text (STT / ASR).

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

Package layout
--------------
This module is a pure re-export FACADE — every symbol below resolves to an
implementation in a sub-module, so ``from lib.transcription import X`` keeps
working byte-identically after the split:

  * ``_config``    — env caps, capability constants, slot selection
  * ``_audio``     — format allow-list, MIME map, WAV probes, silence gate
  * ``_transcribe``— error/result types, provider seams, transcribe()
  * ``_correct``   — flag-gated LLM correction pass

``transcribe`` resolves its swappable dependencies (``_transcription_slots``,
``_post_to_provider``, ``audit_log``, the caps, the probes, …) through THIS
package module, so ``monkeypatch.setattr(lib.transcription, name, ...)`` in
tests takes effect exactly as it did against the original single file.
"""

from __future__ import annotations

from lib.log import audit_log, get_logger  # noqa: F401 (audit_log is a patch seam)

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


# ── Config: caps, capability constants, slot selection ──────────────────
from lib.transcription._config import (  # noqa: E402,F401
    AUDIO_CHAT_CAP,
    TRANSCRIPTION_CAP,
    TRANSCRIPTION_CAPS,
    audio_byte_cap,
    correction_enabled,
    inline_audio_byte_cap,
    list_transcription_models,
    max_audio_duration_s,
    max_chars_per_second,
    silence_peak_floor,
    silence_rms_floor,
    transcription_available,
    _slot_audio_mode,
    _transcription_slots,
)

# ── Audio: allow-list, MIME map, WAV probes, silence/hallucination gates ─
from lib.transcription._audio import (  # noqa: E402,F401
    allowed_audio_upload,
    _ALLOWED_AUDIO,
    _MIME_TO_FORMAT,
    _audio_format_token,
    _is_silent_wav,
    _probe_duration_s,
    _probe_wav_level,
    _suspect_hallucination,
)

# ── Transcribe: errors, result, provider seams, entry point ─────────────
from lib.transcription._transcribe import (  # noqa: E402,F401
    TranscriptionError,
    TranscriptionResult,
    transcribe,
    _AUDIO_CHAT_INSTRUCTION,
    _post_to_provider,
    _transcribe_via_chat,
)

# ── Correction: flag-gated LLM cleanup pass ─────────────────────────────
from lib.transcription._correct import maybe_correct  # noqa: E402,F401
