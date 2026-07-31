#!/usr/bin/env python3
"""Unit tests for lib/tts (Layer 2 of the paper-podcast feature).

Covers: taxonomy registration ('tts' as a non-chat cap), the config-driven
voice/format/speed resolution (owner directive: NO hardcoded model/voice),
the /audio/speech provider seam with a mocked http_post, multi-slot
fallback, MIME sniffing, WAV concat/duration helpers, and the access-matrix
probe. Includes NEUTERs proving the voice resolution and MIME sniffing are
load-bearing (amputate → the affected behavior visibly changes).

Run standalone (python tests/test_tts.py) or via pytest.
"""

import io
import os
import sys
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import pytest
    pytestmark = pytest.mark.unit
except ImportError:
    pass


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def _tiny_wav(duration_s=0.1, rate=8000) -> bytes:
    pcm = b'\x00\x00' * int(duration_s * rate)
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


class _FakeSlot:
    def __init__(self, model='unit-tts', key_name='k0', score=1.0,
                 caps=frozenset({'tts'}), oauth=False, base_url='https://tts.example/v1'):
        self.model = model
        self.key_name = key_name
        self.capabilities = caps
        self.oauth = oauth
        self.base_url = base_url
        self.api_key = 'sk-test'
        self.extra_headers = {}
        self.provider_id = 'prov0'
        self._score = score

    def score(self):
        return self._score


def _install(slot_list=None, post_fn=None, cfg=None):
    """Patch lib.tts seams (slots / provider POST / config); return restore."""
    import lib.tts as T
    orig = {k: getattr(T, k, None) for k in
            ('_tts_slots', '_post_speech', '_load_tts_config')}
    if slot_list is not None:
        T._tts_slots = lambda: slot_list
    if post_fn is not None:
        T._post_speech = post_fn
    if cfg is not None:
        T._load_tts_config = lambda: cfg

    def restore():
        for k, v in orig.items():
            if v is not None:
                setattr(T, k, v)
    return restore


# ═══ Taxonomy ═══

def test_taxonomy_registers_tts():
    from lib.model_info.capability_taxonomy import (
        CAPABILITY_SEMANTICS, CHAT_EXCLUDED_CAPS, DISPATCHER_NON_CHAT_CAPS,
        is_chat_model, taxonomy_payload)
    assert 'tts' in CHAT_EXCLUDED_CAPS
    assert 'tts' in DISPATCHER_NON_CHAT_CAPS
    sem = CAPABILITY_SEMANTICS['tts']
    assert sem['endpoint'] == 'audio_speech' and sem['role'] == 'non-chat'
    assert sem['in_chat_picker'] is False and sem['is_dispatch_chat'] is False
    assert is_chat_model(['tts']) is False
    assert is_chat_model(['text', 'vision']) is True
    payload = taxonomy_payload()
    assert 'tts' in payload['chat_excluded_caps']
    assert 'tts' in payload['capability_semantics']
    _ok('taxonomy: tts registered as non-chat cap, chat pickers exclude it')


def test_js_fallback_parity():
    """static/js/core/model_caps.js fallback must contain every excluded cap."""
    from lib.model_info.capability_taxonomy import CHAT_EXCLUDED_CAPS
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'static', 'js', 'core', 'model_caps.js')
    with open(path, encoding='utf-8') as f:
        src = f.read()
    for cap in CHAT_EXCLUDED_CAPS:
        assert f"'{cap}'" in src, f'model_caps.js fallback missing {cap!r}'
    _ok('parity: model_caps.js fallback covers every CHAT_EXCLUDED_CAPS entry')


def test_slot_config_reference_entries():
    from lib.llm_dispatch.config._slots import DEFAULT_SLOT_CONFIGS
    for name in ('tts-1', 'tts-1-hd', 'gpt-4o-mini-tts'):
        cfg = DEFAULT_SLOT_CONFIGS.get(name)
        assert cfg, f'{name} missing from reference table'
        assert cfg['caps'] == {'tts'}, cfg
    _ok('slots: public TTS model names pre-seeded with the tts cap')


