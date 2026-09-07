"""tests/test_multimodal_token_estimate.py — Multimodal token estimates.

Covers the Codex-inspired estimator upgrades (codex-rs utils/audio +
context_manager/history.rs):

  1. ``input_audio`` blocks count by DURATION (10 tokens/sec, WAV-decoded)
     instead of slipping past the estimator as ZERO tokens.
  2. Undecodable audio falls back to a size heuristic; results are cached
     by payload sha1.
  3. ``image_url`` blocks honor ``detail='low'`` (flat 85) vs the
     conservative high-detail default.
"""

from __future__ import annotations

import base64
import io
import math
import unittest
import wave

import pytest

pytestmark = pytest.mark.unit

# Boot the Flask→Quart shim BEFORE any lib.* imports (see test_hook_taxonomy).
import importlib.util as _importlib_util
_spec = _importlib_util.spec_from_file_location(
    'server_for_shim_mm_test', 'server.py')
_mod = _importlib_util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
del _spec, _mod, _importlib_util

from lib.tasks_pkg.compaction._constants import (
    _IMAGE_TOKENS_DEFAULT,
    _IMAGE_TOKENS_LOW,
)
from lib.tasks_pkg.compaction._tokens import (
    _AUDIO_TOKENS_PER_SECOND,
    _audio_token_cache,
    _estimate_audio_tokens,
    _estimate_msg_tokens,
    _image_block_tokens,
)


def _wav_bytes(seconds: float, rate: int = 8000) -> bytes:
    """Synthesize a real PCM WAV of the given duration (silence)."""
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b'\x00\x00' * int(seconds * rate))
    return buf.getvalue()


def _audio_msg(payload_b64: str, fmt: str = 'wav') -> dict:
    return {'role': 'user', 'content': [
        {'type': 'input_audio',
         'input_audio': {'data': payload_b64, 'format': fmt}},
    ]}


class TestAudioEstimate(unittest.TestCase):

    def setUp(self):
        _audio_token_cache.clear()

    def tearDown(self):
        _audio_token_cache.clear()

    def test_wav_duration_based(self):
        b64 = base64.b64encode(_wav_bytes(3.0)).decode('ascii')
        tokens = _estimate_audio_tokens(_audio_msg(b64)['content'][0])
        self.assertEqual(tokens, math.ceil(3.0 * _AUDIO_TOKENS_PER_SECOND))

    def test_previously_zero_now_counted(self):
        b64 = base64.b64encode(_wav_bytes(2.0)).decode('ascii')
        tokens = _estimate_msg_tokens(_audio_msg(b64))
        self.assertGreater(tokens, 0,
                           'audio blocks must no longer count as zero')

    def test_undecodable_falls_back_to_size(self):
        garbage = base64.b64encode(b'not real audio at all' * 10).decode()
        tokens = _estimate_audio_tokens(_audio_msg(garbage)['content'][0])
        self.assertEqual(tokens, max(1, len(garbage) // 4))

    def test_data_url_shape_supported(self):
        b64 = base64.b64encode(_wav_bytes(1.0)).decode('ascii')
        block = {'type': 'input_audio',
                 'audio_url': f'data:audio/wav;base64,{b64}'}
        tokens = _estimate_audio_tokens(block)
        self.assertEqual(tokens, math.ceil(1.0 * _AUDIO_TOKENS_PER_SECOND))

    def test_cache_hits_and_lru_bound(self):
        b64 = base64.b64encode(_wav_bytes(1.0)).decode('ascii')
        block = _audio_msg(b64)['content'][0]
        first = _estimate_audio_tokens(block)
        second = _estimate_audio_tokens(block)
        self.assertEqual(first, second)
        self.assertEqual(len(_audio_token_cache), 1)
        # LRU stays bounded at 32 entries.
        for k in range(40):
            payload = base64.b64encode(bytes([k]) * 64).decode()
            _estimate_audio_tokens(
                {'input_audio': {'data': payload, 'format': 'bin'}})
        self.assertLessEqual(len(_audio_token_cache), 32)

    def test_empty_payload_zero(self):
        self.assertEqual(
            _estimate_audio_tokens({'input_audio': {'data': '',
                                                    'format': 'wav'}}), 0)


class TestImageDetailEstimate(unittest.TestCase):

    def test_low_detail_uses_low_flat_rate(self):
        block = {'type': 'image_url',
                 'image_url': {'url': 'data:image/png;base64,xx',
                               'detail': 'low'}}
        self.assertEqual(_image_block_tokens(block), _IMAGE_TOKENS_LOW)

    def test_default_and_high_use_default(self):
        for detail in (None, 'high', 'auto'):
            block = {'type': 'image_url',
                     'image_url': {'url': 'u', **({'detail': detail}
                                                  if detail else {})}}
            self.assertEqual(_image_block_tokens(block),
                             _IMAGE_TOKENS_DEFAULT)

    def test_msg_tokens_mixed_content(self):
        msg = {'role': 'user', 'content': [
            {'type': 'text', 'text': 'look at these'},
            {'type': 'image_url',
             'image_url': {'url': 'u1', 'detail': 'low'}},
            {'type': 'image_url', 'image_url': {'url': 'u2'}},
        ]}
        tokens = _estimate_msg_tokens(msg)
        self.assertGreaterEqual(tokens, _IMAGE_TOKENS_LOW + _IMAGE_TOKENS_DEFAULT)


if __name__ == '__main__':
    unittest.main()
