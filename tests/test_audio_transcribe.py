#!/usr/bin/env python3
"""Tests for the voice-input backend: POST /api/v1/audio/transcribe.

Verifies the endpoint contract WITHOUT any real provider or configured slot:
  * happy path        → {ok, text, model} (stubbed slot + stubbed provider POST)
  * oversize blob     → 400
  * missing file      → 400
  * unsupported MIME  → 400
  * no model config   → 503 (graceful disable)
plus a unit test that the WAV duration guard rejects an over-long clip.

The transcription slot pool and the provider network POST are both stubbed via
monkeypatch, so the test is hermetic (no keys, no HTTP, no configured models).

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_audio_transcribe.py -v
"""

import asyncio
import io
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]


@pytest.fixture
def client(flask_app):
    """Raw Quart test client (async), matching tests/test_server_async.py."""
    return flask_app.test_client()


# ── Helpers ────────────────────────────────────────────────────────────

def _run_async(coro):
    """Drive an async test body on a private loop (no pytest-asyncio here)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeSlot:
    """Minimal stand-in for a dispatch Slot with the transcription capability."""

    def __init__(self, model='gpt-4o-transcribe', provider_id='prov_test'):
        self.model = model
        self.provider_id = provider_id
        self.key_name = 'key_0'
        self.api_key = 'sk-test'
        self.base_url = 'https://stt.example/v1'
        self.extra_headers = {}
        self.capabilities = {'transcription'}
        self.oauth = ''

    def score(self):
        return 1.0


class _FakeRouteGroup:
    pin_id = 'prov_test'


def _stub_owner_route(monkeypatch, *, group=None):
    """Make an HTTP route test cross the v2 owner boundary hermetically."""
    import lib.model_routing as routing

    route_group = group or _FakeRouteGroup()
    monkeypatch.setattr(
        routing,
        'mint_capability_slot_group',
        lambda *_args, **_kwargs: ('gpt-4o-transcribe', route_group),
    )
    monkeypatch.setattr(
        routing, 'dispose_routed_slot_group', lambda _group: None)
    return route_group


def _multipart(audio_bytes, filename='clip.webm', content_type='audio/webm',
               extra_form=None):
    """Build (form, files) for Quart's test client (needs a FileStorage)."""
    from werkzeug.datastructures import FileStorage
    fs = FileStorage(stream=io.BytesIO(audio_bytes), filename=filename,
                     content_type=content_type)
    return (dict(extra_form or {}), {'file': fs})


def _make_wav(seconds=1.0, sample_rate=8000, amplitude=0.3, freq=220.0):
    """Return a minimal mono 16-bit PCM WAV of the given duration.

    Fills with a sine tone at ``amplitude`` (0..1) so the clip is NON-silent by
    default and passes the transcription silence gate (which short-circuits
    measurably-silent WAVs before dispatch). Pass ``amplitude=0.0`` for a truly
    silent clip. The byte layout/size is independent of the sample values, so
    the duration-probe tests are unaffected by the tone.
    """
    import math
    n = int(seconds * sample_rate)
    byte_rate = sample_rate * 2  # mono, 16-bit
    if amplitude <= 0:
        data = b'\x00\x00' * n
    else:
        peak = int(max(-1.0, min(1.0, amplitude)) * 32767)
        frames = bytearray()
        for i in range(n):
            s = int(peak * math.sin(2 * math.pi * freq * i / sample_rate))
            frames += struct.pack('<h', s)
        data = bytes(frames)
    header = b'RIFF' + struct.pack('<I', 36 + len(data)) + b'WAVE'
    header += b'fmt ' + struct.pack('<IHHIIHH', 16, 1, 1, sample_rate,
                                     byte_rate, 2, 16)
    header += b'data' + struct.pack('<I', len(data))
    return header + data


# ── Endpoint tests ──────────────────────────────────────────────────────

