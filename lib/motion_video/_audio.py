"""lib/motion_video/_audio.py — TTS narration + audio/video mux (P2 音画合成).

The audio half of the motion-video pipeline, reusing :mod:`lib.tts` (the
paper-podcast chain's provider-agnostic TTS) and :mod:`._gates` probing:

  * :func:`synthesize_scene_narrations` — per-scene narration WAVs with
    sentence-boundary chunking, per-chunk retry, cooperative abort, and the
    **alignment** contract (parameterized so the strategy is a config flip,
    not a rewrite):

      - ``'loose'`` (default, audio-led): each scene's *target* duration is
        ``max(srt_duration, audio_duration + tail_pad)`` — short audio is
        silence-padded to the SRT duration; long audio EXTENDS the scene
        (the caller re-renders that scene with the adjusted
        ``data-duration``; trailing time renders as hold/outro per the
        composition contract).
      - ``'strict'`` (srt-led): the scene duration is fixed to the SRT span;
        audio longer than the span is reported as ``overflow`` (the caller
        shortens the scene text or raises the TTS speed) — we never
        time-stretch audio.

  * :func:`concat_narrations` — scene WAVs → one narration WAV (inter-scene
    pause injection via lib.tts helpers).
  * :func:`mux_audio_video` — final silent MP4 + narration → final MP4 with
    an AAC track (optional loudnorm single pass), atomic write, probe
    verified (audio present, duration preserved).

Graceful degrade (owner directive, same as podcast): with no tts-capable
slot the narration step reports ``degraded`` and the pipeline ships the
silent video instead of dying.
"""

from __future__ import annotations

import os
import re
import tempfile

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['NarrationAborted', 'synthesize_scene_narrations',
           'concat_narrations', 'mux_audio_video']

#: Silence appended after each scene's narration (loose mode) so the audio
#: never hard-cuts against the scene boundary.
_DEFAULT_TAIL_PAD = 0.35
#: Pause inserted between scene narrations when concatenating.
_SCENE_PAUSE_MS = 250
#: Pause inserted between chunks within one scene.
_CHUNK_PAUSE_MS = 150
#: Per-chunk synthesis attempts (transient provider hiccups).
_CHUNK_RETRIES = 2

_SENTENCE_END_RE = re.compile(r'[。！？!?；;…\n]|\.(?:\s|$)')


class NarrationAborted(Exception):
    """Raised when the task's abort_event fires mid-synthesis."""


def _chunk_text(text: str, max_chars: int) -> list[str]:
    """Split narration text on sentence boundaries, ≤ ``max_chars`` per chunk.

    Long sentences with no boundary are hard-split at ``max_chars``.
    Mirrors the podcast chain's chunking contract (same provider input
    limit), kept local so motion_video doesn't import from lib.paper.
    """
    text = (text or '').strip()
    if not text:
        return []
    chunks: list[str] = []
    rest = text
    while rest:
        if len(rest) <= max_chars:
            chunks.append(rest)
            break
        window = rest[:max_chars]
        cut = -1
        for m in _SENTENCE_END_RE.finditer(window):
            cut = m.end()
        if cut <= 0:
            cut = max_chars
        chunks.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    return [c for c in chunks if c]


