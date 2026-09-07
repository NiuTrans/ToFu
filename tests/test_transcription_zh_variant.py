#!/usr/bin/env python3
"""Tests for approach-B Chinese-variant normalization of voice transcripts.

ASR models (Whisper-family / omni-chat) transcribe Mandarin into Traditional
Chinese by default; ``lib.transcription._zh`` converts the transcript to the
configured variant (Simplified by default) as a FAIL-SAFE last gate. This suite
covers the env parsing, the no-op fast paths, the fail-safe fall-throughs, the
real conversion, and integration through ``transcribe()`` — all hermetic (no
network, no configured slot).

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_transcription_zh_variant.py -v
"""

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

pytestmark = [pytest.mark.unit]

# A short Traditional-Chinese phrase and its Simplified form. Every differing
# character is a genuine variant pair, so a real conversion is observable.
_TRAD = '這個開發專案的語言設定'
_SIMP = '这个开发专案的语言设定'


def _has_zhconv():
    try:
        import zhconv  # noqa: F401
        return True
    except ImportError:
        return False


# ── zh_variant_target: env parsing ──────────────────────────────────────

def test_default_target_is_simplified(monkeypatch):
    """Unset env → default to zh-cn (Simplified)."""
    from lib.transcription import _zh
    monkeypatch.delenv('TOFU_ASR_ZH_VARIANT', raising=False)
    assert _zh.zh_variant_target() == 'zh-cn'


@pytest.mark.parametrize('val', ['', 'off', 'none', 'no', '0', 'false', 'raw',
                                 '  OFF  '])
def test_disable_tokens_return_none(monkeypatch, val):
    """Explicit off-tokens disable conversion (None)."""
    from lib.transcription import _zh
    monkeypatch.setenv('TOFU_ASR_ZH_VARIANT', val)
    assert _zh.zh_variant_target() is None


def test_explicit_traditional_target(monkeypatch):
    """A valid non-default locale is honored (case/space-insensitive)."""
    from lib.transcription import _zh
    monkeypatch.setenv('TOFU_ASR_ZH_VARIANT', '  ZH-TW ')
    assert _zh.zh_variant_target() == 'zh-tw'


def test_unknown_locale_disables_fail_safe(monkeypatch):
    """A typo'd locale disables conversion rather than raising later."""
    from lib.transcription import _zh
    monkeypatch.setenv('TOFU_ASR_ZH_VARIANT', 'klingon')
    assert _zh.zh_variant_target() is None


# ── normalize_zh_variant: no-op fast paths ──────────────────────────────

def test_empty_text_is_returned_unchanged(monkeypatch):
    from lib.transcription import _zh
    monkeypatch.delenv('TOFU_ASR_ZH_VARIANT', raising=False)
    assert _zh.normalize_zh_variant('') == ''


def test_non_cjk_text_skips_conversion(monkeypatch):
    """English-only text short-circuits before touching the converter."""
    from lib.transcription import _zh
    monkeypatch.delenv('TOFU_ASR_ZH_VARIANT', raising=False)

    def _boom(*a, **k):
        raise AssertionError('converter must not run on non-CJK text')
    monkeypatch.setattr(_zh, '_has_cjk', lambda t: False)
    # _has_cjk stubbed to False → returns input without importing zhconv.
    assert _zh.normalize_zh_variant('hello world') == 'hello world'


def test_disabled_returns_raw_even_for_cjk(monkeypatch):
    """When conversion is disabled, Traditional text is returned untouched."""
    from lib.transcription import _zh
    monkeypatch.setenv('TOFU_ASR_ZH_VARIANT', 'off')
    assert _zh.normalize_zh_variant(_TRAD) == _TRAD


# ── normalize_zh_variant: fail-safe fall-throughs ───────────────────────

def test_missing_zhconv_falls_through(monkeypatch):
    """A missing zhconv package returns the raw text (no crash)."""
    from lib.transcription import _zh
    monkeypatch.delenv('TOFU_ASR_ZH_VARIANT', raising=False)

    import builtins
    real_import = builtins.__import__

    def _fake_import(name, *a, **k):
        if name == 'zhconv':
            raise ImportError('simulated missing zhconv')
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, '__import__', _fake_import)

    assert _zh.normalize_zh_variant(_TRAD) == _TRAD


