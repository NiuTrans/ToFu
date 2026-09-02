"""lib/paper/podcast_engine/_audio.py — script → one audio file.

Per-segment synthesis over the spoken script, then stitching:

  * each segment's text is chunked to fit the provider input ceiling
    (lib.tts.max_input_chars, sentence-boundary splits so a chunk never
    starts mid-sentence);
  * every chunk is synthesized via lib.tts.synthesize with one retry and an
    abort check between chunks (per-chunk progress → ``segment_done``
    events, so a 12-segment podcast shows real progress instead of a
    spinner);
  * WAV parts are concatenated LOSSLESSLY with silence injected — 150 ms
    between chunks of one segment, 300 ms between segments of one section,
    800 ms at a section boundary (the audible "page turn");
  * MP3 parts fall back to byte-concat (MP3 frames decode sequentially;
    logged) with a bitrate-estimated duration;
  * with ffmpeg on the host, a WAV master is transcoded to MP3 (128k mono +
    loudnorm) for a phone-friendly export; without ffmpeg the WAV is served
    (bigger, but universally playable — logged as a degraded path).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile

from lib import tts as _tts
from lib.log import get_logger
from lib.paper.podcast_engine._errors import AudioSynthesisAborted

logger = get_logger(__name__)

#: Pauses (ms) injected between parts, by boundary kind.
_PAUSE_SAME_SEGMENT_MS = 150
_PAUSE_SAME_SECTION_MS = 300
_PAUSE_SECTION_BREAK_MS = 800

#: Sentence-ending punctuation for chunk splits (zh + en).
_SENTENCE_END_RE = re.compile(r'(?<=[。！？；!?;.])\s*')


def _chunk_text(text: str, max_chars: int) -> list[str]:
    """Split ``text`` into ≤max_chars chunks on sentence boundaries.

    A single over-long sentence is hard-split at max_chars (rare — script
    segments are 80–200 chars by prompt, so chunking mainly guards
    providers with small ceilings).
    """
    text = (text or '').strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    sentences = [s for s in _SENTENCE_END_RE.split(text) if s.strip()]
    chunks: list[str] = []
    cur = ''
    for s in sentences:
        if cur and len(cur) + len(s) + 1 > max_chars:
            chunks.append(cur)
            cur = s
        else:
            cur = (cur + ' ' + s).strip() if cur else s
        while len(cur) > max_chars:  # a lone over-long sentence
            chunks.append(cur[:max_chars])
            cur = cur[max_chars:].strip()
    if cur:
        chunks.append(cur)
    return chunks


def _synth_chunk_with_retry(chunk: str, *, voice: str, fmt: str,
                            speed: float | None) -> tuple[bytes, str]:
    """Synthesize one chunk; ONE retry on TTSError. Returns (bytes, model)."""
    try:
        res = _tts.synthesize(chunk, voice=voice, fmt=fmt, speed=speed)
        return res.audio_bytes, res.model
    except _tts.TTSError as _e:
        logger.debug('synth chunk with retry: TTSError (%s)', _e)
        res = _tts.synthesize(chunk, voice=voice, fmt=fmt, speed=speed)
        return res.audio_bytes, res.model


def _transcode_to_mp3(wav_bytes: bytes) -> bytes | None:
    """WAV → MP3 128k mono + loudnorm via ffmpeg; None when unavailable.

    Any ffmpeg failure (missing binary, missing libmp3lame, timeout) falls
    back to None — the caller then ships the WAV master (degraded path,
    logged there).
    """
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        return None
    try:
        with tempfile.TemporaryDirectory(prefix='tofu-podcast-') as td:
            src = os.path.join(td, 'master.wav')
            dst = os.path.join(td, 'out.mp3')
            with open(src, 'wb') as f:
                f.write(wav_bytes)
            proc = subprocess.run(
                [ffmpeg, '-y', '-v', 'error', '-i', src,
                 '-af', 'loudnorm', '-codec:a', 'libmp3lame', '-b:a', '128k',
                 '-ac', '1', dst],
                capture_output=True, timeout=300)
            if proc.returncode != 0 or not os.path.exists(dst):
                logger.warning('[Paper:Podcast:Audio] ffmpeg transcode failed '
                               '(rc=%s): %.300s', proc.returncode,
                               proc.stderr.decode('utf-8', 'replace'))
                return None
            with open(dst, 'rb') as f:
                return f.read()
    except Exception as e:
        logger.warning('[Paper:Podcast:Audio] ffmpeg transcode error: %s', e)
        return None


def synthesize_script_audio(script: dict, *, voice: str,
                            abort_check=None, on_segment_done=None,
                            fmt: str | None = None,
                            speed: float | None = None) -> dict:
    """Synthesize the whole script into one audio blob.

    Args:
        script: The validated script (segments carry section/text).
        voice: The resolved voice (already through request > config >
            fallback at the task layer).
        abort_check: Callable raising AudioSynthesisAborted (or returning
            True) when the task was aborted; checked before every chunk.
        on_segment_done: ``fn(done, total)`` progress hook → segment_done.
        fmt: response_format override (default: lib.tts.default_format()).
        speed: rate override (default: lib.tts.default_speed()).

    Returns:
        {audio_bytes, ext, mime, duration_sec, duration_estimated,
         tts_model, voice, container}

    Raises:
        AudioSynthesisAborted / tts.TTSError (no slot → 503, all slots down
        → 502 — the task layer maps these onto the error event).
    """
    segments = script.get('segments') or []
    if not segments:
        raise _tts.TTSError('script has no segments to synthesize', status=400)
    use_fmt = (fmt or '').strip() or _tts.default_format()
    max_chars = _tts.max_input_chars()

    parts: list[bytes] = []
    pauses: list[int] = []
    containers: set[str] = set()
    tts_model = ''
    prev_section: str | None = None
    total = len(segments)

    for si, seg in enumerate(segments):
        section = (seg or {}).get('section') or ''
        chunks = _chunk_text((seg or {}).get('text') or '', max_chars)
        for ci, chunk in enumerate(chunks):
            if abort_check and abort_check():
                raise AudioSynthesisAborted()
            blob, model = _synth_chunk_with_retry(chunk, voice=voice,
                                                  fmt=use_fmt, speed=speed)
            tts_model = tts_model or model
            containers.add(_tts.sniff_container(blob))
            if not parts:
                pauses.append(0)
            elif ci > 0:
                pauses.append(_PAUSE_SAME_SEGMENT_MS)   # chunk of same segment
            elif section == prev_section:
                pauses.append(_PAUSE_SAME_SECTION_MS)   # new segment, same section
            else:
                pauses.append(_PAUSE_SECTION_BREAK_MS)  # section boundary
            parts.append(blob)
        prev_section = section
        if on_segment_done:
            on_segment_done(si + 1, total)

    # ── Stitch ──
    duration_estimated = False
    if containers == {'wav'}:
        master = _tts.concat_wavs(parts, pause_ms=pauses)
        exact = _tts.wav_duration(master)
        mp3 = _transcode_to_mp3(master)
        if mp3 is not None:
            return {'audio_bytes': mp3, 'ext': 'mp3', 'mime': 'audio/mpeg',
                    'duration_sec': exact, 'duration_estimated': False,
                    'tts_model': tts_model, 'voice': voice, 'container': 'mp3'}
        logger.warning('[Paper:Podcast:Audio] ffmpeg unavailable — serving '
                       'WAV master (%.1f MB), export will be large',
                       len(master) / 1e6)
        return {'audio_bytes': master, 'ext': 'wav', 'mime': 'audio/wav',
                'duration_sec': exact, 'duration_estimated': False,
                'tts_model': tts_model, 'voice': voice, 'container': 'wav'}
    if containers == {'mp3'}:
        logger.info('[Paper:Podcast:Audio] provider returned MP3 parts — '
                    'byte-concat (frame-aligned), duration is a 128kbps estimate')
        joined = b''.join(parts)
        return {'audio_bytes': joined, 'ext': 'mp3', 'mime': 'audio/mpeg',
                'duration_sec': _tts.estimate_mp3_duration(joined),
                'duration_estimated': True, 'tts_model': tts_model,
                'voice': voice, 'container': 'mp3'}
    # Mixed/unknown containers: fall back to WAV shape only if everything is
    # wav-sniffable; otherwise byte-concat and mark duration as the script's
    # own estimate (honest flag, never presented as exact).
    logger.warning('[Paper:Podcast:Audio] mixed/unknown containers %s — '
                   'byte-concat fallback, duration from script estimates',
                   sorted(containers))
    joined = b''.join(parts)
    duration_estimated = True
    est = sum((s or {}).get('est_seconds') or 0 for s in segments)
    return {'audio_bytes': joined, 'ext': 'bin',
            'mime': 'application/octet-stream', 'duration_sec': est,
            'duration_estimated': duration_estimated,
            'tts_model': tts_model, 'voice': voice,
            'container': '+'.join(sorted(containers)) or 'unknown'}


__all__ = [
    'AudioSynthesisAborted',
    '_chunk_text',
    '_transcode_to_mp3',
    'synthesize_script_audio',
]
