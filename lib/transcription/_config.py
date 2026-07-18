"""lib/transcription/_config.py — env-driven caps, capability constants, slots.

Holds the tunable limits (byte caps, duration cap, silence floors, chars/sec
flag), the transcription capability tags, and the slot-selection helpers that
reuse the ``llm_dispatch`` slot pool. See the package ``__init__`` docstring
for the design rationale (capability-on-slot, NO vendor branches).
"""

from __future__ import annotations

import os

from lib.log import get_logger

logger = get_logger(__name__)

# The capability tags a slot may carry to be a transcription target. Modelled
# exactly like 'vision' — see lib/model_info.model_supports_vision.
#   TRANSCRIPTION_CAP → dedicated multipart /audio/transcriptions endpoint.
#   AUDIO_CHAT_CAP    → omni chat model, audio sent inline as an input_audio
#                       content-part through /chat/completions.
TRANSCRIPTION_CAP = 'transcription'
AUDIO_CHAT_CAP = 'audio_chat'
TRANSCRIPTION_CAPS = frozenset({TRANSCRIPTION_CAP, AUDIO_CHAT_CAP})


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


def correction_enabled() -> bool:
    """True when the ASR LLM-correction pass is enabled (default OFF).

    Env ``TOFU_ASR_CORRECTION`` in {1,true,yes,on}. Kept OFF by default:
    published generative-error-correction studies show a zero-shot correction
    pass can RAISE word-error-rate, and strong transcription models already
    self-correct. Turn on only after measuring a win on real samples.
    """
    return (os.environ.get('TOFU_ASR_CORRECTION', '').strip().lower()
            in ('1', 'true', 'yes', 'on'))


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
    # Resolve through the package so test monkeypatches on
    # ``lib.transcription._transcription_slots`` take effect (facade parity).
    from lib import transcription as _facade
    return bool(_facade._transcription_slots())


def list_transcription_models() -> list[dict]:
    """Return ``[{model, provider_id}]`` for configured transcription slots.

    Deduplicated by ``(model, provider_id)`` in preference order. Used by the
    capabilities surface so the frontend can decide whether to show the mic.
    """
    # Resolve through the package facade so test monkeypatches take effect.
    from lib import transcription as _facade
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for s in _facade._transcription_slots():
        key = (s.model, s.provider_id or 'default')
        if key in seen:
            continue
        seen.add(key)
        out.append({'model': s.model, 'provider_id': s.provider_id or 'default',
                    'mode': _facade._slot_audio_mode(s)})
    return out
