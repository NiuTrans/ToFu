"""Bind media verification and atomic publication to the same candidate bytes.

Entry points: assemble_candidate, inspect_candidate, final_media_findings and
publish_candidate.
Callers own authorized workdir selection, codecs, task lifecycle and quality
policy. This owner uses explicit codec/probe/gate/cancellation ports; it never
spawns workers, settles tasks or replaces domain artifact acceptance.
"""
from __future__ import annotations

import hashlib
import os
import shutil


def assemble_candidate(video_path, narration_wav, audio_plan, output, *,
                       media, abort_event):
    """Use one audio-selection policy for initial films and their revisions."""
    if audio_plan:
        return media.mix_audio_timeline(video_path, narration_wav, audio_plan,
                                        output, abort_event=abort_event)
    if narration_wav:
        return media.mux_audio_video(video_path, narration_wav, output,
                                     abort_event=abort_event)
    shutil.copyfile(video_path, output)
    return {'ok': True, 'output': output}


def media_receipt(path: str) -> dict:
    """Bind verification/publication to the same nonempty bytes."""
    with open(path, 'rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        size = stream.tell()
    if size <= 0:
        raise ValueError('film output is empty')
    return {'path': path, 'bytes': size, 'sha256': digest}


def inspect_candidate(path: str, *, probe_video) -> dict:
    """Probe once and reject media that changes during that observation."""
    media = media_receipt(path)
    probe = probe_video(path)
    if media_receipt(path) != media:
        raise ValueError('candidate changed during media verification')
    return {**media, 'probe': probe}


def final_media_findings(probe, *, width, height, fps, duration,
                         needs_audio, verify_spec) -> list:
    """Reuse the scene media specification with final-film audio intent."""
    errors = verify_spec(probe, width=width, height=height, fps=fps,
                         duration=duration, require_silent=not needs_audio)
    if needs_audio and not (probe or {}).get('has_audio'):
        errors.append('final film is missing its requested audio track')
    return errors


def publish_candidate(media: dict, final_path: str, *, abort_check) -> dict:
    """Replace the stable film only if the verified candidate is still current."""
    candidate = media['path']
    if media_receipt(candidate) != {key: media[key] for key in ('path', 'bytes', 'sha256')}:
        raise ValueError('candidate changed after media verification')
    if abort_check():
        from lib.production.stages import StageAborted
        raise StageAborted('cancelled before film publication')
    os.replace(candidate, final_path)
    return {**media, 'path': final_path}
