"""routes/api_v1/videos.py — video upload + processing status + playback.

P1 of the video-upload epic (): the client POSTs a video,
gets a ``video_id`` immediately (202-style async pattern on a 200 envelope),
and polls ``GET /api/v1/videos/<video_id>`` until ``status == 'ready'`` —
the record then carries the full self-contained payload (durable frame URLs +
transcript + metadata) that the frontend embeds into the conversation message.

Limits (owner ruling 2026-08-04): 512 MiB / 15 min. The app-global
MAX_CONTENT_LENGTH is raised to fit the 512 MiB cap, so a central
before_request guard in server.py keeps every OTHER route at the legacy
50 MiB ceiling — only this upload path accepts big bodies.
"""

from __future__ import annotations

import os
import tempfile
import time

from quart import Blueprint, request

from lib.quart_sync import request_files

from lib.api_response import (
    api_bad_request,
    api_error,
    api_not_found,
    api_ok,
    api_payload_too_large,
)
from lib.file_serving import send_file_conditional
from lib.log import get_logger

from .auth import request_user_id

logger = get_logger(__name__)

api_v1_videos_bp = Blueprint('api_v1_videos', __name__)

__all__ = ['api_v1_videos_bp']

_CHUNK = 1024 * 1024  # 1 MiB streaming chunks


def _sniff_video_container(head: bytes) -> str | None:
    """Magic-bytes container sniff: 'mp4' (mp4/mov/m4v), 'webm' (webm/mkv),
    'avi', or None. Extension claims are NOT trusted — the payload decides."""
    if len(head) >= 12 and head[4:8] == b'ftyp':
        return 'mp4'
    if head[:4] == b'\x1a\x45\xdf\xa3':
        return 'webm'
    if len(head) >= 12 and head[:4] == b'RIFF' and head[8:12] == b'AVI ':
        return 'avi'
    return None


#: extension → the container family it must sniff as
_EXT_CONTAINER = {
    '.mp4': 'mp4', '.m4v': 'mp4', '.mov': 'mp4',
    '.webm': 'webm', '.mkv': 'webm',
    '.avi': 'avi',
}


@api_v1_videos_bp.route('/api/v1/videos/upload', methods=['POST'])
def upload_video():
    from lib import video_analysis as va

    owner_user_id = int(request_user_id())
    if not va.video_analysis_enabled():
        return api_error('Video analysis is disabled on this server', status=503)

    files = request_files()
    if 'file' not in files:
        return api_bad_request('No file')
    file = files['file']
    if not file.filename:
        return api_bad_request('No filename')
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in va.VIDEO_EXTS:
        logger.warning('[videos] rejected extension=%s', ext)
        return api_bad_request(
            f'Unsupported video type {ext!r}. Allowed: '
            + ', '.join(sorted(va.VIDEO_EXTS)))

    cap = va.video_max_bytes()
    # Honor an honest Content-Length up front; still enforce while streaming.
    cl = request.content_length
    if cl and cl > cap:
        logger.warning('[videos] rejected by Content-Length %d > %d', cl, cap)
        return api_payload_too_large(cap)

    # Stream to LOCAL-disk scratch with a hard byte cap — never decode or
    # buffer a multi-hundred-MB upload in memory, never on the FUSE mount.
    scratch_dir = tempfile.mkdtemp(prefix='job_', dir=va.scratch_root())
    tmp_path = os.path.join(scratch_dir, 'upload' + ext)
    total = 0
    try:
        with open(tmp_path, 'wb') as out:
            while True:
                chunk = file.stream.read(_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > cap:
                    raise _TooLarge()
                out.write(chunk)
    except _TooLarge:
        _cleanup(scratch_dir)
        logger.warning('[videos] rejected while streaming: >%d bytes', cap)
        return api_payload_too_large(cap)
    except Exception as e:
        _cleanup(scratch_dir)
        logger.error('[videos] upload stream failed: %s', e, exc_info=True)
        return api_error('Failed to receive the upload', status=500)

    if total < 1024:
        _cleanup(scratch_dir)
        return api_bad_request('Empty or truncated video upload')

    # Magic-bytes container check (extension claims are not trusted).
    try:
        with open(tmp_path, 'rb') as f:
            head = f.read(32)
    except Exception as e:
        _cleanup(scratch_dir)
        logger.error('[videos] scratch read-back failed: %s', e, exc_info=True)
        return api_error('Failed to verify the upload', status=500)
    container = _sniff_video_container(head)
    if container is None or container != _EXT_CONTAINER.get(ext):
        _cleanup(scratch_dir)
        logger.warning('[videos] magic mismatch: ext=%s sniffed=%s', ext, container)
        return api_bad_request('Payload does not match the declared video format')

    video_id = f'v_{int(time.time() * 1000)}_{os.urandom(4).hex()}'
    va.create_record(
        video_id,
        name=file.filename,
        size_bytes=total,
        user_id=owner_user_id,
    )
    va.start_processing(
        video_id,
        tmp_path,
        file.filename,
        user_id=owner_user_id,
    )
    logger.info('[videos] accepted %s (%s, %d bytes) → processing',
                video_id, file.filename, total)
    return api_ok({'video_id': video_id, 'status': 'processing',
                   'poll': f'/api/v1/videos/{video_id}'})


class _TooLarge(Exception):
    pass


def _cleanup(path: str) -> None:
    import shutil
    shutil.rmtree(path, ignore_errors=True)


@api_v1_videos_bp.route('/api/v1/videos/<video_id>', methods=['GET'])
def video_status(video_id: str):
    from lib import video_analysis as va

    if not va.video_analysis_enabled():
        return api_error('Video analysis is disabled on this server', status=503)
    rec = va.get_record(video_id, user_id=int(request_user_id()))
    if rec is None:
        return api_not_found('video_not_found')
    return api_ok(rec)


@api_v1_videos_bp.route('/api/videos/<filename>')
def serve_video(filename: str):
    """Serve the persisted original video (frontend playback / re-download).

    Range-aware via the unified file_serving seam (single-byte Range probes
    must not 500 — see the 2026-08-03 file_serving consolidation)."""
    from lib import video_analysis as va

    filepath = va.resolve_owned_video_asset(
        filename,
        user_id=int(request_user_id()),
    )
    if not filepath:
        return api_not_found('Not found')
    return send_file_conditional(filepath)