def test_converter_error_falls_through(monkeypatch):
    """A converter exception returns the raw text (never degrades)."""
    from lib.transcription import _zh
    monkeypatch.delenv('TOFU_ASR_ZH_VARIANT', raising=False)

    import types
    fake = types.ModuleType('zhconv')

    def _boom(text, locale):
        raise RuntimeError('boom')
    fake.convert = _boom
    monkeypatch.setitem(sys.modules, 'zhconv', fake)

    assert _zh.normalize_zh_variant(_TRAD) == _TRAD


def test_conversion_via_stub_seam(monkeypatch):
    """With a stubbed zhconv, the target locale is passed through and the
    converted text is returned — proves the seam is load-bearing."""
    from lib.transcription import _zh
    monkeypatch.delenv('TOFU_ASR_ZH_VARIANT', raising=False)

    import types
    captured = {}
    fake = types.ModuleType('zhconv')

    def _convert(text, locale):
        captured['text'] = text
        captured['locale'] = locale
        return _SIMP
    fake.convert = _convert
    monkeypatch.setitem(sys.modules, 'zhconv', fake)

    out = _zh.normalize_zh_variant(_TRAD)
    assert out == _SIMP
    assert captured['text'] == _TRAD
    assert captured['locale'] == 'zh-cn'


@pytest.mark.skipif(not _has_zhconv(), reason='zhconv not installed')
def test_real_zhconv_traditional_to_simplified(monkeypatch):
    """End-to-end with the real zhconv package (when present)."""
    from lib.transcription import _zh
    monkeypatch.delenv('TOFU_ASR_ZH_VARIANT', raising=False)
    assert _zh.normalize_zh_variant(_TRAD) == _SIMP


# ── Integration through transcribe() ────────────────────────────────────

def _make_wav(seconds=1.0, sample_rate=8000, amplitude=0.3, freq=220.0):
    import math
    n = int(seconds * sample_rate)
    byte_rate = sample_rate * 2
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


class _FakeSlot:
    def __init__(self):
        self.model = 'gpt-4o-transcribe'
        self.provider_id = 'prov_test'
        self.key_name = 'key_0'
        self.api_key = 'sk-test'
        self.base_url = 'https://stt.example/v1'
        self.extra_headers = {}
        self.capabilities = {'transcription'}
        self.oauth = ''

    def score(self):
        return 1.0


def test_transcribe_normalizes_variant(monkeypatch):
    """transcribe() runs the provider text through normalize_zh_variant so the
    returned transcript is in the target variant."""
    import lib.transcription as tr
    monkeypatch.delenv('TOFU_ASR_ZH_VARIANT', raising=False)
    monkeypatch.setattr(tr, '_transcription_slots', lambda: [_FakeSlot()])
    monkeypatch.setattr(tr, '_post_to_provider', lambda *a, **k: _TRAD)
    # Stub the converter so the test does not depend on zhconv being installed.
    monkeypatch.setattr(tr, 'normalize_zh_variant',
                        lambda text: _SIMP if text == _TRAD else text)

    result = tr.transcribe(_make_wav(1.0), 'clip.wav', content_type='audio/wav')
    assert result.text == _SIMP


def test_transcribe_variant_noop_leaves_text(monkeypatch):
    """NEUTER: when normalization is disabled, transcribe() returns the raw
    (Traditional) provider text — proves the gate is what changes the output."""
    import lib.transcription as tr
    monkeypatch.setenv('TOFU_ASR_ZH_VARIANT', 'off')
    monkeypatch.setattr(tr, '_transcription_slots', lambda: [_FakeSlot()])
    monkeypatch.setattr(tr, '_post_to_provider', lambda *a, **k: _TRAD)

    result = tr.transcribe(_make_wav(1.0), 'clip.wav', content_type='audio/wav')
    assert result.text == _TRAD


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
