"""Assemble and verify one revision without writing the published film early.

Entry: finalize_revision_media. Dependencies: the shared timeline/media gates,
media_publication and caller-owned codec/heartbeat/cancellation ports. Existing
workers retain source edits and task settlement; this owner stages only the final
binary and does not claim transactionality for earlier scene/narration edits.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
import os
from pathlib import Path
import shutil
import tempfile

from lib.json_store import read_json
from lib.log import get_logger
from lib.production.stages import StageAborted
from ._timeline import normalise_timeline_contract
from .media_publication import (
    assemble_candidate, final_media_findings, inspect_candidate, publish_candidate,
)

logger = get_logger(__name__)


@contextmanager
def _candidate_path(workdir):
    # Same filesystem permits an atomic rename; a private directory prevents
    # another revision from sharing or cleaning this candidate's filename.
    directory = tempfile.mkdtemp(prefix='.revision-media-', dir=workdir)
    try:
        yield os.path.join(directory, 'final.mp4')
    finally:
        try:
            shutil.rmtree(directory)
        except OSError as error:
            logger.warning('[MotionVideo] revision candidate cleanup failed: %s', error)


def finalize_revision_media(task, *, scenes, video_path, narration_wav,
                            style_overrides, media, pulse, aborted):
    """Return the published path/duration, or an empty result for owner Stop."""
    if aborted():
        return {}
    workdir = task['workdir']
    # Resolve from the same timeline policy as initial film production. Copy
    # rows because a verification pass must not rewrite the persisted board.
    timeline = normalise_timeline_contract([dict(scene) for scene in scenes])
    duration = timeline['duration_s']
    if duration <= 0:
        raise ValueError('revision has no valid film timeline')
    audio_plan = read_json(os.path.join(workdir, 'audio_plan.json'), default=None)
    with _candidate_path(workdir) as candidate:
        video_final = video_path
        if task.get('burn_in'):
            burned = os.path.join(workdir, 'final_burned.mp4')
            with pulse():
                result = media.burn_in_subtitles(
                    video_path, os.path.join(workdir, 'final.srt'), burned,
                    fontsdir=task.get('burn_in_fontsdir') or '',
                    style_overrides=style_overrides,
                    abort_event=task.get('abort_event'))
            if not result.get('ok'):
                raise RuntimeError('re-burn failed: ' + result.get('detail', ''))
            if aborted():
                return {}
            video_final = burned

        with pulse() if audio_plan or narration_wav else nullcontext():
            result = assemble_candidate(video_final, narration_wav, audio_plan,
                candidate, media=media, abort_event=task.get('abort_event'))
        if not result.get('ok'):
            operation = 're-mix' if audio_plan else 're-mux'
            raise RuntimeError(operation + ' failed: ' + result.get('detail', ''))
        if aborted():
            return {}

        inspected = inspect_candidate(candidate, probe_video=media.probe_video)
        findings = final_media_findings(inspected['probe'],
            width=task['width'], height=task['height'], fps=30,
            duration=duration, needs_audio=bool(narration_wav or audio_plan),
            verify_spec=media.verify_spec)
        if findings:
            raise ValueError('revised film failed media verification: ' + ' | '.join(findings))
        final_path = str(Path(workdir) / 'final.mp4')
        try:
            publish_candidate(inspected, final_path, abort_check=aborted)
        except StageAborted:
            return {}
        return {'final_path': final_path,
                'duration': round(float(inspected['probe']['duration']), 3)}