def test_transcribe_happy_path(client, monkeypatch):
    """A valid audio blob with a configured slot returns {ok, text, model}."""
    import lib.transcription as tr

    _stub_owner_route(monkeypatch)
    monkeypatch.setattr(tr, '_transcription_slots', lambda: [_FakeSlot()])

    captured = {}

    def _fake_post(slot, audio_bytes, filename, mime, *, language, prompt):
        captured['model'] = slot.model
        captured['mime'] = mime
        captured['filename'] = filename
        captured['language'] = language
        captured['prompt'] = prompt
        return '  hello world  '

    monkeypatch.setattr(tr, '_post_to_provider', _fake_post)

    async def go():
        form, files = _multipart(b'\x1a\x45\xdf\xa3fake-webm-bytes',
                                 extra_form={'language': 'en',
                                             'prompt': 'Tofu, ASR'})
        r = await client.post('/api/v1/audio/transcribe', form=form, files=files)
        assert r.status_code == 200, r.status_code
        data = await r.get_json()
        assert data['ok'] is True
        assert data['text'] == 'hello world'          # trimmed by transcribe()
        assert data['model'] == 'gpt-4o-transcribe'
        assert data['provider_id'] == 'prov_test'
    _run_async(go())

    # The route forwarded the biasing form fields + resolved the webm MIME.
    assert captured['mime'] == 'audio/webm'
    assert captured['language'] == 'en'
    assert captured['prompt'] == 'Tofu, ASR'


def test_transcribe_oversize_returns_400(client, monkeypatch):
    """A blob above the byte cap is rejected before ever hitting a provider."""
    import lib.transcription as tr

    # Tiny cap so a small payload trips it; assert the provider is NEVER called.
    monkeypatch.setattr(tr, 'audio_byte_cap', lambda: 16)
    monkeypatch.setattr(tr, '_transcription_slots', lambda: [_FakeSlot()])

    def _boom(*a, **k):
        raise AssertionError('provider must not be called for oversize upload')
    monkeypatch.setattr(tr, '_post_to_provider', _boom)

    async def go():
        form, files = _multipart(b'x' * 4096, filename='big.wav',
                                 content_type='audio/wav')
        r = await client.post('/api/v1/audio/transcribe', form=form, files=files)
        assert r.status_code == 400, r.status_code
        data = await r.get_json()
        assert data['ok'] is False
    _run_async(go())


def test_transcribe_missing_file_returns_400(client):
    """No file field → 400."""
    async def go():
        r = await client.post('/api/v1/audio/transcribe', form={}, files={})
        assert r.status_code == 400, r.status_code
        data = await r.get_json()
        assert data['ok'] is False
    _run_async(go())


def test_transcribe_unsupported_format_returns_400(client, monkeypatch):
    """A non-audio extension is rejected by the MIME allow-list."""
    import lib.transcription as tr
    monkeypatch.setattr(tr, '_transcription_slots', lambda: [_FakeSlot()])

    async def go():
        form, files = _multipart(b'not audio at all', filename='notes.txt',
                                 content_type='text/plain')
        r = await client.post('/api/v1/audio/transcribe', form=form, files=files)
        assert r.status_code == 400, r.status_code
        data = await r.get_json()
        assert data['ok'] is False
    _run_async(go())


def test_transcribe_no_model_configured_returns_503(client, monkeypatch):
    """With no transcription-capable slot the feature disables gracefully (503)."""
    import lib.transcription as tr
    monkeypatch.setattr(tr, '_transcription_slots', lambda: [])

    async def go():
        form, files = _multipart(b'\x1a\x45\xdf\xa3fake-webm')
        r = await client.post('/api/v1/audio/transcribe', form=form, files=files)
        assert r.status_code == 503, r.status_code
        data = await r.get_json()
        assert data['ok'] is False
    _run_async(go())


def test_unexpected_transcription_failure_is_redacted(client, monkeypatch):
    """Provider/programmer faults never disclose exception text."""
    import lib.transcription as tr

    monkeypatch.setattr(
        tr,
        'transcribe',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError('/secret/path?token=do-not-expose')),
    )

    async def go():
        form, files = _multipart(b'\x1a\x45\xdf\xa3fake-webm')
        response = await client.post(
            '/api/v1/audio/transcribe', form=form, files=files)
        assert response.status_code == 500
        body = await response.get_json()
        assert body['ok'] is False
        assert '/secret/path' not in str(body)
        assert 'do-not-expose' not in str(body)

    _run_async(go())


