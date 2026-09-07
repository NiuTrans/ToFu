#!/usr/bin/env python3
"""tests/test_local_serve_probe.py — model-path inspection unit tests.

Covers HF directory reading (config.json + safetensors index, nested
text_config, quant flag) and the dependency-free GGUF header parser, plus
the unknown-format error paths the agent relays to users verbatim.
"""

import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from lib.local_serve._probe import inspect_model_path

pytestmark = pytest.mark.unit


def _write_hf_dir(tmp_path, config, *, index_total=None, shard_bytes=0):
    d = tmp_path / 'Qwen3-8B'
    d.mkdir()
    (d / 'config.json').write_text(json.dumps(config), encoding='utf-8')
    if index_total is not None:
        (d / 'model.safetensors.index.json').write_text(
            json.dumps({'metadata': {'total_size': index_total},
                        'weight_map': {}}), encoding='utf-8')
    if shard_bytes:
        with open(d / 'model.safetensors', 'wb') as f:
            f.truncate(shard_bytes)
    return str(d)


def _write_gguf(path, meta):
    """Serialise a minimal GGUF v3 header with the given metadata KV pairs."""
    out = bytearray()
    out += b'GGUF'
    out += struct.pack('<I', 3)          # version
    out += struct.pack('<Q', 0)          # tensor_count
    out += struct.pack('<Q', len(meta))  # metadata_kv_count
    for key, value in meta:
        k = key.encode()
        out += struct.pack('<Q', len(k)) + k
        if isinstance(value, str):
            out += struct.pack('<I', 8)  # STRING
            v = value.encode()
            out += struct.pack('<Q', len(v)) + v
        else:
            out += struct.pack('<I', 4)  # UINT32
            out += struct.pack('<I', value)
    path.write_bytes(bytes(out))
    return str(path)


class TestHFInspection:
    def test_basic_hf_dir(self, tmp_path):
        p = _write_hf_dir(tmp_path, {
            'architectures': ['Qwen3ForCausalLM'],
            'model_type': 'qwen3',
            'max_position_embeddings': 40960,
            'torch_dtype': 'bfloat16',
        }, index_total=16_000_000_000)
        r = inspect_model_path(p)
        assert r['format'] == 'hf'
        assert r['architecture'] == 'Qwen3ForCausalLM'
        assert r['max_context'] == 40960
        assert r['weight_bytes'] == 16_000_000_000
        assert r['param_count_estimate'] == 8_000_000_000  # bf16 → 2 bytes
        assert r['served_name'] == 'Qwen3-8B'
        assert 'error' not in r

    def test_nested_text_config(self, tmp_path):
        p = _write_hf_dir(tmp_path, {
            'architectures': ['Gemma3ForConditionalGeneration'],
            'model_type': 'gemma3',
            'text_config': {'max_position_embeddings': 131072,
                            'hidden_size': 2560},
        }, index_total=8_000_000_000)
        r = inspect_model_path(p)
        assert r['max_context'] == 131072
        assert r['hidden_size'] == 2560

    def test_quantization_flag(self, tmp_path):
        p = _write_hf_dir(tmp_path, {
            'architectures': ['Qwen3ForCausalLM'],
            'quantization_config': {'quant_method': 'awq'},
        }, index_total=5_000_000_000)
        r = inspect_model_path(p)
        assert r['quantization'] == 'awq'

    def test_shard_file_fallback(self, tmp_path):
        p = _write_hf_dir(tmp_path, {'architectures': ['X']},
                          shard_bytes=3_000_000)
        r = inspect_model_path(p)
        assert r['weight_bytes'] == 3_000_000
        assert r['weight_source'] == 'shard-files'

    def test_missing_weights_flagged(self, tmp_path):
        p = _write_hf_dir(tmp_path, {'architectures': ['X']})
        r = inspect_model_path(p)
        assert r['format'] == 'hf'
        assert 'error' in r  # directory without weights is incomplete

    def test_dir_without_config(self, tmp_path):
        d = tmp_path / 'empty'
        d.mkdir()
        r = inspect_model_path(str(d))
        assert r['format'] == 'unknown'
        assert 'config.json' in r['error']


class TestGGUFInspection:
    def test_gguf_header(self, tmp_path):
        p = _write_gguf(tmp_path / 'qwen3-8b-q4_k_m.gguf', [
            ('general.architecture', 'qwen3'),
            ('general.name', 'Qwen3 8B'),
            ('general.file_type', 15),           # Q4_K_M
            ('qwen3.context_length', 40960),
        ])
        r = inspect_model_path(p)
        assert r['format'] == 'gguf'
        assert r['architecture'] == 'qwen3'
        assert r['quantization'] == 'Q4_K_M'
        assert r['max_context'] == 40960
        assert r['served_name'] == 'qwen3-8b-q4_k_m'

    def test_bad_magic(self, tmp_path):
        f = tmp_path / 'fake.gguf'
        f.write_bytes(b'NOTGGUF-content')
        r = inspect_model_path(str(f))
        assert r['format'] == 'unknown'
        assert 'GGUF' in r['error']

    def test_truncated_header(self, tmp_path):
        f = tmp_path / 'trunc.gguf'
        f.write_bytes(b'GGUF' + struct.pack('<I', 3))
        r = inspect_model_path(str(f))
        assert r['format'] == 'unknown'
        assert 'error' in r


class TestUnknownPaths:
    def test_missing_path(self):
        r = inspect_model_path('/nonexistent/definitely-not-here')
        assert r['format'] == 'unknown'
        assert '不存在' in r['error']

    def test_empty_path(self):
        r = inspect_model_path('')
        assert r['format'] == 'unknown'

    def test_unknown_extension(self, tmp_path):
        f = tmp_path / 'model.onnx'
        f.write_bytes(b'x')
        r = inspect_model_path(str(f))
        assert r['format'] == 'unknown'
        assert '无法识别' in r['error']
