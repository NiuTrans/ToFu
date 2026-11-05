"""lib/video_analysis/_inspect.py — on-demand, read-only video inspection.

The tool-time sibling of the upload pipeline (``_pipeline``): one local video
(project file, server-staged download, or resolved chat attachment) becomes
probe facts, timestamped key frames as in-memory JPEG images, and an optional
transcript. An audio-only file (no video stream) becomes probe facts and the
transcript as text, without frames. It reuses the pipeline owners unchanged —
motion_video's ``probe_video``, :func:`extract_frames` (duration-derived
uniform cadence plus
scene cuts, scaled at extraction to ``FRAME_LONG_SIDE_PX``) and
:func:`transcribe_track` — and the per-model send budget
(``lib.model_info.video_frame_budget`` + ``_thin_frames``) that the media
projection applies to uploaded videos.

Like the pipeline, decoding happens only on a private local copy inside a
``job_*`` directory under :func:`scratch_root` (never on the FUSE mount) that
is removed in ``finally``. An optional window is cut from that copy with the
same ffmpeg owner, then the same functions run on the cut segment. Nothing is
persisted. Failures raise :class:`VideoInspectError` (stable code + next
action); the tool layer renders it as a typed tool error.

Entry point: :func:`inspect_video_file`. Seams resolve through the
``lib.video_analysis`` facade so tests patch them there.
"""

from __future__ import annotations

import base64
import math
import mimetypes
import os
import shutil
import tempfile

from lib.log import get_logger

logger = get_logger(__name__)

_WINDOW_NEXT_ACTION = (
    'Pass start/end as seconds from the beginning of the file (e.g. start=90 '
    'for 1:30) with 0 <= start < end; omit both to inspect the whole file.')