def _atomic_write(path: str, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(prefix='.mv-audio-', dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError as e:
                logger.debug('[MotionVideo] tmp audio cleanup failed: %s', e)


def _synth_chunk_with_retry(chunk: str, *, voice, fmt, speed) -> bytes:
    import lib.tts as _tts  # facade — resolves through lib.tts for test seams
    last: Exception | None = None
    for attempt in range(1, _CHUNK_RETRIES + 1):
        try:
            res = _tts.synthesize(chunk, voice=voice, fmt=fmt, speed=speed)
            return res.audio_bytes
        except Exception as e:
            last = e
            logger.warning('[MotionVideo] TTS chunk attempt %d/%d failed: %s',
                           attempt, _CHUNK_RETRIES, e)
    raise last if last else RuntimeError('TTS chunk failed')


def synthesize_scene_narrations(
        scenes: list[dict], out_dir: str, *, voice: str | None = None,
        speed: float | None = None, alignment: str = 'loose',
        tail_pad: float = _DEFAULT_TAIL_PAD, abort_event=None,
        on_scene_done=None) -> dict:
    """Synthesize per-scene narration WAVs + the alignment manifest.

    Args:
        scenes: storyboard scenes (``id`` / ``start`` / ``end`` / ``text``).
        out_dir: directory for ``<scene-id>.wav`` outputs (created).
        voice / speed: TTS overrides (None → data/config/tts.json defaults).
        alignment: ``'loose'`` (audio-led, default) or ``'strict'`` (srt-led).
        tail_pad: seconds of silence appended after narration (loose mode).
        abort_event: optional threading.Event — checked between chunks.
        on_scene_done: optional ``fn(index, total, scene_id)`` called as each
            scene's narration is settled (P-UX3 per-scene progress events).

    Returns ``{'ok', 'degraded', 'alignment', 'scenes': [{scene_id, wav,
    text_chars, audio_duration, srt_duration, target_duration, overflow}]}``.
    Per-scene ``target_duration`` is what the scene's ``data-duration``
    must become (== srt duration in strict mode or when audio fits).
    """
    import lib.tts as _tts

    if alignment not in ('loose', 'strict'):
        return {'ok': False, 'degraded': False,
                'detail': f'invalid alignment {alignment!r} (loose|strict)'}
    if not scenes:
        return {'ok': False, 'degraded': False, 'detail': 'no scenes'}

    if not _tts.tts_available():
        logger.warning('[MotionVideo] no TTS slot configured — narration degraded')
        return {'ok': False, 'degraded': True,
                'detail': 'no tts-capable slot configured (Settings → providers); '
                          'delivering the silent video path instead'}

    os.makedirs(out_dir, exist_ok=True)
    max_chars = _tts.max_input_chars()
    results: list[dict] = []
    silent_entries: list[dict] = []
    ref_params: tuple | None = None  # (channels, sampwidth, framerate) of provider WAVs

    def _scene_settled(scene_id: str) -> None:
        if on_scene_done is None:
            return
        try:
            on_scene_done(len(results), len(scenes), scene_id)
        except Exception as e:
            logger.debug('[MotionVideo] on_scene_done sink failed: %s', e)

    for sc in scenes:
        if abort_event is not None and abort_event.is_set():
            raise NarrationAborted('aborted before scene '
                                   + str(sc.get('id', '?')))
        scene_id = str(sc.get('id') or f'scene-{len(results) + 1:03d}')
        text = str(sc.get('text') or '').strip()
        srt_dur = float(sc.get('end') or 0) - float(sc.get('start') or 0)
        entry = {'scene_id': scene_id, 'wav': '', 'text_chars': len(text),
                 'audio_duration': 0.0, 'srt_duration': round(srt_dur, 3),
                 'target_duration': round(srt_dur, 3), 'overflow': 0.0}
        results.append(entry)
        if not text:
            logger.info('[MotionVideo] scene %s has no text — silence only', scene_id)
            silent_entries.append(entry)
            _scene_settled(scene_id)
            continue

        parts: list[bytes] = []
        chunks = _chunk_text(text, max_chars)
        for ci, chunk in enumerate(chunks):
            if abort_event is not None and abort_event.is_set():
                raise NarrationAborted(f'aborted in scene {scene_id} '
                                       f'chunk {ci + 1}/{len(chunks)}')
            parts.append(_synth_chunk_with_retry(chunk, voice=voice,
                                                 fmt='wav', speed=speed))
        wav = parts[0] if len(parts) == 1 else _tts.concat_wavs(
            parts, pause_ms=[_CHUNK_PAUSE_MS] * len(parts))
        audio_dur = _tts.wav_duration(wav)
        entry['audio_duration'] = round(audio_dur, 3)
        ch, sw, rate, _frames = _tts.wav_params(wav)
        if ref_params is None:
            ref_params = (ch, sw, rate)

        if alignment == 'loose':
            target = max(srt_dur, audio_dur + tail_pad)
        else:  # strict
            target = srt_dur
            if audio_dur > srt_dur:
                entry['overflow'] = round(audio_dur - srt_dur, 3)
                logger.warning('[MotionVideo] scene %s narration overflows '
                               'SRT span by %.2fs (strict mode)', scene_id,
                               entry['overflow'])
        if audio_dur < target:
            wav = _tts.concat_wavs(
                [wav, _tts.silence_wav_bytes(target - audio_dur,
                                             channels=ch, sampwidth=sw,
                                             framerate=rate)],
                pause_ms=[0, 0])
        entry['target_duration'] = round(target, 3)
        wav_path = os.path.join(out_dir, f'{scene_id}.wav')
        _atomic_write(wav_path, wav)
        entry['wav'] = wav_path
        logger.info('[MotionVideo] scene %s narration: %.2fs audio → target %.2fs',
                    scene_id, audio_dur, entry['target_duration'])
        _scene_settled(scene_id)

    # Second pass: text-less scenes get silence in the PROVIDER's WAV params
    # (falls back to the lib.tts default when no scene carries text at all),
    # so concat_narrations never mixes framerates.
    for entry in silent_entries:
        dur = entry['target_duration']
        if dur <= 0:
            continue
        kwargs = (dict(zip(('channels', 'sampwidth', 'framerate'), ref_params))
                  if ref_params else {})
        wav = _tts.silence_wav_bytes(dur, **kwargs)
        wav_path = os.path.join(out_dir, f"{entry['scene_id']}.wav")
        _atomic_write(wav_path, wav)
        entry['wav'] = wav_path
        entry['audio_duration'] = round(dur, 3)

    overflow_total = round(sum(e['overflow'] for e in results), 3)
    return {'ok': True, 'degraded': False, 'alignment': alignment,
            'overflow_total': overflow_total, 'scenes': results}


def concat_narrations(wavs: list[str], out_path: str, *,
                      pause_ms: int = _SCENE_PAUSE_MS) -> dict:
    """Concatenate scene narration WAVs into one narration track."""
    import lib.tts as _tts

    if not wavs:
        return {'ok': False, 'detail': 'no narration wavs'}
    parts: list[bytes] = []
    for p in wavs:
        try:
            with open(p, 'rb') as f:
                parts.append(f.read())
        except OSError as e:
            logger.debug('concat narrations: unreadable (%s)', e)
            return {'ok': False, 'detail': f'cannot read {p}: {e}'}
    merged = parts[0] if len(parts) == 1 else _tts.concat_wavs(
        parts, pause_ms=[pause_ms] * len(parts))
    _atomic_write(out_path, merged)
    duration = _tts.wav_duration(merged)
    logger.info('[MotionVideo] narration track: %s (%.2fs, %d scene(s))',
                out_path, duration, len(parts))
    return {'ok': True, 'output': out_path, 'duration': round(duration, 3)}


def mux_audio_video(video_path: str, audio_path: str, output: str, *,
                    loudnorm: bool = True, timeout: int = 900,
                    abort_event=None) -> dict:
    """Mux the silent final MP4 with the narration track → deliverable MP4.

    Video stream is copied (no re-encode); audio → AAC (optionally through
    a single-pass loudnorm). Atomic output; post-probe verifies an audio
    track exists and the duration is preserved (±0.5s).
    """
    from lib.motion_video._env import ffmpeg_bin
    from lib.motion_video._gates import probe_video
    from lib.motion_video._render import _run_cli  # shared timeout/abort runner

    for p, label in ((video_path, 'video'), (audio_path, 'audio')):
        if not os.path.isfile(p):
            return {'ok': False, 'category': 'io',
                    'detail': f'missing {label} file: {p}'}
    ffmpeg = ffmpeg_bin()
    if not ffmpeg:
        return {'ok': False, 'category': 'env_missing',
                'detail': 'ffmpeg not found (run motion_video_env_check)'}

    tmp_out = output + '.tmp.mp4'
    args = [ffmpeg, '-y', '-i', video_path, '-i', audio_path,
            '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy']
    if loudnorm:
        args += ['-af', 'loudnorm=I=-16:TP=-1.5:LRA=11']
    args += ['-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart', tmp_out]
    logger.info('[MotionVideo] mux %s + %s → %s (loudnorm=%s)',
                video_path, audio_path, output, loudnorm)
    res = _run_cli(args, cwd=os.path.dirname(os.path.abspath(output)) or '.',
                   timeout=timeout, abort_event=abort_event)
    if res['category'] or res['rc'] != 0:
        return {'ok': False, 'category': res['category'] or 'unknown',
                'detail': res['err'][-1500:]}
    if not os.path.isfile(tmp_out) or os.path.getsize(tmp_out) == 0:
        return {'ok': False, 'category': 'io',
                'detail': 'ffmpeg produced no output'}
    os.replace(tmp_out, output)

    v_probe = probe_video(video_path)
    f_probe = probe_video(output)
    if f_probe is None:
        return {'ok': False, 'category': 'io',
                'detail': 'post-mux probe failed'}
    if not f_probe.get('has_audio'):
        return {'ok': False, 'category': 'io',
                'detail': 'muxed MP4 has no audio track'}
    if v_probe:
        dv = abs(float(f_probe.get('duration') or 0)
                 - float(v_probe.get('duration') or 0))
        if dv > 0.5:
            return {'ok': False, 'category': 'io',
                    'detail': f'muxed duration drifted {dv:.3f}s from the video'}
    logger.info('[MotionVideo] mux done: %s', output)
    return {'ok': True, 'output': output,
            'duration': round(float(f_probe.get('duration') or 0), 3),
            'elapsed': round(res['elapsed'], 2)}