def test_capabilities_reports_availability(client, monkeypatch):
    """GET /audio/capabilities projects owner-scoped v2 routes."""
    from types import SimpleNamespace

    import lib.model_routing as routing

    monkeypatch.setattr(
        routing,
        'list_capability_route_groups',
        lambda _repository, _boundary, requirements: {
            capability: [] for capability in requirements
        },
    )
    async def unavailable():
        r = await client.get('/api/v1/audio/capabilities')
        assert r.status_code == 200
        data = await r.get_json()
        assert data['available'] is False
        assert data['models'] == []
        assert data['maxBytes'] > 0
    _run_async(unavailable())

    def _route_groups(_repository, _boundary, requirements):
        return {
            capability: ([SimpleNamespace(
                model_id='gpt-4o-transcribe',
                provider_id='prov_test',
                offering_id='offering_test',
            )] if capability == 'transcription' else [])
            for capability in requirements
        }

    monkeypatch.setattr(routing, 'list_capability_route_groups', _route_groups)
    async def available():
        r = await client.get('/api/v1/audio/capabilities')
        data = await r.get_json()
        assert data['available'] is True
        assert data['models'] == [{'model': 'gpt-4o-transcribe',
                                    'provider_id': 'prov_test',
                                    'mode': 'endpoint'}]
    _run_async(available())


def test_owner_route_pins_and_disposes_request_group(monkeypatch):
    """Owner-aware transcription cannot escape its request-scoped v2 group."""
    import lib.model_routing as routing
    import lib.transcription as tr
    from lib.llm_dispatch.provider_pin import get_pinned_provider

    route_group = _FakeRouteGroup()
    disposed = []
    monkeypatch.setattr(
        routing,
        'mint_capability_slot_group',
        lambda *_args, **_kwargs: ('gpt-4o-transcribe', route_group),
    )
    monkeypatch.setattr(
        routing,
        'dispose_routed_slot_group',
        lambda group: disposed.append(group),
    )
    monkeypatch.setattr(tr, '_transcription_slots', lambda: [_FakeSlot()])
    observed_pins = []

    def _post(*_args, **_kwargs):
        observed_pins.append(get_pinned_provider())
        return 'owner transcript'

    monkeypatch.setattr(tr, '_post_to_provider', _post)

    result = tr.transcribe(
        b'\x1a\x45\xdf\xa3fake-webm-bytes',
        'clip.webm',
        content_type='audio/webm',
        owner_user_id=73,
        tenant_id='tenant-a',
    )

    assert result.text == 'owner transcript'
    assert observed_pins == ['prov_test']
    assert disposed == [route_group]
    assert get_pinned_provider() is None


# ── Unit tests (no HTTP) ────────────────────────────────────────────────

def test_wav_duration_guard_rejects_long_clip(monkeypatch):
    """A WAV longer than the max-duration cap is rejected with status 400."""
    import lib.transcription as tr
    monkeypatch.setattr(tr, 'max_audio_duration_s', lambda: 2.0)
    monkeypatch.setattr(tr, '_transcription_slots', lambda: [_FakeSlot()])
    monkeypatch.setattr(tr, '_post_to_provider',
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError('should not reach provider')))

    long_wav = _make_wav(seconds=5.0)
    with pytest.raises(tr.TranscriptionError) as ei:
        tr.transcribe(long_wav, 'long.wav', content_type='audio/wav')
    assert ei.value.status == 400
    assert 'too long' in ei.value.detail.lower()


def test_wav_duration_probe_reads_header():
    """The WAV probe returns ~the true duration; non-WAV returns None."""
    from lib.transcription import _probe_duration_s
    wav = _make_wav(seconds=3.0)
    dur = _probe_duration_s(wav, 'audio/wav')
    assert dur is not None and abs(dur - 3.0) < 0.05
    assert _probe_duration_s(b'\x1a\x45\xdf\xa3opusish', 'audio/webm') is None


