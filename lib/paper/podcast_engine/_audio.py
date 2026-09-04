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
_MAX_SYNTHESIS_CHUNKS = 160
_MAX_AUDIO_PART_BYTES = 32 * 1024 * 1024
_MAX_AUDIO_INPUT_BYTES = 192 * 1024 * 1024

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
                            speed: float | None,
                            abort_check=None,
                            synthesize_fn=None) -> tuple[bytes, str]:
    """Synthesize one chunk; ONE retry on TTSError. Returns (bytes, model)."""
    synthesize_fn = synthesize_fn or _tts.synthesize
    try:
        res = synthesize_fn(chunk, voice=voice, fmt=fmt, speed=speed)
        return res.audio_bytes, res.model
    except _tts.TTSError as _e:
        logger.debug('synth chunk with retry: TTSError (%s)', _e)
        if abort_check and abort_check():
            raise AudioSynthesisAborted() from _e
        try:
            res = synthesize_fn(chunk, voice=voice, fmt=fmt, speed=speed)
        except _tts.TTSError as retry_error:
            if abort_check and abort_check():
                raise AudioSynthesisAborted() from retry_error
            raise
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
                            speed: float | None = None,
                            max_workers: int | None = None,
                            owner_user_id: int | None = None,
                            tenant_id: str | None = None,
                            _synthesize_fn=None) -> dict:
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
    from lib.paper.podcast_prompts import PODCAST_MODES, PODCAST_SEGMENT_LIMITS
    mode = script.get('mode') if script.get('mode') in PODCAST_MODES else 'short'
    segment_limit = PODCAST_SEGMENT_LIMITS[mode]
    if len(segments) > segment_limit:
        raise _tts.TTSError(
            f'{mode} podcast has {len(segments)} segments; limit is '
            f'{segment_limit}', status=400)
    from lib.paper.podcast_engine._validate import estimate_seconds
    estimated_seconds = sum(
        estimate_seconds((segment or {}).get('text') or '')
        for segment in segments)
    duration_limit = PODCAST_MODES[mode][2] * 1.25
    if estimated_seconds > duration_limit:
        raise _tts.TTSError(
            f'{mode} podcast estimates {estimated_seconds:.0f}s; synthesis '
            f'ceiling is {duration_limit:.0f}s', status=400)
    use_fmt = (fmt or '').strip() or _tts.default_format()
    max_chars = _tts.max_input_chars()

    segment_plans = [
        ((segment or {}).get('section') or '',
         _chunk_text((segment or {}).get('text') or '', max_chars))
        for segment in segments
    ]
    chunk_count = sum(len(chunks) for _section, chunks in segment_plans)
    if chunk_count > _MAX_SYNTHESIS_CHUNKS:
        raise _tts.TTSError(
            f'podcast requires {chunk_count} synthesis chunks; limit is '
            f'{_MAX_SYNTHESIS_CHUNKS}', status=400)

    if max_workers is None:
        from runtime_guards import resolve_resource_budget
        worker_limit = resolve_resource_budget(
            'TOFU_PRODUCTION_TTS_FANOUT', maximum=8)
    else:
        try:
            worker_limit = max(1, min(8, int(max_workers)))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError('max_workers must be a positive integer') from exc

    # Validate and plan the full bounded job before touching owner routing.
    # One session is then shared by every segment worker; its synthesize method
    # installs the hard pin separately on each pooled thread.
    if owner_user_id is not None:
        with _tts.synthesis_session(owner_user_id, tenant_id) as session:
            return synthesize_script_audio(
                script,
                voice=voice,
                abort_check=abort_check,
                on_segment_done=on_segment_done,
                fmt=use_fmt,
                speed=speed,
                max_workers=worker_limit,
                _synthesize_fn=session.synthesize,
            )

    def _synthesize_segment(index: int, plan):
        section, chunks = plan
        rows = []
        for chunk in chunks:
            if abort_check and abort_check():
                raise AudioSynthesisAborted()
            synthesize_kwargs = {
                'voice': voice,
                'fmt': use_fmt,
                'speed': speed,
                'abort_check': abort_check,
            }
            if _synthesize_fn is not None:
                synthesize_kwargs['synthesize_fn'] = _synthesize_fn
            blob, model = _synth_chunk_with_retry(chunk, **synthesize_kwargs)
            if len(blob) > _MAX_AUDIO_PART_BYTES:
                raise _tts.TTSError(
                    f'TTS part is {len(blob)} bytes; limit is '
                    f'{_MAX_AUDIO_PART_BYTES}', status=502)
            rows.append((blob, model, _tts.sniff_container(blob)))
        return index, section, rows

    total = len(segment_plans)
    segment_results = [None] * total
    completed_count = 0

    def _record(result) -> None:
        nonlocal completed_count
        index, section, rows = result
        segment_results[index] = (section, rows)
        completed_count += 1
        if on_segment_done:
            on_segment_done(completed_count, total)

    if worker_limit == 1 or total == 1:
        for index, plan in enumerate(segment_plans):
            _record(_synthesize_segment(index, plan))
    else:
        from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
        queued = list(enumerate(segment_plans))
        active = {}
        failure = None

        def _submit_available(pool) -> None:
            while queued and len(active) < worker_limit:
                index, plan = queued.pop(0)
                future = pool.submit(_synthesize_segment, index, plan)
                active[future] = index

        with ThreadPoolExecutor(
                max_workers=min(worker_limit, total),
                thread_name_prefix='paper-podcast-tts') as pool:
            _submit_available(pool)
            while active:
                done, _not_done = wait(active, return_when=FIRST_COMPLETED)
                for future in sorted(done, key=lambda item: active[item]):
                    segment_index = active.pop(future)
                    try:
                        result = future.result()
                    except Exception as exc:
                        logger.debug(
                            '[PaperPodcast] segment future failed index=%d: %s',
                            segment_index, type(exc).__name__,
                        )
                        failure = failure or exc
                    else:
                        _record(result)
                if abort_check and abort_check():
                    failure = AudioSynthesisAborted()
                if failure is None:
                    _submit_available(pool)
        if failure is not None:
            raise failure

    retained_audio_bytes = sum(
        len(blob)
        for result in segment_results
        for blob, _model, _container in result[1]
    )
    if retained_audio_bytes > _MAX_AUDIO_INPUT_BYTES:
        raise _tts.TTSError(
            f'TTS parts retain {retained_audio_bytes} bytes; assembly limit is '
            f'{_MAX_AUDIO_INPUT_BYTES}', status=502)

    parts: list[bytes] = []
    pauses: list[int] = []
    containers: set[str] = set()
    tts_model = ''
    prev_section: str | None = None

    for result in segment_results:
        section, rows = result
        for chunk_index, (blob, model, container) in enumerate(rows):
            tts_model = tts_model or model
            containers.add(container)
            if not parts:
                pauses.append(0)
            elif chunk_index > 0:
                pauses.append(_PAUSE_SAME_SEGMENT_MS)   # chunk of same segment
            elif section == prev_section:
                pauses.append(_PAUSE_SAME_SECTION_MS)   # new segment, same section
            else:
                pauses.append(_PAUSE_SECTION_BREAK_MS)  # section boundary
            parts.append(blob)
        prev_section = section

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
