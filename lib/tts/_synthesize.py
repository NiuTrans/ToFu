"""lib/tts/_synthesize.py — the /audio/speech provider seam + synthesize().

OpenAI-compatible text-to-speech: ``POST {base}/audio/speech`` with
``{model, input, voice, response_format, speed}`` and raw audio bytes back.
Mirrors lib/transcription/_transcribe.py: slot selection comes from the
dispatcher pool (capability on the slot, never a vendor branch), the POST is
issued here as an isolated, monkeypatchable seam, and every failure carries
an HTTP status for the route layer.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

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


@dataclass(frozen=True, slots=True)
class SynthesisSession:
    """One owner-scoped v2 route group shared by a bounded synthesis batch."""

    pin_id: str

    def synthesize(self, text: str, *, voice: str | None = None,
                   fmt: str | None = None,
                   speed: float | None = None) -> SynthesizeResult:
        """Run one call inside this session's hard provider-group pin."""
        from lib import tts as _facade
        from lib.llm_dispatch.provider_pin import provider_pin

        with provider_pin(self.pin_id):
            return _facade.synthesize(
                text, voice=voice, fmt=fmt, speed=speed)


@contextmanager
def synthesis_session(
    owner_user_id: int,
    tenant_id: str | None = None,
    *,
    prefer_model: str = '',
    preferred_provider_id: str = '',
) -> Iterator[SynthesisSession]:
    """Mint one bounded owner route for a complete podcast/video TTS batch.

    A session is deliberately reusable across worker threads: each call enters
    the same thread-local hard pin, while disposal happens only after the
    caller's bounded fan-out has joined. This avoids a repository read and up
    to dozens of ephemeral slots for every narration chunk.
    """
    import lib.model_routing as routing

    route_group = None
    try:
        _model, route_group = routing.mint_capability_slot_group(
            routing.ModelRoutingRepository(),
            routing.OwnerBoundary.create(owner_user_id, tenant_id),
            TTS_CAP,
            prefer_model=prefer_model,
            preferred_provider_id=preferred_provider_id,
            required_protocols=routing.OPENAI_COMPATIBLE_PROTOCOLS,
            owner_tag=f'tts:{owner_user_id}',
            # A long narration shares this group. Eight ordered failover
            # candidates are ample and cap its resident dispatcher footprint.
            max_candidates=8,
        )
    except routing.ModelRoutingError as exc:
        raise TTSError(str(exc), status=503) from exc
    try:
        yield SynthesisSession(route_group.pin_id)
    finally:
        routing.dispose_routed_slot_group(route_group)


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
    headers = {}
    if slot.api_key:
        headers['Authorization'] = f'Bearer {slot.api_key}'
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
               fmt: str | None = None, speed: float | None = None,
               owner_user_id: int | None = None,
               tenant_id: str | None = None,
               prefer_model: str = '',
               preferred_provider_id: str = '') -> SynthesizeResult:
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
    if owner_user_id is not None:
        with synthesis_session(
            owner_user_id,
            tenant_id,
            prefer_model=prefer_model,
            preferred_provider_id=preferred_provider_id,
        ) as session:
            return session.synthesize(
                text, voice=voice, fmt=fmt, speed=speed)
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
        logical_model = getattr(slot, 'logical_model', '') or slot.model
        public_provider_id = (getattr(slot, 'routing_provider_id', '')
                              or slot.provider_id or 'default')
        audit_log('tts_synthesize', model=slot.model,
                  provider_id=public_provider_id,
                  voice=use_voice, fmt=use_fmt, chars=len(text), bytes=len(data))
        logger.info('[TTS] synthesized %d chars via %s:%s voice=%s fmt=%s → '
                    '%d bytes (%s)', len(text), slot.key_name, slot.model,
                    use_voice, use_fmt, len(data), mime)
        return SynthesizeResult(
            audio_bytes=data, mime=mime, model=logical_model,
            provider_id=public_provider_id, voice=use_voice)

    raise last_err or TTSError('All tts slots failed', status=502)
