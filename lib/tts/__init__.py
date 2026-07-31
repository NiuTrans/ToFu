"""lib/tts — provider-agnostic text-to-speech (TTS).

Turns text into spoken audio by routing through the existing ``llm_dispatch``
slot pool, exactly the way lib/transcription routes speech-to-text: a
*capability* (``tts``) on a slot, not a hard-coded vendor branch
(CLAUDE.md §3.5).

The wire shape is the OpenAI-compatible one::

    POST {base_url}/audio/speech
    {model, input, voice, response_format, speed}  →  raw audio bytes

Registration (owner directive 2026-07-25 — model name and voice are NEVER
hardcoded in the feature code):

  * Settings UI / server_config.json: add a provider whose model entry
    declares ``capabilities: ["tts"]`` (explicit per-cell capabilities win
    in the dispatcher), or
  * register a well-known public TTS model name (``tts-1``, ``tts-1-hd``,
    ``gpt-4o-mini-tts``) — these carry DEFAULT_SLOT_CONFIGS reference
    entries with the ``tts`` cap already attached (same pattern as the
    ``whisper-1`` transcription block).

Voice / format / speed resolve per request → ``data/config/tts.json``
(``default_voice``, ``default_format``, ``speed``, ``max_input_chars``) →
fallback constants (see ``_config``).

Graceful disable (owner directive): with no tts-capable slot,
:func:`tts_available` is False and callers degrade (paper podcast delivers
script + transcript only) instead of erroring.

This module is a pure re-export FACADE; ``synthesize`` resolves its
swappable dependencies through this package so
``monkeypatch.setattr(lib.tts, name, ...)`` in tests takes effect, same as
the transcription package's seam contract.
"""

from __future__ import annotations

from lib.log import get_logger  # noqa: F401

logger = get_logger(__name__)

__all__ = [
    'TTS_CAP',
    'TTSError',
    'SynthesizeResult',
    'synthesize',
    'tts_available',
    'list_tts_models',
    'default_voice',
    'default_format',
    'default_speed',
    'max_input_chars',
    'sniff_container',
    'wav_params',
    'wav_duration',
    'silence_wav_bytes',
    'concat_wavs',
    'estimate_mp3_duration',
]

# ── Config: capability constant, slot selection, deployment config ───────
from lib.tts._config import (  # noqa: E402,F401
    TTS_CAP,
    default_format,
    default_speed,
    default_voice,
    list_tts_models,
    max_input_chars,
    tts_available,
    _load_tts_config,
    _tts_slots,
)

# ── Synthesize: error/result types, provider seam, entry point ───────────
from lib.tts._synthesize import (  # noqa: E402,F401
    SynthesizeResult,
    TTSError,
    sniff_container,
    synthesize,
    _post_speech,
    _sniff_mime,
)

# ── Audio helpers: WAV params/concat/silence, MP3 duration estimate ──────
from lib.tts._audio import (  # noqa: E402,F401
    concat_wavs,
    estimate_mp3_duration,
    silence_wav_bytes,
    wav_duration,
    wav_params,
)
