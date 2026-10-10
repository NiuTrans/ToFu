"""lib/transcription/_audio.py — audio format gating & best-effort WAV probes.

Pure, dependency-free audio helpers: the upload allow-list, the ``input_audio``
format-token map, and the header-only WAV probes (duration, RMS/peak level,
silence gate) plus the chars/sec hallucination heuristic. None of these touch
the network or a provider — they only inspect the raw bytes.
"""

from __future__ import annotations

import struct

from lib.log import get_logger

from lib.transcription._config import (
    max_chars_per_second,
    silence_peak_floor,
    silence_rms_floor,
)

logger = get_logger(__name__)

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

def _suspect_hallucination(text: str, duration_s: float | None) -> bool:
    """True when ``text`` is implausibly long for a known ``duration_s``.

    Only fires when the duration is known (WAV) and positive; returns ``False``
    for unknown-duration (compressed) input so we never flag on a guess.
    """
    if not text or not duration_s or duration_s <= 0:
        return False
    return (len(text) / duration_s) > max_chars_per_second()