def test_correction_pass_is_noop_by_default(monkeypatch):
    """maybe_correct is a no-op unless TOFU_ASR_CORRECTION is enabled."""
    import lib.transcription as tr
    monkeypatch.delenv('TOFU_ASR_CORRECTION', raising=False)
    assert tr.correction_enabled() is False
    assert tr.maybe_correct('teh cat sat') == 'teh cat sat'


def test_transcription_not_in_personal_scope():
    """Transcription is a stateless utility — must NOT be a personal capability."""
    from lib.agent_core.personal_scope import PERSONAL_CFG_KEYS
    assert 'transcriptionEnabled' not in PERSONAL_CFG_KEYS
    assert 'transcription' not in PERSONAL_CFG_KEYS


# ── audio_chat (inline chat-audio) mechanism ────────────────────────────

class _ChatSlot:
    """A slot that transcribes via the inline chat-audio path (audio_chat)."""

    def __init__(self, model='gemini-3-flash-preview', provider_id='meituan'):
        self.model = model
        self.provider_id = provider_id
        self.key_name = 'key_0'
        self.api_key = 'sk-test'
        self.base_url = 'https://aigc.example/v1/openai/native'
        self.extra_headers = {}
        self.capabilities = {'text', 'vision', 'cheap', 'audio_chat'}
        self.oauth = ''

    def score(self):
        return 1.0


def test_audio_chat_routes_via_dispatch_chat(monkeypatch):
    """An audio_chat slot builds input_audio messages → dispatch_chat, and
    NEVER issues a multipart /audio/transcriptions POST."""
    import lib.transcription as tr
    import lib.llm_dispatch as dispatch

    monkeypatch.setattr(tr, '_transcription_slots', lambda: [_ChatSlot()])

    # The multipart endpoint must NOT be touched for an audio_chat slot.
    def _no_post(*a, **k):
        raise AssertionError('audio_chat must NOT hit the multipart endpoint')
    monkeypatch.setattr(tr, '_post_to_provider', _no_post)

    captured = {}

    def _fake_dispatch_chat(messages, *, capability, prefer_model, strict_model,
                            temperature, max_tokens, log_prefix):
        captured['messages'] = messages
        captured['capability'] = capability
        captured['prefer_model'] = prefer_model
        captured['strict_model'] = strict_model
        return ('  chat transcript here  ', {})
    monkeypatch.setattr(dispatch, 'dispatch_chat', _fake_dispatch_chat)

    wav = _make_wav(seconds=1.0)
    result = tr.transcribe(wav, 'clip.wav', content_type='audio/wav')

    assert result.text == 'chat transcript here'      # trimmed by transcribe()
    assert result.model == 'gemini-3-flash-preview'
    # Routing: correct capability + pinned model + strict (no silent swap).
    assert captured['capability'] == 'audio_chat'
    assert captured['prefer_model'] == 'gemini-3-flash-preview'
    assert captured['strict_model'] is True
    # Message shape: a single user turn carrying an input_audio part (base64
    # data + format token) followed by a text instruction.
    content = captured['messages'][0]['content']
    audio_part = next(p for p in content if p['type'] == 'input_audio')
    assert audio_part['input_audio']['format'] == 'wav'
    import base64
    assert base64.b64decode(audio_part['input_audio']['data']) == wav
    assert any(p['type'] == 'text' and 'verbatim' in p['text'].lower()
               for p in content)


def test_transcription_slot_does_not_hit_chat_path(monkeypatch):
    """NEUTER (reverse): a plain `transcription` slot must use the multipart
    endpoint and NEVER the chat path — proves the branch is load-bearing."""
    import lib.transcription as tr
    import lib.llm_dispatch as dispatch

    monkeypatch.setattr(tr, '_transcription_slots', lambda: [_FakeSlot()])
    monkeypatch.setattr(tr, '_post_to_provider',
                        lambda *a, **k: 'endpoint transcript')

    def _no_chat(*a, **k):
        raise AssertionError('transcription slot must NOT use dispatch_chat')
    monkeypatch.setattr(dispatch, 'dispatch_chat', _no_chat)

    result = tr.transcribe(_make_wav(1.0), 'clip.wav', content_type='audio/wav')
    assert result.text == 'endpoint transcript'


