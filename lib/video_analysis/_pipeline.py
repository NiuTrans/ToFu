"""lib/video_analysis/_pipeline.py — the upload-time processing orchestrator.

Runs ENTIRELY at upload time in a daemon thread (owner ruling 2026-08-04:
send must never block on processing). Stages, each recorded as a registry
phase for the polling status endpoint:

    probe    — codec/duration/audio-stream facts (reuses motion_video's
               probe_video, the in-tree ffprobe/ffmpeg-fallback SSOT)
    persist  — copy the original upload from local scratch into the durable
               uploads/videos store (served for playback; P2 Gemini native
               passthrough needs the real file)
    frames   — uniform+scene extraction → durable /api/images/ URLs
    audio    — optional transcript via the existing lib.transcription chain
    done     — registry record carries the full self-contained payload

Scratch (the uploaded file + decoded frames) lives on LOCAL disk only —
never decoded on the FUSE mount — and is removed in ``finally``.
"""

from __future__ import annotations

import os
import shutil
import threading
import time

from lib.log import get_logger
from lib.runtime_paths import uploads_root

from lib.video_analysis import _store
from lib.video_analysis._audio import transcribe_track
from lib.video_analysis._config import (
    TRANSCRIPT_CHAR_CAP,
    video_max_duration_s,
)
from lib.video_analysis._frames import extract_frames, persist_frames

logger = get_logger(__name__)


def videos_dir() -> str:
    path = os.path.join(uploads_root(), 'videos')
    os.makedirs(path, exist_ok=True)
    return path


def start_processing(video_id: str, scratch_path: str, original_name: str,
                     *, user_id: int) -> None:
    """Spawn the background processing thread (daemon — dies with the process;
    a killed server leaves the record in ``processing``, which the status
    endpoint reports as failed-after-the-fact via the stale sweep)."""
    t = threading.Thread(
        target=_process_guarded,
        args=(video_id, scratch_path, original_name, user_id),
        name=f'video-analysis-{video_id[-8:]}',
        daemon=True,
    )
    t.start()
    logger.info('[VideoPipeline] %s started for %s', video_id, original_name)


def _process_guarded(video_id: str, scratch_path: str, original_name: str,
                     user_id: int) -> None:
    try:
        _process(video_id, scratch_path, original_name, user_id=user_id)
    except Exception as e:
        logger.error('[VideoPipeline] %s crashed: %s', video_id, e, exc_info=True)
        _store.fail_record(video_id, f'internal error: {e}')
    finally:
        scratch_dir = os.path.dirname(scratch_path)
        try:
            shutil.rmtree(scratch_dir, ignore_errors=True)
        except Exception as e:
            logger.warning('[VideoPipeline] scratch cleanup failed (%s): %s', scratch_dir, e)


def _process(video_id: str, scratch_path: str, original_name: str, *,
             user_id: int) -> None:
    from lib.motion_video._gates import probe_video

    # ── probe ──
    _store.set_phase(video_id, 'probe')
    probe = probe_video(scratch_path)
    if not probe or not probe.get('codec'):
        _store.fail_record(video_id, 'not a readable video file')
        return
    duration_s = float(probe.get('duration') or 0)
    if duration_s <= 0:
        _store.fail_record(video_id, 'could not determine video duration')
        return
    max_dur = video_max_duration_s()
    if duration_s > max_dur:
        _store.fail_record(
            video_id, f'video too long ({duration_s:.0f}s, max {max_dur:.0f}s)')
        return

    # ── persist the original (durable, for playback + P2 passthrough) ──
    _store.set_phase(video_id, 'persist')
    ext = os.path.splitext(original_name)[1].lower() or '.mp4'
    stored_name = f"{int(time.time() * 1000)}_{os.urandom(4).hex()}{ext}"
    stored_path = os.path.join(videos_dir(), stored_name)
    try:
        shutil.copyfile(scratch_path, stored_path)
        _store.register_video_asset(
            stored_name,
            video_id=video_id,
            user_id=user_id,
        )
    except Exception as e:
        try:
            os.unlink(stored_path)
        except OSError:
            pass
        logger.error('[VideoPipeline] persist original failed: %s', e, exc_info=True)
        _store.fail_record(video_id, 'could not store the video file')
        return
    video_url = f'/api/videos/{stored_name}'

    # ── frames ──
    _store.set_phase(video_id, 'frames')
    scratch_dir = os.path.dirname(scratch_path)
    frames = extract_frames(scratch_path, duration_s, scratch_dir)
    if not frames:
        _store.fail_record(video_id, 'frame extraction produced no frames')
        return
    images_dir = os.path.join(uploads_root(), 'images')
    os.makedirs(images_dir, exist_ok=True)
    persisted = persist_frames(frames, images_dir)
    if not persisted:
        _store.fail_record(video_id, 'could not store extracted frames')
        return
    avg_frame_bytes = int(sum(f['bytes'] for f in persisted) / len(persisted))
    _store.update_record(video_id, frame_count=len(persisted))

    # ── visual storyboard (owner ruling 2026-08-04: model-agnostic — narrate
    #    the frames once via the pool's vision slot so a TEXT-ONLY chat model
    #    still gets the visual channel; unused when the chat model has vision)
    _store.set_phase(video_id, 'storyboard')
    from lib.video_analysis._caption import storyboard_for_frames
    sb = storyboard_for_frames(persisted, name=original_name, duration_s=duration_s)
    storyboard = sb['text']
    if len(storyboard) > TRANSCRIPT_CHAR_CAP:
        storyboard = storyboard[:TRANSCRIPT_CHAR_CAP] + '\n[storyboard truncated]'

    # ── audio transcript (optional, degrades to a status) ──
    _store.set_phase(video_id, 'audio')
    if probe.get('has_audio'):
        tr = transcribe_track(scratch_path, scratch_dir, duration_s)
    else:
        tr = {'text': '', 'status': 'no_audio', 'model': ''}
    transcript = tr['text']
    if len(transcript) > TRANSCRIPT_CHAR_CAP:
        transcript = transcript[:TRANSCRIPT_CHAR_CAP] + '\n[transcript truncated]'
        logger.info('[VideoPipeline] transcript truncated at %d chars', TRANSCRIPT_CHAR_CAP)

    _store.complete_record(
        video_id,
        name=original_name,
        video_url=video_url,
        duration_s=round(duration_s, 2),
        width=int(probe.get('width') or 0),
        height=int(probe.get('height') or 0),
        fps=probe.get('fps') or 0,
        poster=persisted[0]['url'],
        frames=persisted,
        frame_count=len(persisted),
        avg_frame_bytes=avg_frame_bytes,
        transcript=transcript,
        transcript_status=tr['status'],
        transcript_model=tr['model'],
        storyboard=storyboard,
        storyboard_status=sb['status'],
        storyboard_model=sb['model'],
    )
    logger.info('[VideoPipeline] %s ready: %.1fs %sx%s, %d frames (~%dB each), '
                'transcript=%s(%d chars)',
                video_id, duration_s, probe.get('width'), probe.get('height'),
                len(persisted), avg_frame_bytes, tr['status'], len(transcript))