# ═══ Config resolution ═══

def test_config_defaults_without_file():
    import lib.tts as T
    restore = _install(cfg={})
    try:
        assert T.default_voice() == 'alloy'          # documented fallback only
        assert T.default_format() == 'wav'
        assert T.default_speed() == 1.0
        assert T.max_input_chars() == 2000
    finally:
        restore()
    _ok('config: missing tts.json → documented fallbacks (no hardcoded model)')


def test_config_file_wins():
    import lib.tts as T
    restore = _install(cfg={'default_voice': 'zh-female-1', 'default_format': 'mp3',
                            'speed': 1.2, 'max_input_chars': 900})
    try:
        assert T.default_voice() == 'zh-female-1'
        assert T.default_format() == 'mp3'
        assert T.default_speed() == 1.2
        assert T.max_input_chars() == 900
    finally:
        restore()
    _ok('config: tts.json values win over fallbacks (voice/format/speed/chunk)')


def test_availability_and_model_listing():
    import lib.tts as T
    restore = _install(slot_list=[])
    try:
        assert T.tts_available() is False
        assert T.list_tts_models() == []
    finally:
        restore()
    s = _FakeSlot()
    restore = _install(slot_list=[s, _FakeSlot(key_name='k1')])
    try:
        assert T.tts_available() is True
        models = T.list_tts_models()
        assert models[0]['model'] == 'unit-tts'
    finally:
        restore()
    _ok('availability: False with zero tts slots (degrade signal), True with slots')


# ═══ Synthesize ═══

def test_synthesize_happy_path():
    import lib.tts as T
    seen = {}
    wav = _tiny_wav()

    def _post(slot, text, *, voice, fmt, speed):
        seen.update(model=slot.model, text=text, voice=voice, fmt=fmt, speed=speed)
        return wav

    restore = _install(slot_list=[_FakeSlot()], post_fn=_post, cfg={})
    try:
        res = T.synthesize('你好,世界。')
        assert res.audio_bytes == wav
        assert res.mime == 'audio/wav'           # sniffed from RIFF header
        assert res.model == 'unit-tts'
        assert seen['voice'] == 'alloy'          # fallback voice resolved
        assert seen['fmt'] == 'wav'
    finally:
        restore()
    _ok('synthesize: bytes + sniffed WAV mime; fallback voice resolved into payload')


def test_synthesize_voice_precedence():
    import lib.tts as T
    seen = {}

    def _post(slot, text, *, voice, fmt, speed):
        seen['voice'] = voice
        return _tiny_wav()

    # request voice > config voice > fallback
    restore = _install(slot_list=[_FakeSlot()], post_fn=_post,
                       cfg={'default_voice': 'cfg-voice'})
    try:
        T.synthesize('x', voice='req-voice')
        assert seen['voice'] == 'req-voice'
        T.synthesize('x')
        assert seen['voice'] == 'cfg-voice'
    finally:
        restore()
    _ok('synthesize: voice precedence request > tts.json > fallback')


def test_synthesize_no_slot_503():
    import lib.tts as T
    restore = _install(slot_list=[])
    try:
        try:
            T.synthesize('x')
            raise AssertionError('must raise TTSError')
        except T.TTSError as e:
            assert e.status == 503, e.status
            assert 'tts' in e.detail
    finally:
        restore()
    _ok('synthesize: no tts slot → TTSError 503 (degrade path trigger)')


