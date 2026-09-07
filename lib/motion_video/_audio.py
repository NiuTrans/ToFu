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

  * :func:`concat_narrations` — scene WAVs → one narration WAV. Scene WAVs
    already fill their complete program spans, so the engine adds no second
    inter-scene pause (callers may still request one explicitly).
  * :func:`mux_audio_video` — final silent MP4 + narration → final MP4 with
    an AAC track (optional loudnorm single pass), atomic write, probe
    verified (audio present, duration preserved).

Graceful degrade (owner directive, same as podcast): with no tts-capable
slot the narration step reports ``degraded`` and the pipeline ships the
silent video instead of dying.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import threading

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['NarrationAborted', 'synthesize_scene_narrations',
           'concat_narrations', 'mux_audio_video']

#: Silence appended after each scene's narration (loose mode) so the audio
#: never hard-cuts against the scene boundary.
_DEFAULT_TAIL_PAD = 0.35
#: Scene WAVs already include tail padding up to ``target_duration``. Adding a
#: second pause here lengthens the audio beyond the video and breaks alignment.
_SCENE_PAUSE_MS = 0
#: Pause inserted between chunks within one scene.
_CHUNK_PAUSE_MS = 150
#: Per-chunk synthesis attempts (transient provider hiccups).
_CHUNK_RETRIES = 2
_MAX_NARRATION_SCENES = 16
_MAX_NARRATION_CHUNKS = 64
_MAX_SCENE_DURATION_S = 60.0
_MAX_SCENE_AUDIO_BYTES = 32 * 1024 * 1024
_MAX_NARRATION_DISK_BYTES = 192 * 1024 * 1024
_MAX_SCENE_ID_CHARS = 128
_NARRATION_MANIFEST_VERSION = 2

_SENTENCE_END_RE = re.compile(r'[。！？!?；;…\n]|\.(?:\s|$)')


class NarrationAborted(Exception):
    """Raised when the task's abort_event fires mid-synthesis."""


class _NarrationBudgetExceeded(Exception):
    """Raised when provider output exceeds a declared narration boundary."""


def _manifest_request_contract(*, voice, speed, alignment: str,
                               tail_pad: float) -> dict:
    """Canonical inputs that decide whether persisted narration is reusable."""
    try:
        normalised_speed = None if speed is None else float(speed)
        normalised_tail_pad = float(tail_pad)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('speed and tail_pad must be finite numbers') from exc
    if normalised_speed is not None and not math.isfinite(normalised_speed):
        raise ValueError('speed must be finite')
    if not math.isfinite(normalised_tail_pad) or normalised_tail_pad < 0:
        raise ValueError('tail_pad must be a finite non-negative number')
    return {
        'voice': str(voice or ''),
        'speed': normalised_speed,
        'alignment': str(alignment),
        'tail_pad': normalised_tail_pad,
    }


def _scene_text_sha256(text) -> str:
    """Hash the exact normalised text sent to TTS (never persist the text)."""
    return hashlib.sha256(
        str(text or '').strip().encode('utf-8')).hexdigest()


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
    from lib.json_store import write_bytes_atomic
    write_bytes_atomic(path, data)


def _synth_chunk_with_retry(chunk: str, *, voice, fmt, speed,
                            abort_event=None, synthesize_fn=None) -> bytes:
    import lib.tts as _tts  # facade — resolves through lib.tts for test seams
    synthesize_fn = synthesize_fn or _tts.synthesize
    last: Exception | None = None
    for attempt in range(1, _CHUNK_RETRIES + 1):
        if abort_event is not None and abort_event.is_set():
            raise NarrationAborted('aborted before TTS chunk attempt')
        try:
            res = synthesize_fn(chunk, voice=voice, fmt=fmt, speed=speed)
            return res.audio_bytes
        except Exception as e:
            if abort_event is not None and abort_event.is_set():
                raise NarrationAborted(
                    'aborted during TTS chunk attempt') from e
            last = e
            logger.warning('[MotionVideo] TTS chunk attempt %d/%d failed: %s',
                           attempt, _CHUNK_RETRIES, e)
    raise last if last else RuntimeError('TTS chunk failed')


