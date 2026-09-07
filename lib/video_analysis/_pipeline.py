"""lib/video_analysis/_pipeline.py — the upload-time processing orchestrator.

Runs ENTIRELY at upload time in a daemon thread (owner ruling 2026-08-04:
send must never block on processing). Stages, each recorded as a registry
phase for the polling status endpoint:

    probe    — codec/duration/audio-stream facts (reuses motion_video's
               probe_video, the in-tree ffprobe/ffmpeg-fallback SSOT)
    frames   — uniform+scene extraction into bounded local scratch
    audio    — optional transcript via the existing lib.transcription chain
    done     — one atomic replacement commits frames + transcript metadata

Scratch (the uploaded file + decoded frames) lives on LOCAL disk only —
never decoded on the FUSE mount — and is removed in ``finally``.
"""

from __future__ import annotations

import os
import shutil
import threading
from collections.abc import Callable

from lib.log import get_logger
from lib.runtime_paths import uploads_root

from lib.video_analysis import _store
from lib.video_analysis._audio import transcribe_track
from lib.video_analysis._config import (
    TRANSCRIPT_CHAR_CAP,
    video_max_duration_s,
)
from lib.video_analysis._frames import extract_frames

logger = get_logger(__name__)


def _processing_capacity() -> int:
    """Use the launch-probed task budget with a hard video-worker ceiling."""
    from runtime_guards import resolve_resource_budget

    return resolve_resource_budget('TOFU_MAX_INFLIGHT_TASKS', maximum=2)


PROCESSING_CAPACITY = _processing_capacity()
_PROCESSING_SLOTS = threading.BoundedSemaphore(PROCESSING_CAPACITY)