def test_synthesize_slot_fallback():
    import lib.tts as T
    calls = []
    wav = _tiny_wav()

    def _post(slot, text, *, voice, fmt, speed):
        calls.append(slot.key_name)
        if slot.key_name == 'k0':
            raise T.TTSError('upstream 500', status=502)
        return wav

    slots = [_FakeSlot(key_name='k0', score=0.5), _FakeSlot(key_name='k1', score=2.0)]
    restore = _install(slot_list=slots, post_fn=_post, cfg={})
    try:
        res = T.synthesize('x')
        assert res.audio_bytes == wav
        assert calls == ['k0', 'k1'], calls  # best score first, then fallback
    finally:
        restore()
    _ok('synthesize: failing slot falls through to the next (score order)')


def test_synthesize_neuter_voice_resolution():
    """NEUTER: amputate voice resolution (default_voice → '') and the payload
    must lose its voice — proving the resolution chain feeds the request."""
    import lib.tts as T
    seen = {}

    def _post(slot, text, *, voice, fmt, speed):
        seen['voice'] = voice
        return _tiny_wav()

    restore = _install(slot_list=[_FakeSlot()], post_fn=_post, cfg={})
    orig = T.default_voice
    try:
        T.synthesize('x')
        assert seen['voice'], 'baseline: payload carries a resolved voice'
        T.default_voice = lambda: ''   # amputate
        T.synthesize('x')
        assert seen['voice'] == '', 'NEUTER failed: voice still resolved'
    finally:
        T.default_voice = orig
        restore()
    _ok('NEUTER: amputating default_voice drops the payload voice — chain load-bearing')


def test_sniff_mime():
    from lib.tts._synthesize import _sniff_mime, sniff_container
    assert sniff_container(_tiny_wav()) == 'wav'
    assert sniff_container(b'ID3' + b'\x00' * 10) == 'mp3'
    assert sniff_container(b'\xff\xfb' + b'\x00' * 10) == 'mp3'
    assert sniff_container(b'fLaC' + b'\x00' * 10) == 'flac'
    assert sniff_container(b'\x01\x02\x03\x04') == 'unknown'
    assert _sniff_mime(b'\x01\x02\x03\x04', 'mp3') == 'audio/mpeg'   # fmt fallback
    assert _sniff_mime(b'\x01\x02\x03\x04', 'wav') == 'audio/wav'
    _ok('mime sniff: RIFF/ID3/frame-sync/fLaC detected; fmt fallback works')


def test_sniff_neuter():
    """NEUTER: without the container sniff, WAV bytes fall through to the fmt
    map — if fmt says 'mp3' the mime is WRONG. Proves sniffing outranks fmt."""
    from lib.tts import _synthesize as SZ
    wav = _tiny_wav()
    assert SZ._sniff_mime(wav, 'mp3') == 'audio/wav'   # sniff beats wrong fmt
    orig = SZ.sniff_container
    SZ.sniff_container = lambda data: 'unknown'        # amputate
    try:
        assert SZ._sniff_mime(wav, 'mp3') == 'audio/mpeg', \
            'NEUTER failed: sniff must be what corrects a wrong fmt hint'
    finally:
        SZ.sniff_container = orig
    _ok('NEUTER: amputating sniff_container lets a wrong fmt through — sniff load-bearing')


# ═══ Audio helpers ═══

def test_wav_helpers_and_concat():
    import lib.tts as T
    a, b = _tiny_wav(0.2), _tiny_wav(0.3)
    assert abs(T.wav_duration(a) - 0.2) < 0.01
    joined = T.concat_wavs([a, b], pause_ms=[0, 500])
    dur = T.wav_duration(joined)
    assert abs(dur - (0.2 + 0.3 + 0.5)) < 0.02, dur
    ch, sw, rate, frames = T.wav_params(joined)
    assert (ch, sw, rate) == (1, 2, 8000)
    silence = T.silence_wav_bytes(0.25, framerate=8000)
    assert abs(T.wav_duration(silence) - 0.25) < 0.01
    _ok('audio helpers: duration exact, concat + 500ms pause lands at 1.0s')