def test_slot_audio_mode_branches_on_capability():
    """_slot_audio_mode: 'transcription' → endpoint, 'audio_chat'-only → chat,
    both → endpoint wins (cheaper, purpose-built)."""
    import lib.transcription as tr

    class _S:
        def __init__(self, caps):
            self.capabilities = set(caps)
    assert tr._slot_audio_mode(_S({'transcription'})) == 'endpoint'
    assert tr._slot_audio_mode(_S({'audio_chat'})) == 'chat'
    assert tr._slot_audio_mode(_S({'transcription', 'audio_chat'})) == 'endpoint'


def test_inline_audio_size_guard_fires(monkeypatch):
    """A blob over the inline cap is rejected for an audio_chat slot WITHOUT
    invoking dispatch_chat — while staying under the (larger) endpoint cap."""
    import lib.transcription as tr
    import lib.llm_dispatch as dispatch

    # Endpoint cap large (25 MB default), inline cap tiny → only chat path trips.
    monkeypatch.setattr(tr, 'inline_audio_byte_cap', lambda: 1024)
    monkeypatch.setattr(tr, '_transcription_slots', lambda: [_ChatSlot()])

    def _no_chat(*a, **k):
        raise AssertionError('over-inline-cap audio must not reach dispatch_chat')
    monkeypatch.setattr(dispatch, 'dispatch_chat', _no_chat)

    big = _make_wav(seconds=1.0, sample_rate=8000)  # ~16 KB > 1 KB inline cap
    assert len(big) > 1024
    with pytest.raises(tr.TranscriptionError) as ei:
        tr.transcribe(big, 'clip.wav', content_type='audio/wav')
    assert ei.value.status == 400
    assert 'inline' in ei.value.detail.lower()


def test_inline_cap_smaller_than_endpoint_cap():
    """The inline (base64) cap must be <= the endpoint cap by design."""
    import lib.transcription as tr
    assert tr.inline_audio_byte_cap() <= tr.audio_byte_cap()


# ── Silence gate (root-cause guard against generative hallucination) ────

def test_silence_gate_short_circuits_without_model_call(monkeypatch):
    """A measurably-silent WAV returns empty text WITHOUT dispatching to any
    slot — the fix for the omni audio_chat silence→hallucination path."""
    import lib.transcription as tr
    import lib.llm_dispatch as dispatch

    # A slot IS configured — the gate must fire BEFORE it is ever used.
    monkeypatch.setattr(tr, '_transcription_slots', lambda: [_ChatSlot()])

    def _no_chat(*a, **k):
        raise AssertionError('silent clip must NOT reach dispatch_chat')
    monkeypatch.setattr(dispatch, 'dispatch_chat', _no_chat)

    def _no_post(*a, **k):
        raise AssertionError('silent clip must NOT reach the endpoint POST')
    monkeypatch.setattr(tr, '_post_to_provider', _no_post)

    silent = _make_wav(seconds=1.5, amplitude=0.0)   # pure silence, 1.5s
    result = tr.transcribe(silent, 'clip.wav', content_type='audio/wav')
    assert result.text == ''
    assert result.model == 'silence-gate'
    assert result.provider_id == 'local'


def test_silence_gate_fires_even_with_no_slot(monkeypatch):
    """A silent clip returns empty (not 503) even when NO slot is configured —
    there is nothing to transcribe regardless of model availability."""
    import lib.transcription as tr
    monkeypatch.setattr(tr, '_transcription_slots', lambda: [])
    silent = _make_wav(seconds=1.0, amplitude=0.0)
    result = tr.transcribe(silent, 'clip.wav', content_type='audio/wav')
    assert result.text == ''
    assert result.model == 'silence-gate'