def synthesize_scene_narrations(
        scenes: list[dict], out_dir: str, *, voice: str | None = None,
        speed: float | None = None, alignment: str = 'loose',
        tail_pad: float = _DEFAULT_TAIL_PAD, abort_event=None,
        on_scene_done=None, max_workers: int | None = None,
        owner_user_id: int | None = None,
        tenant_id: str | None = None,
        _synthesize_fn=None) -> dict:
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
    if len(scenes) > _MAX_NARRATION_SCENES:
        return {'ok': False, 'degraded': False,
                'detail': f'{len(scenes)} narration scenes exceed the '
                          f'{_MAX_NARRATION_SCENES}-scene limit'}
    try:
        request_contract = _manifest_request_contract(
            voice=voice, speed=speed, alignment=alignment,
            tail_pad=tail_pad)
    except ValueError as exc:
        return {'ok': False, 'degraded': False, 'detail': str(exc)}
    speed = request_contract['speed']
    tail_pad = request_contract['tail_pad']

    if owner_user_id is None and not _tts.tts_available():
        logger.warning('[MotionVideo] no TTS slot configured — narration degraded')
        return {'ok': False, 'degraded': True,
                'detail': 'no tts-capable slot configured (Settings → providers); '
                          'delivering the silent video path instead'}

    os.makedirs(out_dir, exist_ok=True)
    max_chars = _tts.max_input_chars()
    results: list[dict] = []
    silent_entries: list[tuple[int, dict]] = []
    voiced_plans = []
    total_chunks = 0
    completed_count = 0
    seen_scene_ids: set[str] = set()

    def _scene_settled(scene_id: str) -> None:
        nonlocal completed_count
        completed_count += 1
        if on_scene_done is None:
            return
        try:
            on_scene_done(completed_count, len(scenes), scene_id)
        except Exception as e:
            logger.debug('[MotionVideo] on_scene_done sink failed: %s', e)

    for scene_index, sc in enumerate(scenes):
        if abort_event is not None and abort_event.is_set():
            raise NarrationAborted('aborted before scene '
                                   + str(sc.get('id', '?')))
        scene_id = str(sc.get('id') or f'scene-{len(results) + 1:03d}')
        if (len(scene_id) > _MAX_SCENE_ID_CHARS or scene_id in ('.', '..')
                or '/' in scene_id or '\\' in scene_id or '\x00' in scene_id):
            return {'ok': False, 'degraded': False,
                    'detail': f'unsafe narration scene id {scene_id!r}'}
        if scene_id in seen_scene_ids:
            return {'ok': False, 'degraded': False,
                    'detail': f'duplicate narration scene id {scene_id!r}'}
        seen_scene_ids.add(scene_id)
        text = str(sc.get('text') or '').strip()
        try:
            srt_dur = float(sc.get('end') or 0) - float(sc.get('start') or 0)
        except (TypeError, ValueError, OverflowError):
            return {'ok': False, 'degraded': False,
                    'detail': f'scene {scene_id} start/end must be finite numbers'}
        if (not math.isfinite(srt_dur) or srt_dur < 0
                or srt_dur > _MAX_SCENE_DURATION_S):
            return {'ok': False, 'degraded': False,
                    'detail': f'scene {scene_id} duration {srt_dur:.3f}s is '
                              f'outside 0..{_MAX_SCENE_DURATION_S:.0f}s'}
        entry = {'scene_id': scene_id, 'wav': '', 'text_chars': len(text),
                 'text_sha256': _scene_text_sha256(text),
                 'audio_duration': 0.0, 'srt_duration': round(srt_dur, 3),
                 'target_duration': round(srt_dur, 3), 'overflow': 0.0}
        results.append(entry)
        if not text:
            logger.info('[MotionVideo] scene %s has no text — silence only', scene_id)
            silent_entries.append((scene_index, entry))
            continue
        chunks = _chunk_text(text, max_chars)
        total_chunks += len(chunks)
        voiced_plans.append((scene_index, scene_id, srt_dur, entry, chunks))

    if total_chunks > _MAX_NARRATION_CHUNKS:
        return {'ok': False, 'degraded': False,
                'detail': f'narration requires {total_chunks} TTS chunks; '
                          f'limit is {_MAX_NARRATION_CHUNKS}'}

    if max_workers is None:
        from runtime_guards import resolve_resource_budget
        worker_limit = resolve_resource_budget(
            'TOFU_PRODUCTION_TTS_FANOUT', maximum=8)
    else:
        try:
            requested_workers = int(max_workers)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError('max_workers must be a positive integer') from exc
        if requested_workers < 1:
            raise ValueError('max_workers must be a positive integer')
        worker_limit = min(8, requested_workers)

    # All scene/chunk/resource validation above runs before model-routing
    # access. Share one owner route across the bounded worker pool; the session
    # installs its hard pin independently in each synthesis thread.
    if owner_user_id is not None:
        try:
            with _tts.synthesis_session(owner_user_id, tenant_id) as session:
                return synthesize_scene_narrations(
                    scenes,
                    out_dir,
                    voice=voice,
                    speed=speed,
                    alignment=alignment,
                    tail_pad=tail_pad,
                    abort_event=abort_event,
                    on_scene_done=on_scene_done,
                    max_workers=worker_limit,
                    _synthesize_fn=session.synthesize,
                )
        except _tts.TTSError as exc:
            if exc.status != 503:
                raise
            logger.warning('[MotionVideo] owner has no TTS route — narration '
                           'degraded: %s', exc.detail)
            return {
                'ok': False,
                'degraded': True,
                'detail': 'no owner-authorized tts route configured; '
                          'delivering the silent video path instead',
            }

    artifact_lock = threading.Lock()
    written_paths: set[str] = set()
    reserved_audio_bytes = 0

    def _reserve_audio_bytes(size: int) -> None:
        nonlocal reserved_audio_bytes
        with artifact_lock:
            projected = reserved_audio_bytes + size
            if projected > _MAX_NARRATION_DISK_BYTES:
                raise _NarrationBudgetExceeded(
                    f'narration WAVs retain {projected} bytes; limit is '
                    f'{_MAX_NARRATION_DISK_BYTES}')
            reserved_audio_bytes = projected

    def _record_written(path: str) -> None:
        with artifact_lock:
            written_paths.add(path)

    def _remove_written() -> None:
        with artifact_lock:
            paths = tuple(written_paths)
            written_paths.clear()
        for path in paths:
            try:
                os.unlink(path)
            except FileNotFoundError:
                continue
            except OSError as exc:
                logger.debug('[MotionVideo] narration cleanup failed for %s: %s',
                             path, exc)

    def _render_voiced_scene(plan):
        scene_index, scene_id, srt_dur, entry, chunks = plan
        parts: list[bytes] = []
        retained_bytes = 0
        for chunk_index, chunk in enumerate(chunks):
            if abort_event is not None and abort_event.is_set():
                raise NarrationAborted(f'aborted in scene {scene_id} '
                                       f'chunk {chunk_index + 1}/{len(chunks)}')
            synthesize_kwargs = {
                'voice': voice,
                'fmt': 'wav',
                'speed': speed,
                'abort_event': abort_event,
            }
            if _synthesize_fn is not None:
                synthesize_kwargs['synthesize_fn'] = _synthesize_fn
            blob = _synth_chunk_with_retry(chunk, **synthesize_kwargs)
            retained_bytes += len(blob)
            if retained_bytes > _MAX_SCENE_AUDIO_BYTES:
                raise _NarrationBudgetExceeded(
                    f'scene {scene_id} retains {retained_bytes} audio bytes; '
                    f'limit is {_MAX_SCENE_AUDIO_BYTES}')
            parts.append(blob)
        wav = parts[0] if len(parts) == 1 else _tts.concat_wavs(
            parts, pause_ms=[_CHUNK_PAUSE_MS] * len(parts))
        audio_dur = _tts.wav_duration(wav)
        entry['audio_duration'] = round(audio_dur, 3)
        ch, sw, rate, _frames = _tts.wav_params(wav)
        params = (ch, sw, rate)

        if alignment == 'loose':
            target = max(srt_dur, audio_dur + tail_pad)
        else:  # strict
            target = srt_dur
            if audio_dur > srt_dur:
                entry['overflow'] = round(audio_dur - srt_dur, 3)
                logger.warning('[MotionVideo] scene %s narration overflows '
                               'SRT span by %.2fs (strict mode)', scene_id,
                               entry['overflow'])
        if target > _MAX_SCENE_DURATION_S:
            raise _NarrationBudgetExceeded(
                f'scene {scene_id} target {target:.3f}s exceeds '
                f'{_MAX_SCENE_DURATION_S:.0f}s')
        if audio_dur < target:
            wav = _tts.concat_wavs(
                [wav, _tts.silence_wav_bytes(target - audio_dur,
                                             channels=ch, sampwidth=sw,
                                             framerate=rate)],
                pause_ms=[0, 0])
        if len(wav) > _MAX_SCENE_AUDIO_BYTES:
            raise _NarrationBudgetExceeded(
                f'scene {scene_id} WAV is {len(wav)} bytes; limit is '
                f'{_MAX_SCENE_AUDIO_BYTES}')
        entry['target_duration'] = round(target, 3)
        wav_path = os.path.join(out_dir, f'{scene_id}.wav')
        _reserve_audio_bytes(len(wav))
        _atomic_write(wav_path, wav)
        _record_written(wav_path)
        entry['wav'] = wav_path
        entry['wav_bytes'] = len(wav)
        entry['wav_sha256'] = hashlib.sha256(wav).hexdigest()
        logger.info('[MotionVideo] scene %s narration: %.2fs audio → target %.2fs',
                    scene_id, audio_dur, entry['target_duration'])
        return scene_index, entry, params

    rendered = {}
    try:
        if worker_limit == 1 or len(voiced_plans) <= 1:
            for plan in voiced_plans:
                outcome = _render_voiced_scene(plan)
                rendered[outcome[0]] = outcome
                _scene_settled(outcome[1]['scene_id'])
        elif voiced_plans:
            from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
            queued = list(voiced_plans)
            active = {}
            failure = None

            def _submit_available(pool) -> None:
                while queued and len(active) < worker_limit:
                    plan = queued.pop(0)
                    future = pool.submit(_render_voiced_scene, plan)
                    active[future] = plan[0]

            with ThreadPoolExecutor(
                    max_workers=min(worker_limit, len(voiced_plans)),
                    thread_name_prefix='motion-narration-tts') as pool:
                _submit_available(pool)
                while active:
                    done, _not_done = wait(active, return_when=FIRST_COMPLETED)
                    for future in sorted(done, key=lambda item: active[item]):
                        scene_index = active.pop(future)
                        try:
                            outcome = future.result()
                        except Exception as exc:
                            logger.debug(
                                '[MotionAudio] scene future failed index=%d: %s',
                                scene_index, type(exc).__name__,
                            )
                            failure = failure or exc
                        else:
                            rendered[outcome[0]] = outcome
                            _scene_settled(outcome[1]['scene_id'])
                    if abort_event is not None and abort_event.is_set():
                        failure = NarrationAborted(
                            'aborted during narration batch')
                    if failure is None:
                        _submit_available(pool)
            if failure is not None:
                raise failure
    except _NarrationBudgetExceeded as exc:
        _remove_written()
        return {'ok': False, 'degraded': True, 'detail': str(exc)}
    except BaseException:
        _remove_written()
        raise

    ordered_voiced = [rendered[index] for index in sorted(rendered)]
    ref_params = ordered_voiced[0][2] if ordered_voiced else None
    if ref_params is not None:
        mismatches = [entry['scene_id'] for _index, entry, params
                      in ordered_voiced if params != ref_params]
        if mismatches:
            _remove_written()
            return {'ok': False, 'degraded': True,
                    'detail': 'TTS WAV parameter mismatch in scene(s): '
                              + ', '.join(mismatches)}

    # Second pass: text-less scenes get silence in the PROVIDER's WAV params
    # (falls back to the lib.tts default when no scene carries text at all),
    # so concat_narrations never mixes framerates.
    for _scene_index, entry in silent_entries:
        dur = entry['target_duration']
        if dur <= 0:
            _scene_settled(entry['scene_id'])
            continue
        kwargs = (dict(zip(('channels', 'sampwidth', 'framerate'), ref_params))
                  if ref_params else {})
        wav_path = os.path.join(out_dir, f"{entry['scene_id']}.wav")
        try:
            wav = _tts.silence_wav_bytes(dur, **kwargs)
            if len(wav) > _MAX_SCENE_AUDIO_BYTES:
                raise _NarrationBudgetExceeded(
                    'silent narration WAV exceeds the bounded scene audio '
                    'budget')
            _reserve_audio_bytes(len(wav))
            _atomic_write(wav_path, wav)
            _record_written(wav_path)
        except _NarrationBudgetExceeded as exc:
            _remove_written()
            return {'ok': False, 'degraded': True, 'detail': str(exc)}
        except BaseException:
            _remove_written()
            raise
        entry['wav'] = wav_path
        entry['wav_bytes'] = len(wav)
        entry['wav_sha256'] = hashlib.sha256(wav).hexdigest()
        entry['audio_duration'] = round(dur, 3)
        _scene_settled(entry['scene_id'])

    overflow_total = round(sum(e['overflow'] for e in results), 3)
    return {'ok': True, 'degraded': False,
            'manifest_version': _NARRATION_MANIFEST_VERSION,
            'request': request_contract, 'alignment': alignment,
            'overflow_total': overflow_total, 'scenes': results}