class VideoInspectError(Exception):
    """A typed inspection failure with a stable code and a recovery action."""

    def __init__(self, code: str, message: str, next_action: str, *,
                 retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.next_action = next_action
        self.retryable = retryable


def _invalid_window(message: str) -> VideoInspectError:
    return VideoInspectError('video_invalid_window', message, _WINDOW_NEXT_ACTION)


def _seconds(value, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise _invalid_window(f'{name} must be a number of seconds, got {value!r}.')
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise _invalid_window(
            f'{name} must be a number of seconds, got {value!r}.') from None
    if not math.isfinite(seconds) or seconds < 0:
        raise _invalid_window(
            f'{name} must be a finite number of seconds >= 0, got {value!r}.')
    return seconds


def _span(start: float, end: float) -> str:
    from lib.video_analysis._frames import _fmt_video_ts
    return (f'{_fmt_video_ts(start)}–{_fmt_video_ts(end)} '
            f'({start:.2f}s–{end:.2f}s)')


def _claimed_media(name: str) -> str:
    """'video' or 'audio' when the file name claims that media type, else ''."""
    from lib.video_analysis._config import AUDIO_EXTS, VIDEO_EXTS
    ext = os.path.splitext(name)[1].lower()
    guessed = mimetypes.guess_type(name)[0] or ''
    if ext in VIDEO_EXTS or guessed.startswith('video/'):
        return 'video'
    if ext in AUDIO_EXTS or guessed.startswith('audio/'):
        return 'audio'
    return ''


def _undecodable(name: str) -> VideoInspectError:
    claimed = _claimed_media(name)
    if claimed == 'video':
        return VideoInspectError(
            'video_unreadable',
            f'{name} could not be decoded as video (corrupt, truncated, or an '
            'unsupported codec).',
            'Verify the file is a complete video — a failed download often saves '
            'an HTML error page under a video name. Re-download or re-stage it, '
            'then retry.')
    if claimed == 'audio':
        return VideoInspectError(
            'video_unreadable',
            f'{name} could not be decoded as audio (corrupt, truncated, or an '
            'unsupported codec).',
            'Verify the file is a complete audio file — a failed download often '
            'saves an HTML error page under an audio name. Re-download or '
            're-stage it, then retry.')
    return VideoInspectError(
        'video_not_a_video',
        f'{name} is not a video or audio file: no decodable video or audio stream '
        'with a duration.',
        'Pass a video (mp4/mov/webm/mkv/avi…) or audio file (mp3/m4a/wav/ogg/'
        'flac…). Use read_files for documents and inspect_image for still images.')


def _probe_facts(probe: dict, duration: float) -> str:
    from lib.video_analysis._frames import _fmt_video_ts
    facts = [f'duration {duration:.2f}s ({_fmt_video_ts(duration)})']
    if probe.get('codec'):
        if probe.get('width') and probe.get('height'):
            facts.append(f'{probe["width"]}x{probe["height"]}')
        fps = float(probe.get('fps') or 0)
        if fps > 0:
            facts.append(f'{fps:g} fps')
        facts.append(f'video codec {probe.get("codec")}')
    else:
        facts.append('video no')
    if probe.get('has_audio'):
        codec = probe.get('audio_codec')
        facts.append(f'audio yes ({codec})' if codec else 'audio yes')
    else:
        facts.append('audio no')
    return 'Probe: ' + ' · '.join(facts)


def _cut_window(ffmpeg: str, source: str, start: float, end: float,
                scratch_dir: str, *, audio_only: bool = False) -> str:
    """Cut [start, end) from the local copy into a frame-accurate segment.

    Re-encoding (not stream copy) keeps the cut exact instead of snapping to
    the previous keyframe; mpeg4/pcm are ffmpeg-native encoders, available in
    every build, and the segment is a transient intermediate. The extraction
    owner's scale filter bounds it for high-resolution sources. An audio-only
    source keeps just its first audio stream.
    """
    from lib.video_analysis._frames import _SCALE_FILTER, _run
    segment = os.path.join(scratch_dir, 'window.mkv')
    streams = (['-map', '0:a:0'] if audio_only else
               ['-map', '0:v:0', '-map', '0:a:0?', '-vf', _SCALE_FILTER,
                '-c:v', 'mpeg4', '-q:v', '2'])
    try:
        _run([
            ffmpeg, '-hide_banner', '-ss', f'{start:.3f}', '-i', source,
            '-t', f'{end - start:.3f}', *streams,
            '-c:a', 'pcm_s16le', '-y', segment,
        ], timeout=max(180.0, (end - start) * 2), what='window cut')
    except RuntimeError as error:
        raise VideoInspectError(
            'video_unreadable', f'The requested window could not be decoded: {error}',
            'Retry without start/end, or re-encode the source '
            f'{"audio" if audio_only else "video"}.') from error
    return segment


def _transcript_lines(probe: dict, decode_path: str, scratch_dir: str,
                      start: float, end: float, owner_user_id) -> tuple[list[str], str]:
    if not probe.get('has_audio'):
        return ['Transcript: unavailable (no audio track)'], 'no_audio'
    import lib.video_analysis as va
    track = va.transcribe_track(decode_path, scratch_dir, end - start,
                                owner_user_id=owner_user_id)
    status = track.get('status')
    text = str(track.get('text') or '').strip()
    if status == 'ok' and text:
        via = f' (via {track["model"]})' if track.get('model') else ''
        return [f'Transcript{via}:', f'[{_span(start, end)}] {text}'], 'ok'
    if status == 'ok':
        return ['Transcript: unavailable (no speech detected)'], 'empty'
    detail = str(track.get('detail') or '').strip()
    if status == 'unavailable':
        reason = detail or 'no transcription model is configured'
    else:
        reason = f'transcription failed: {detail or "see the server log"}'
    return [f'Transcript: unavailable ({reason})'], str(status or 'failed')


def inspect_video_file(path: str, *, start=None, end=None,
                       owner_user_id: int | None = None, model: str = '',
                       source_name: str = '') -> dict | str:
    """Inspect one local video or audio-only file: probe facts, key frames
    (video only) and transcript.

    Args:
        path: Absolute path to an existing local file (already resolved and
            authorized by the caller).
        start / end: Optional window in seconds; frames and transcript cover
            only ``[start, end]``. ``end`` past the video is clamped.
        owner_user_id: Owner whose transcription routing applies.
        model: Active chat model; selects the per-model frame budget. Empty
            means no model-specific thinning.
        source_name: Display name (an attachment's original name).

    Returns:
        ``{'__batch_images__': {i: frame}, '_text_content': str, ...}`` — the
        read_files multi-image shape — or plain text when the model has no
        vision support or the file has no video stream (no frames are
        extracted then).

    Raises:
        VideoInspectError: disabled feature, invalid window, missing file,
            missing ffmpeg, non-media or undecodable media.
    """
    import lib.video_analysis as va
    from lib.video_analysis._config import FRAME_LONG_SIDE_PX, mark_scratch_owner
    from lib.video_analysis._frames import _ffmpeg, _fmt_video_ts, _thin_frames

    if not va.video_analysis_enabled():
        raise VideoInspectError(
            'video_analysis_disabled',
            'Video analysis is disabled on this server (TOFU_VIDEO_ANALYSIS=0).',
            'Tell the user video inspection is disabled; an operator can unset '
            'TOFU_VIDEO_ANALYSIS to enable it. Continue with other evidence.')
    start_s = _seconds(start, 'start')
    end_s = _seconds(end, 'end')
    if start_s is not None and end_s is not None and end_s <= start_s:
        raise _invalid_window(
            f'end ({end_s:g}s) must be greater than start ({start_s:g}s).')
    name = source_name or os.path.basename(path) or 'video'
    if not os.path.isfile(path):
        raise VideoInspectError(
            'video_file_not_found', f'Video or audio file not found: {path}',
            'Pass an existing video or audio file: a project-relative path, an '
            'absolute path (e.g. a data/fetched/server-download-* file returned '
            'by browser_download_url_to_server), or an att_media_ video reference. '
            'Use find_files to locate it.')
    ffmpeg = _ffmpeg()
    if not ffmpeg:
        raise VideoInspectError(
            'video_ffmpeg_unavailable',
            'ffmpeg is not available on this server, so the file cannot be '
            'decoded.',
            'Tell the user the server lacks ffmpeg; an operator can install '
            'imageio-ffmpeg into the server environment or set TOFU_FFMPEG to '
            'an ffmpeg binary, then retry.')

    scratch_dir = tempfile.mkdtemp(prefix='job_', dir=va.scratch_root())
    try:
        mark_scratch_owner(scratch_dir)
        local_copy = os.path.join(
            scratch_dir, 'source' + os.path.splitext(path)[1].lower())
        try:
            shutil.copyfile(path, local_copy)
        except OSError as error:
            raise VideoInspectError(
                'video_read_failed', f'Could not stage {name} for decoding: {error}',
                'Check that the file is readable and the server has local '
                'scratch space, then retry.', retryable=True) from error

        from lib.motion_video._gates import probe_video
        probe = probe_video(local_copy, allow_audio_only=True)
        # ffmpeg's tty demuxer renders any text file as ANSI-art "video"
        # (codec 'ansi'); a document is not a video.
        if not probe or probe.get('codec') == 'ansi':
            raise _undecodable(name)
        audio_only = not probe.get('codec')
        media = 'audio' if audio_only else 'video'
        duration = float(probe.get('duration') or 0)
        if not math.isfinite(duration) or duration <= 0:
            raise _undecodable(name)

        window_start = start_s or 0.0
        if window_start >= duration:
            raise _invalid_window(
                f'start ({window_start:.2f}s) is at or past the end of the '
                f'{media} ({duration:.2f}s).')
        clamped = end_s is not None and end_s > duration
        window_end = duration if end_s is None else min(end_s, duration)
        windowed = window_start > 0 or window_end < duration
        decode_path = (_cut_window(ffmpeg, local_copy, window_start, window_end,
                                   scratch_dir, audio_only=audio_only)
                       if windowed else local_copy)
        window_duration = window_end - window_start

        window_line = (f'Window: {_span(window_start, window_end)} of '
                       f'{_fmt_video_ts(duration)}')
        if not windowed:
            window_line += f' (whole {media})'
        if clamped:
            window_line += f'; end clamped to the {media} duration'
        lines = [f'inspect_video: {name}', _probe_facts(probe, duration), window_line]

        budget = None
        if model and not audio_only:
            from lib.model_info import video_frame_budget
            budget = video_frame_budget(model)
        chosen: list[dict] = []
        extracted_count = 0
        if audio_only:
            lines.append('Frames: none (audio-only file: no video stream).')
        elif budget == 0:
            lines.append(
                f'Frames: not extracted — the current model ({model}) has no '
                'vision support; rely on the probe facts and transcript.')
        else:
            frames_dir = os.path.join(scratch_dir, 'frames')
            os.makedirs(frames_dir)
            try:
                frames = va.extract_frames(
                    decode_path, window_duration, frames_dir,
                    source_fps=float(probe.get('fps') or 0))
            except RuntimeError as error:
                raise VideoInspectError(
                    'video_unreadable', f'Frame extraction failed for {name}: {error}',
                    'Retry with a narrower start/end window, or re-encode the '
                    'source video.') from error
            if not frames:
                raise VideoInspectError(
                    'video_unreadable', f'Frame extraction produced no frames for {name}.',
                    'Re-encode or re-download the source video, then retry.')
            extracted_count = len(frames)
            chosen = frames
            if model:
                average = sum(os.path.getsize(f['path']) for f in frames) // len(frames)
                from lib.model_info import video_frame_budget
                chosen = _thin_frames(
                    frames, video_frame_budget(model, avg_frame_bytes=average))
            if len(chosen) < extracted_count:
                lines.append(
                    f'Frames: {len(chosen)} of {extracted_count} extracted key '
                    "frames (thinned uniformly to this model's video frame "
                    'budget; inspect a narrower start/end window for denser '
                    'frames), shown above in this order:')
            else:
                lines.append(
                    f'Frames: {len(chosen)} key frames (uniform cadence plus '
                    f'scene cuts; JPEG, long side ≤{FRAME_LONG_SIDE_PX}px), '
                    'shown above in this order:')
            for index, frame in enumerate(chosen, 1):
                moment = window_start + float(frame['t'])
                frame['moment'] = moment
                lines.append(f'  {index}. {_fmt_video_ts(moment)} ({moment:.2f}s)')

        transcript, transcript_status = _transcript_lines(
            probe, decode_path, scratch_dir, window_start, window_end,
            owner_user_id)
        lines.extend(transcript)
        text = '\n'.join(lines)

        if not chosen:
            return text
        images = {}
        for index, frame in enumerate(chosen):
            with open(frame['path'], 'rb') as stream:
                data = stream.read()
            images[index] = {
                '__screenshot__': True,
                'dataUrl': 'data:image/jpeg;base64,' + base64.b64encode(data).decode('ascii'),
                'format': 'jpeg',
                'originalSize': len(data),
                'compressedSize': len(data),
                'compressionApplied': False,
                'filename': f'{name} @ {frame["moment"]:.2f}s',
            }
        logger.info('[VideoInspect] %s: %.1fs %sx%s window=%.2f-%.2f frames=%d/%d '
                    'transcript=%s', name, duration, probe.get('width'),
                    probe.get('height'), window_start, window_end, len(chosen),
                    extracted_count, transcript_status)
        return {
            '__batch_images__': images,
            '_text_content': text,
            '_no_vision_fallback': (
                text + '\n[The frames listed above are not shown: the current '
                'model has no vision support.]'),
            'videoInspect': {
                'name': name, 'durationS': round(duration, 2),
                'width': int(probe.get('width') or 0),
                'height': int(probe.get('height') or 0),
                'frames': len(chosen), 'extractedFrames': extracted_count,
                'window': [round(window_start, 2), round(window_end, 2)],
                'transcript': transcript_status,
            },
        }
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)