def test_real_speech_wav_is_not_silence_gated(monkeypatch):
    """A NON-silent tone WAV passes the gate and reaches the provider — proves
    the conservative floor does not drop a quiet-but-real utterance."""
    import lib.transcription as tr
    monkeypatch.setattr(tr, '_transcription_slots', lambda: [_FakeSlot()])
    monkeypatch.setattr(tr, '_post_to_provider',
                        lambda *a, **k: 'real transcript')

    # Deliberately quiet (amplitude 0.05 ≈ -26 dBFS) but with clear peaks —
    # must NOT be gated, must reach the stubbed provider.
    quiet = _make_wav(seconds=2.0, amplitude=0.05)
    assert tr._is_silent_wav(quiet, 'audio/wav') is False
    result = tr.transcribe(quiet, 'clip.wav', content_type='audio/wav')
    assert result.text == 'real transcript'


def test_compressed_input_is_never_silence_gated():
    """Non-WAV / unmeasurable input is 'unknown, allow' — the gate never fires
    on it (mirrors the _probe_duration_s None-means-allow convention)."""
    import lib.transcription as tr
    assert tr._probe_wav_level(b'\x1a\x45\xdf\xa3opus-ish', 'audio/webm') is None
    assert tr._is_silent_wav(b'\x1a\x45\xdf\xa3opus-ish', 'audio/webm') is False


def test_silence_floor_is_conservative():
    """Both floors must be set (RMS and peak) and the gate requires BOTH."""
    import lib.transcription as tr
    assert 0 < tr.silence_rms_floor() <= 0.02
    assert 0 < tr.silence_peak_floor() <= 0.10


# ── Hallucination ratio flag (diagnostic FLAG, never a drop) ────────────

def test_suspected_hallucination_flag_on_implausible_density(monkeypatch):
    """A transcript far too dense for the known duration is FLAGGED in the
    audit row (suspected_hallucination=True) but still RETURNED, not dropped."""
    import lib.transcription as tr

    monkeypatch.setattr(tr, '_transcription_slots', lambda: [_FakeSlot()])
    # 1809 chars for a 1.5s clip ≈ 1200 chars/s — the real 08:32:50 anomaly.
    monkeypatch.setattr(tr, '_post_to_provider', lambda *a, **k: 'x' * 1809)

    captured = {}
    real_audit = tr.audit_log

    def _spy(event, **kw):
        if event == 'audio_transcribe':
            captured.update(kw)
        return real_audit(event, **kw)
    monkeypatch.setattr(tr, 'audit_log', _spy)

    wav = _make_wav(seconds=1.5, amplitude=0.3)
    result = tr.transcribe(wav, 'clip.wav', content_type='audio/wav')
    # NOT dropped — the flag is diagnostic only.
    assert result.text == 'x' * 1809
    assert captured.get('suspected_hallucination') is True


def test_plausible_transcript_not_flagged(monkeypatch):
    """A normal-density transcript is NOT flagged."""
    import lib.transcription as tr

    monkeypatch.setattr(tr, '_transcription_slots', lambda: [_FakeSlot()])
    # ~13 chars/s over a 3s clip — well within a real speaking rate.
    monkeypatch.setattr(tr, '_post_to_provider', lambda *a, **k: 'x' * 40)

    captured = {}
    real_audit = tr.audit_log

    def _spy(event, **kw):
        if event == 'audio_transcribe':
            captured.update(kw)
        return real_audit(event, **kw)
    monkeypatch.setattr(tr, 'audit_log', _spy)

    wav = _make_wav(seconds=3.0, amplitude=0.3)
    tr.transcribe(wav, 'clip.wav', content_type='audio/wav')
    assert captured.get('suspected_hallucination') is False


def test_hallucination_flag_needs_known_duration():
    """Unknown duration (compressed) → never flagged (no guessing)."""
    import lib.transcription as tr
    assert tr._suspect_hallucination('x' * 5000, None) is False
    assert tr._suspect_hallucination('x' * 5000, 0) is False


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
