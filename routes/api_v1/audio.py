"""routes/api_v1/audio.py — Speech-to-text (voice input) endpoints.

Routes:
  POST /api/v1/audio/transcribe     — multipart audio blob → transcript text
  GET  /api/v1/audio/capabilities   — is voice input available + which models

Patterned on ``routes/upload.py::parse_pdf`` (the binary-upload template): a
``multipart/form-data`` POST with a ``file`` field, guarded by a byte cap, a
MIME allow-list, and a best-effort duration check, then routed through
``lib.transcription`` to whatever transcription-capable slot is configured
(provider-agnostic — no vendor branches, per CLAUDE.md §3.5).

Scope: ``chat``. Transcription is a stateless blob→text utility (it injects no
operator-personal state into any prompt), so it is intentionally NOT registered
in ``lib/agent_core/personal_scope.py`` and works on headless surfaces for any
caller holding the ``chat`` scope — same tier as the completion it feeds.
"""

from __future__ import annotations

from quart import Blueprint, request

from lib.quart_sync import request_files, request_form

from lib.api_response import api_bad_request, api_error, api_ok
from lib.log import get_logger
from lib.openapi import api_meta

from .auth import require_scope

logger = get_logger(__name__)

api_v1_audio_bp = Blueprint('api_v1_audio', __name__)


@api_v1_audio_bp.route('/api/v1/audio/capabilities', methods=['GET'])
@require_scope('chat')
@api_meta(
    summary='Voice-input availability',
    description=(
        'Returns ``{available, models: [{model, provider_id}], maxBytes, '
        'maxDurationS}``. ``available`` is False when no transcription-capable '
        'model is configured — the frontend hides the mic button in that case '
        'rather than offering a feature that will 503.'
    ),
    tags=['audio'],
    scope='chat',
)
def audio_capabilities_v1():
    from lib.transcription import (
        audio_byte_cap, list_transcription_models, max_audio_duration_s,
        transcription_available,
    )
    return api_ok(
        available=transcription_available(),
        models=list_transcription_models(),
        maxBytes=audio_byte_cap(),
        maxDurationS=max_audio_duration_s(),
    )


@api_v1_audio_bp.route('/api/v1/audio/transcribe', methods=['POST'])
@require_scope('chat')
@api_meta(
    summary='Transcribe an audio blob to text',
    description=(
        'Accepts ``multipart/form-data`` with a ``file`` field carrying the '
        'audio (webm/ogg/wav/mp3/m4a/flac) plus optional ``language`` (ISO-639-1 '
        'hint) and ``prompt`` (domain-term biasing) form fields. Returns '
        '``{ok, text, model, provider_id, durationS?}``. Routes through the '
        'configured transcription slot pool (provider-agnostic). 503 when no '
        'transcription model is configured; 400 for an empty / oversize / '
        'unsupported / too-long upload; 502 on an upstream provider failure.'
    ),
    tags=['audio'],
    scope='chat',
)
def transcribe_audio_v1():
    from lib.transcription import (
        TranscriptionError, audio_byte_cap, transcribe,
    )

    cap = audio_byte_cap()
    # Fast reject by declared length before reading the body into memory.
    if request.content_length and request.content_length > cap:
        logger.warning('[Audio.v1] rejected by content_length: %d > %d',
                       request.content_length, cap)
        return api_bad_request(f'Audio too large (max {cap // (1024 * 1024)} MB)')

    files = request_files()
    if 'file' not in files:
        return api_bad_request('No file provided')
    f = files['file']
    filename = f.filename or ''
    if not filename:
        return api_bad_request('No filename')

    audio_bytes = f.read()
    if not audio_bytes:
        return api_bad_request('Empty audio upload')
    if len(audio_bytes) > cap:
        logger.warning('[Audio.v1] rejected after read: %d > %d',
                       len(audio_bytes), cap)
        return api_bad_request(f'Audio too large (max {cap // (1024 * 1024)} MB)')

    form = request_form()
    language = (form.get('language') or '').strip() or None
    prompt = (form.get('prompt') or '').strip() or None

    try:
        result = transcribe(audio_bytes, filename,
                            content_type=f.content_type,
                            language=language, prompt=prompt)
    except TranscriptionError as e:
        logger.warning('[Audio.v1] transcription failed (%d): %s',
                       e.status, e.detail)
        return api_error(e.detail, status=e.status)
    except Exception as e:
        logger.error('[Audio.v1] transcription crashed for %s (%d bytes): %s',
                     filename, len(audio_bytes), e, exc_info=True)
        return api_error(f'Transcription failed: {e}', status=500)

    return api_ok({
        'text': result.text,
        'model': result.model,
        'provider_id': result.provider_id,
        'durationS': result.duration_s,
    })


__all__ = ['api_v1_audio_bp']