def concat_narrations(wavs: list[str], out_path: str, *,
                      pause_ms: int = _SCENE_PAUSE_MS) -> dict:
    """Concatenate scene narration WAVs into one program-aligned track."""
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

    from lib.json_store import temporary_output_path
    with temporary_output_path(output, suffix='.tmp.mp4') as tmp_out:
        args = [ffmpeg, '-y', '-i', video_path, '-i', audio_path,
                '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy']
        if loudnorm:
            args += ['-af', 'loudnorm=I=-16:TP=-1.5:LRA=11']
        args += ['-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart',
                 tmp_out]
        logger.info('[MotionVideo] mux %s + %s → %s (loudnorm=%s)',
                    video_path, audio_path, output, loudnorm)
        res = _run_cli(args,
                       cwd=os.path.dirname(os.path.abspath(output)) or '.',
                       timeout=timeout, abort_event=abort_event)
        if res['category'] or res['rc'] != 0:
            return {'ok': False, 'category': res['category'] or 'unknown',
                    'detail': res['err'][-1500:]}
        if not os.path.isfile(tmp_out) or os.path.getsize(tmp_out) == 0:
            return {'ok': False, 'category': 'io',
                    'detail': 'ffmpeg produced no output'}

        v_probe = probe_video(video_path)
        f_probe = probe_video(tmp_out)
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
        os.replace(tmp_out, output)
    logger.info('[MotionVideo] mux done: %s', output)
    return {'ok': True, 'output': output,
            'duration': round(float(f_probe.get('duration') or 0), 3),
            'elapsed': round(res['elapsed'], 2)}
