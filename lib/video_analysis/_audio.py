"""lib/video_analysis/_audio.py — audio track → transcript, on the EXISTING STT chain.

Zero new dependencies (owner ruling 2026-08-04): the audio track is extracted
with ffmpeg (mono 16kHz MP3 ≈ 0.36 MB/min, so even the 15-min P1 cap lands
~5 MB — well under the 25 MiB transcription byte cap) and handed to
:func:`lib.transcription.transcribe`, which routes through the configured
whisper-style / omni-audio slots with its own silence gate, hallucination flag
and Chinese-variant normalization.

Every failure mode degrades to a STATUS, never a pipeline failure — a video
without a transcript is still fully analyzable from its frames:

  * ``no_audio``    — probe found no audio stream
  * ``unavailable`` — no transcription-capable slot configured
  * ``failed``      — extraction or provider error (logged with detail)
  * ``ok``          — transcript attached
"""

from __future__ import annotations

import os

from lib.log import get_logger

logger = get_logger(__name__)


def _extract_audio(video_path: str, scratch_dir: str, duration_s: float) -> tuple[bytes, str] | None:
    """Extract the audio track as (bytes, ext). Tries MP3 (libmp3lame) first,
    falls back to Opus-in-ogg — both are in lib.transcription's allow-list."""
    from lib.motion_video._env import ensure_ffmpeg
    ffmpeg = ensure_ffmpeg(install=True)
    if not ffmpeg:
        logger.warning('[VideoAudio] ffmpeg unavailable — cannot extract audio')
        return None
    attempts = [
        ('mp3', ['-codec:a', 'libmp3lame', '-b:a', '48k']),
        ('ogg', ['-codec:a', 'libopus', '-b:a', '48k']),
    ]
    import subprocess
    for ext, codec_args in attempts:
        out = os.path.join(scratch_dir, f'audio.{ext}')
        cmd = [ffmpeg, '-hide_banner', '-i', video_path, '-vn',
               '-ac', '1', '-ar', '16000', *codec_args, '-y', out]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=max(120.0, duration_s))
        except Exception as e:
            logger.warning('[VideoAudio] audio extract (%s) error: %s', ext, e)
            continue
        if proc.returncode != 0:
            logger.warning('[VideoAudio] audio extract (%s) rc=%s: %.300s',
                           ext, proc.returncode, proc.stderr or '')
            continue
        try:
            with open(out, 'rb') as f:
                data = f.read()
        except Exception as e:
            logger.warning('[VideoAudio] audio read failed (%s): %s', out, e)
            continue
        if data:
            logger.info('[VideoAudio] extracted %d bytes (%s, mono 16kHz)',
                        len(data), ext)
            return data, ext
    return None


def transcribe_track(video_path: str, scratch_dir: str, duration_s: float, *,
                     owner_user_id: int | None = None,
                     tenant_id: str | None = None) -> dict:
    """Extract + transcribe the audio track.

    Returns ``{text, status, model}`` — see the module docstring for statuses.
    Never raises.
    """
    from lib import transcription as stt

    available = (
        stt.transcription_available()
        if owner_user_id is None
        else stt.transcription_available(
            owner_user_id=owner_user_id, tenant_id=tenant_id)
    )
    if not available:
        logger.info('[VideoAudio] no transcription slot configured — skipping')
        return {'text': '', 'status': 'unavailable', 'model': ''}

    extracted = _extract_audio(video_path, scratch_dir, duration_s)
    if not extracted:
        return {'text': '', 'status': 'failed', 'model': ''}
    audio_bytes, ext = extracted

    try:
        if owner_user_id is None:
            result = stt.transcribe(audio_bytes, f'track.{ext}', None)
        else:
            result = stt.transcribe(
                audio_bytes,
                f'track.{ext}',
                None,
                owner_user_id=owner_user_id,
                tenant_id=tenant_id,
            )
    except stt.TranscriptionError as e:
        logger.warning('[VideoAudio] transcription failed: %s (status=%s)',
                       e.detail, e.status)
        return {
            'text': '',
            'status': 'unavailable' if e.status == 503 else 'failed',
            'model': '',
        }
    except Exception as e:
        logger.error('[VideoAudio] transcription unexpected error: %s', e, exc_info=True)
        return {'text': '', 'status': 'failed', 'model': ''}

    logger.info('[VideoAudio] transcript: %d chars via %s', len(result.text), result.model)
    return {'text': result.text, 'status': 'ok', 'model': result.model}
