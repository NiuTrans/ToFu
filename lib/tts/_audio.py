"""lib/tts/_audio.py — stdlib-only audio helpers for stitching synthesis output.

The podcast pipeline synthesizes per script segment and then joins the
pieces: lossless PCM concat when the provider returned WAV (with silence
injected at section boundaries), a byte-concat fallback for MP3 (frame-aligned
streams decode sequentially in practice; logged), plus duration measurement
(exact for WAV, bitrate estimate for MP3).

Everything here is stdlib ``wave`` + arithmetic — no ffmpeg dependency. The
optional ffmpeg loudness-normalize/transcode step lives in the podcast
engine, not here.
"""

from __future__ import annotations

import io
import wave

from lib.log import get_logger

logger = get_logger(__name__)


def wav_params(data: bytes) -> tuple[int, int, int, int]:
    """Return (channels, sampwidth, framerate, nframes) for a WAV blob."""
    with wave.open(io.BytesIO(data), 'rb') as w:
        return w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()


def wav_duration(data: bytes) -> float:
    """Exact duration (seconds) of a WAV blob."""
    _ch, _sw, rate, frames = wav_params(data)
    return frames / float(rate) if rate else 0.0


def silence_wav_bytes(duration_s: float, *, channels: int = 1,
                      sampwidth: int = 2, framerate: int = 24000) -> bytes:
    """A valid PCM WAV of digital silence with the given params."""
    frames = int(duration_s * framerate)
    pcm = b'\x00' * frames * channels * sampwidth
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(channels)
        w.setsampwidth(sampwidth)
        w.setframerate(framerate)
        w.writeframes(pcm)
    return buf.getvalue()


def concat_wavs(parts: list[bytes], *, pause_ms: list[int] | None = None) -> bytes:
    """Concatenate WAV blobs into one WAV, injecting silence between parts.

    ``pause_ms[i]`` is the silence inserted BEFORE part i (index 0 ignored).
    All parts take the FIRST part's (channels, sampwidth, framerate); a
    mismatched part is re-read and its frames written as-is (same provider →
    same params in practice; a genuine mismatch logs a warning).
    """
    blobs = [p for p in parts if p]
    if not blobs:
        raise ValueError('concat_wavs: no parts')
    channels, sampwidth, rate, _frames = wav_params(blobs[0])
    frame_size = channels * sampwidth

    out = io.BytesIO()
    with wave.open(out, 'wb') as w:
        w.setnchannels(channels)
        w.setsampwidth(sampwidth)
        w.setframerate(rate)
        for i, blob in enumerate(blobs):
            if i > 0:
                pause = (pause_ms[i] if pause_ms and i < len(pause_ms) else 0) or 0
                if pause > 0:
                    w.writeframes(b'\x00' * int(rate * pause / 1000) * frame_size)
            ch, sw, r, _n = wav_params(blob)
            if (ch, sw, r) != (channels, sampwidth, rate):
                logger.warning('[TTS:Audio] part %d params %s differ from first %s '
                               '— writing frames as-is', i, (ch, sw, r),
                               (channels, sampwidth, rate))
            with wave.open(io.BytesIO(blob), 'rb') as src:
                w.writeframes(src.readframes(src.getnframes()))
    return out.getvalue()


def estimate_mp3_duration(data: bytes, *, bitrate_kbps: int = 128) -> float:
    """Rough duration (seconds) of an MP3 blob from its size.

    MP3 frames carry no total-length field; without an ffprobe dependency we
    assume a constant bitrate (128 kbps is the common TTS default). Marked as
    an estimate in the caller's logs/meta.
    """
    if not data:
        return 0.0
    return len(data) * 8 / float(bitrate_kbps * 1000)
