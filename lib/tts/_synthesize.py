"""lib/tts/_synthesize.py — the /audio/speech provider seam + synthesize().

OpenAI-compatible text-to-speech: ``POST {base}/audio/speech`` with
``{model, input, voice, response_format, speed}`` and raw audio bytes back.
Mirrors lib/transcription/_transcribe.py: slot selection comes from the
dispatcher pool (capability on the slot, never a vendor branch), the POST is
issued here as an isolated, monkeypatchable seam, and every failure carries
an HTTP status for the route layer.
"""

from __future__ import annotations

from dataclasses import dataclass

from lib.http_client import http_post
from lib.log import audit_log, get_logger

from lib.tts._config import TTS_CAP

logger = get_logger(__name__)


class TTSError(Exception):
    """A synthesis failure carrying the HTTP status the route should emit.

    ``status`` is 503 when no TTS slot is configured (feature degraded),
    400 for a payload problem, 502 on an upstream provider failure.
    """

    def __init__(self, detail: str, *, status: int = 502):
        super().__init__(detail)
        self.detail = detail
        self.status = status


@dataclass
class SynthesizeResult:
    """Outcome of a successful synthesis."""

    audio_bytes: bytes
    mime: str
    model: str
    provider_id: str
    voice: str


# ── MIME sniffing ────────────────────────────────────────────────────────

_FMT_MIME = {
    'wav': 'audio/wav', 'mp3': 'audio/mpeg', 'pcm': 'audio/pcm',
    'opus': 'audio/opus', 'flac': 'audio/flac', 'aac': 'audio/aac',
}


def sniff_container(data: bytes) -> str:
    """Best-effort container sniff: 'wav' | 'mp3' | 'flac' | 'ogg' | 'unknown'."""
    if not data or len(data) < 4:
        return 'unknown'
    if data[:4] == b'RIFF' and data[8:12] == b'WAVE':
        return 'wav'
    if data[:3] == b'ID3' or (data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
        return 'mp3'
    if data[:4] == b'fLaC':
        return 'flac'
    if data[:4] == b'OggS':
        return 'ogg'
    return 'unknown'


def _sniff_mime(data: bytes, fmt: str) -> str:
    container = sniff_container(data)
    if container != 'unknown':
        return {'wav': 'audio/wav', 'mp3': 'audio/mpeg', 'flac': 'audio/flac',
                'ogg': 'audio/ogg'}[container]
    return _FMT_MIME.get((fmt or '').lower(), 'application/octet-stream')


# ── Provider POST (isolated seam — monkeypatched in tests) ───────────────


def _post_speech(slot, text: str, *, voice: str, fmt: str,
                 speed: float) -> bytes:
    """POST one synthesis request to a slot's ``/audio/speech``; return bytes.

    Raises :class:`TTSError` on any transport / HTTP-status / empty-body
    failure. Isolated so tests stub the network by monkeypatching
    ``lib.tts._post_speech``.
    """
    base = (slot.base_url or '').rstrip('/')
    if not base:
        raise TTSError('No base URL configured for tts slot', status=503)
    url = f'{base}/audio/speech'
    headers = {'Authorization': f'Bearer {slot.api_key}'}
    if slot.extra_headers:
        headers.update(slot.extra_headers)
    payload: dict = {'model': slot.model, 'input': text, 'voice': voice}
    if fmt:
        payload['response_format'] = fmt
    if speed and speed != 1.0:
        payload['speed'] = speed
    try:
        resp = http_post(url, json=payload, headers=headers, timeout=180)
    except Exception as e:
        raise TTSError(f'TTS request failed: {e}', status=502) from e
    if resp.status_code != 200:
        body = (resp.text or '')[:300]
        raise TTSError(
            f'TTS provider returned HTTP {resp.status_code}: {body}', status=502)
    data = resp.content or b''
    if not data:
        raise TTSError('TTS provider returned an empty body', status=502)
    return data


# ── Public entry point ───────────────────────────────────────────────────


def synthesize(text: str, *, voice: str | None = None,
               fmt: str | None = None, speed: float | None = None) -> SynthesizeResult:
    """Synthesize ``text`` to audio via a configured tts slot.

    Args:
        text: The spoken text (already chunked by the caller to fit the
            provider's input ceiling — see lib.tts.max_input_chars).
        voice: Explicit voice; falls back to data/config/tts.json
            ``default_voice`` then the fallback constant.
        fmt: ``response_format`` ('wav' default via config; 'mp3'…).
        speed: Rate multiplier; 1.0/None omits the field.

    Returns:
        A :class:`SynthesizeResult`.

    Raises:
        TTSError: 503 when no TTS slot is configured (callers degrade to
            script-only), 502 when every configured slot fails.
    """
    text = (text or '').strip()
    if not text:
        raise TTSError('Empty synthesis input', status=400)
    # Resolve swappable dependencies through the PACKAGE so test monkeypatches
    # on ``lib.tts.<name>`` take effect (facade parity with transcription).
    from lib import tts as _facade

    use_voice = (voice or '').strip() or _facade.default_voice()
    use_fmt = (fmt or '').strip() or _facade.default_format()
    use_speed = speed if speed is not None else _facade.default_speed()

    slots = _facade._tts_slots()
    if not slots:
        raise TTSError(
            'No TTS model is configured. Register a provider whose model '
            f'carries the {TTS_CAP!r} capability (POST /audio/speech).',
            status=503)

    last_err: TTSError | None = None
    for slot in slots:
        try:
            data = _facade._post_speech(slot, text, voice=use_voice,
                                        fmt=use_fmt, speed=use_speed)
        except TTSError as e:
            last_err = e
            logger.warning('[TTS] slot %s:%s failed (%s) — trying next',
                           slot.key_name, slot.model, e.detail)
            continue
        mime = _sniff_mime(data, use_fmt)
        audit_log('tts_synthesize', model=slot.model,
                  provider_id=slot.provider_id or 'default',
                  voice=use_voice, fmt=use_fmt, chars=len(text), bytes=len(data))
        logger.info('[TTS] synthesized %d chars via %s:%s voice=%s fmt=%s → '
                    '%d bytes (%s)', len(text), slot.key_name, slot.model,
                    use_voice, use_fmt, len(data), mime)
        return SynthesizeResult(
            audio_bytes=data, mime=mime, model=slot.model,
            provider_id=slot.provider_id or 'default', voice=use_voice)

    raise last_err or TTSError('All tts slots failed', status=502)