class ProcessingReservation:
    """One upload/analysis slot, transferable exactly once to its worker."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._route_owns_slot = True

    def release(self) -> None:
        with self._lock:
            if not self._route_owns_slot:
                return
            self._route_owns_slot = False
        _PROCESSING_SLOTS.release()

    def handoff(self) -> Callable[[], None]:
        with self._lock:
            if not self._route_owns_slot:
                raise RuntimeError('video processing reservation is not active')
            self._route_owns_slot = False
        released = False
        release_lock = threading.Lock()

        def release_from_worker() -> None:
            nonlocal released
            with release_lock:
                if released:
                    return
                released = True
            _PROCESSING_SLOTS.release()

        return release_from_worker


def reserve_processing_slot() -> ProcessingReservation | None:
    """Reserve bounded receive+analysis capacity without creating a queue."""
    if not _PROCESSING_SLOTS.acquire(blocking=False):
        return None
    return ProcessingReservation()


def videos_dir() -> str:
    path = os.path.join(uploads_root(), 'videos')
    os.makedirs(path, exist_ok=True)
    return path


def start_processing(video_id: str, scratch_path: str, original_name: str,
                     *, user_id: int,
                     reservation: ProcessingReservation | None = None) -> bool:
    """Spawn the background processing thread (daemon — dies with the process;
    a killed server leaves the record in ``processing``, which the status
    endpoint reports as failed-after-the-fact via the stale sweep)."""
    active_reservation = reservation or reserve_processing_slot()
    if active_reservation is None:
        logger.warning('[VideoPipeline] processing capacity exhausted')
        return False
    release_slot = active_reservation.handoff()
    try:
        t = threading.Thread(
            target=_process_guarded,
            args=(video_id, scratch_path, original_name, user_id, release_slot),
            name=f'video-analysis-{video_id[-8:]}',
            daemon=True,
        )
        t.start()
    except BaseException:
        release_slot()
        raise
    logger.info('[VideoPipeline] %s started for %s', video_id, original_name)
    return True


def _process_guarded(video_id: str, scratch_path: str, original_name: str,
                     user_id: int,
                     release_slot: Callable[[], None] | None = None) -> None:
    try:
        _process(video_id, scratch_path, original_name, user_id=user_id)
    except Exception as e:
        logger.error('[VideoPipeline] %s crashed: %s', video_id, e, exc_info=True)
        try:
            from lib.media_attachments import mark_failed
            mark_failed(video_id, f'internal error: {e}', user_id=user_id)
        except Exception as metadata_error:
            logger.warning('[VideoPipeline] durable failure update failed: %s',
                           metadata_error)
        _store.fail_record(video_id, f'internal error: {e}')
    finally:
        scratch_dir = os.path.dirname(scratch_path)
        try:
            shutil.rmtree(scratch_dir, ignore_errors=True)
        except Exception as e:
            logger.warning('[VideoPipeline] scratch cleanup failed (%s): %s', scratch_dir, e)
        if release_slot is not None:
            release_slot()


def _process(video_id: str, scratch_path: str, original_name: str, *,
             user_id: int) -> None:
    from lib.motion_video._gates import probe_video
    from lib.media_attachments import complete_video, mark_failed, set_phase

    def phase(value: str) -> None:
        _store.set_phase(video_id, value)
        set_phase(video_id, value, user_id=user_id)

    def fail(message: str) -> None:
        mark_failed(video_id, message, user_id=user_id)
        _store.fail_record(video_id, message)

    # ── probe ──
    phase('probe')
    probe = probe_video(scratch_path)
    if not probe or not probe.get('codec'):
        fail('not a readable video file')
        return
    duration_s = float(probe.get('duration') or 0)
    if duration_s <= 0:
        fail('could not determine video duration')
        return
    max_dur = video_max_duration_s()
    if duration_s > max_dur:
        fail(f'video too long ({duration_s:.0f}s, max {max_dur:.0f}s)')
        return

    # ── frames ──
    phase('frames')
    scratch_dir = os.path.dirname(scratch_path)
    frames = extract_frames(scratch_path, duration_s, scratch_dir)
    if not frames:
        fail('frame extraction produced no frames')
        return
    _store.update_record(video_id, frame_count=len(frames))

    # ── visual storyboard (owner ruling 2026-08-04: model-agnostic — narrate
    #    the frames once via the pool's vision slot so a TEXT-ONLY chat model
    #    still gets the visual channel; unused when the chat model has vision)
    phase('storyboard')
    from lib.video_analysis._caption import storyboard_for_frames
    sb = storyboard_for_frames(
        frames,
        name=original_name,
        duration_s=duration_s,
        owner_user_id=user_id,
    )
    storyboard = sb['text']
    if len(storyboard) > TRANSCRIPT_CHAR_CAP:
        storyboard = storyboard[:TRANSCRIPT_CHAR_CAP] + '\n[storyboard truncated]'

    # ── audio transcript (optional, degrades to a status) ──
    phase('audio')
    if probe.get('has_audio'):
        tr = transcribe_track(
            scratch_path,
            scratch_dir,
            duration_s,
            owner_user_id=user_id,
        )
    else:
        tr = {'text': '', 'status': 'no_audio', 'model': ''}
    transcript = tr['text']
    if len(transcript) > TRANSCRIPT_CHAR_CAP:
        transcript = transcript[:TRANSCRIPT_CHAR_CAP] + '\n[transcript truncated]'
        logger.info('[VideoPipeline] transcript truncated at %d chars', TRANSCRIPT_CHAR_CAP)

    probe = {**probe, 'filename': original_name}
    attachment = complete_video(
        video_id, frames=frames, transcript=transcript,
        transcript_status=tr['status'], transcript_model=tr['model'],
        storyboard=storyboard, storyboard_status=sb['status'],
        storyboard_model=sb['model'], probe=probe, user_id=user_id)
    if attachment is None:
        fail('attachment disappeared during processing')
        return
    record_projection = {
        key: value for key, value in attachment.items()
        if key not in {'status', 'phase'}
    }
    _store.complete_record(
        video_id, attachment=attachment, **record_projection)
    logger.info('[VideoPipeline] %s ready: %.1fs %sx%s, %d frames (~%dB each), '
                'transcript=%s(%d chars)',
                video_id, duration_s, probe.get('width'), probe.get('height'),
                len(frames), int(attachment.get('avgFrameBytes') or 0),
                tr['status'], len(transcript))