def test_mp3_duration_estimate():
    import lib.tts as T
    assert T.estimate_mp3_duration(b'\x00' * 16000) == 1.0  # 128kbps → 16KB/s
    assert T.estimate_mp3_duration(b'') == 0.0
    _ok('audio helpers: mp3 duration estimate at 128kbps')


# ═══ Probe ═══

def test_probe_tts_cell():
    import lib.provider_probe as PP

    class _Resp:
        def __init__(self, code, body):
            self.status_code = code
            self._body = body
            self.text = body.decode('latin-1', errors='replace')[:400]
            self.content = body

        def json(self):
            raise ValueError('not json')

    seen = {}

    def _post(url, json=None, files=None, data=None, headers=None, timeout=None):
        seen.update(url=url, payload=json)
        return _Resp(200, _tiny_wav())

    orig = PP.http_post if hasattr(PP, 'http_post') else None
    import lib.http_client as HC
    orig_hc = HC.http_post
    # _post_and_classify imports http_post lazily inside the function
    HC.http_post = _post
    try:
        status, detail = PP.probe_tts_cell('https://gw.example/v1', 'k', 'tts-1', {}, 10)
        assert status == 'ok', (status, detail)
        assert seen['url'] == 'https://gw.example/v1/audio/speech'
        assert seen['payload']['model'] == 'tts-1'
        assert seen['payload']['input'] == 'ping'
        assert seen['payload']['voice'], 'probe must carry a resolved voice'
    finally:
        HC.http_post = orig_hc
    _ok('probe: /audio/speech 200 + WAV payload → ok, request shape correct')


def test_probe_tts_cell_bad_shape_and_404():
    import lib.provider_probe as PP
    import lib.http_client as HC

    class _Resp:
        def __init__(self, code, body=b'{}'):
            self.status_code = code
            self.text = body.decode('latin-1', errors='replace')[:400]
            self.content = body

        def json(self):
            return {}

    orig_hc = HC.http_post
    try:
        HC.http_post = lambda *a, **k: _Resp(200, b'{"error":"no audio"}')
        status, detail = PP.probe_tts_cell('https://gw/v1', 'k', 'tts-1', {}, 10)
        assert status == 'error' and 'non-audio' in detail, (status, detail)
        HC.http_post = lambda *a, **k: _Resp(404, b'model_not_found')
        status, _d = PP.probe_tts_cell('https://gw/v1', 'k', 'tts-1', {}, 10)
        assert status == 'not_found', status
    finally:
        HC.http_post = orig_hc
    _ok('probe: non-audio 200 → error shape; 404 → not_found')


def test_probe_fn_registration():
    import lib.provider_probe as PP
    assert PP.nonchat_probe_fn(['tts']) is PP.probe_tts_cell
    # priority: transcription still outranks tts for multi-cap cells
    assert PP.nonchat_probe_fn(['tts', 'transcription']) is PP.probe_transcription_cell
    assert PP.nonchat_probe_fn(['unknown_cap']) is None
    _ok('probe: tts registered in nonchat_probe_fn with correct priority')


def main():
    print()
    print(_color('═══ lib/tts Unit Tests ═══', '36'))
    print()
    tests = [
        test_taxonomy_registers_tts,
        test_js_fallback_parity,
        test_slot_config_reference_entries,
        test_config_defaults_without_file,
        test_config_file_wins,
        test_availability_and_model_listing,
        test_synthesize_happy_path,
        test_synthesize_voice_precedence,
        test_synthesize_no_slot_503,
        test_synthesize_slot_fallback,
        test_synthesize_neuter_voice_resolution,
        test_sniff_mime,
        test_sniff_neuter,
        test_wav_helpers_and_concat,
        test_mp3_duration_estimate,
        test_probe_tts_cell,
        test_probe_tts_cell_bad_shape_and_404,
        test_probe_fn_registration,
    ]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
    print()
    print(_color(f'═══ ALL {len(tests)} TTS TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
