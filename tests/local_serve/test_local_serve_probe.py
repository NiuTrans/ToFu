#!/usr/bin/env python3
"""tests/local_serve/test_local_serve_probe.py — model-path inspection unit tests.

Covers HF directory reading (config.json + safetensors index, nested
text_config, quant flag) and the dependency-free GGUF header parser, plus
the unknown-format error paths the agent relays to users verbatim.
"""

import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

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

    def test_metadata_above_old_count_key_and_string_limits(self, tmp_path):
        path = _write_gguf(tmp_path / 'large.gguf', [
            ('ignored.' + 'k' * 1025, 'x' * ((1 << 20) + 1)),
            *[(f'ignored.{i}', i) for i in range(10001)],
            ('general.architecture', 'qwen3'),
            ('qwen3.context_length', 65536),
        ])
        result = inspect_model_path(path)
        assert result['architecture'] == 'qwen3'
        assert result['max_context'] == 65536 and 'error' not in result

    @pytest.mark.parametrize('element_type', [0, 8])
    def test_large_array_skips_to_following_metadata(self, tmp_path, element_type):
        path = tmp_path / 'array.gguf'
        key = b'ignored.array'
        prefix = (b'GGUF' + struct.pack('<IQQ', 3, 0, 2)
                  + struct.pack('<Q', len(key)) + key
                  + struct.pack('<IIQ', 9, element_type, 1000001))
        following = tmp_path / 'following.gguf'
        _write_gguf(following, [('general.architecture', 'qwen3')])
        with path.open('wb') as output:
            output.write(prefix)
            # A string array of empty strings still has a length per item.
            output.write(b'\0' * (1000001 * (8 if element_type == 8 else 1)))
            output.write(following.read_bytes()[24:])
        result = inspect_model_path(str(path))
        assert result['architecture'] == 'qwen3' and 'error' not in result

    def test_nested_arrays_do_not_depend_on_python_recursion_limit(self, tmp_path):
        path = tmp_path / 'nested.gguf'
        key = b'ignored.nested'
        data = (b'GGUF' + struct.pack('<IQQ', 3, 0, 2)
                + struct.pack('<Q', len(key)) + key + struct.pack('<I', 9)
                + struct.pack('<IQ', 9, 1) * 2048
                + struct.pack('<IQ', 0, 1) + b'x')
        following = tmp_path / 'following.gguf'
        _write_gguf(following, [('general.architecture', 'qwen3')])
        path.write_bytes(data + following.read_bytes()[24:])
        assert inspect_model_path(str(path))['architecture'] == 'qwen3'

    @pytest.mark.parametrize('value', [
        struct.pack('<IQ', 8, (1 << 64) - 1),
        struct.pack('<IIQ', 9, 0, (1 << 64) - 1),
        struct.pack('<IIQ', 9, 8, (1 << 64) - 1),
        struct.pack('<IIQ', 9, 9, (1 << 64) - 1),
        struct.pack('<I', 12) + b'x',
    ])
    def test_declared_value_past_eof_returns_structured_error(self, tmp_path, value):
        path = tmp_path / 'truncated.gguf'
        key = b'ignored'
        path.write_bytes(b'GGUF' + struct.pack('<IQQ', 3, 0, 1)
                         + struct.pack('<Q', len(key)) + key + value)
        result = inspect_model_path(str(path))
        assert result['format'] == 'unknown' and 'EOF' in result['error']

    def test_declared_metadata_count_past_eof(self, tmp_path):
        path = tmp_path / 'count.gguf'
        path.write_bytes(b'GGUF' + struct.pack('<IQQ', 3, 0, (1 << 64) - 1))
        result = inspect_model_path(str(path))
        assert result['format'] == 'unknown' and 'EOF' in result['error']

    def test_truncated_selected_string_is_not_adopted(self, tmp_path):
        path = tmp_path / 'short.gguf'
        key = b'general.architecture'
        path.write_bytes(b'GGUF' + struct.pack('<IQQ', 3, 0, 1)
                         + struct.pack('<Q', len(key)) + key
                         + struct.pack('<IQ', 8, 100) + b'qwen3')
        assert inspect_model_path(str(path))['format'] == 'unknown'

    def test_attention_dimensions_survive_late_architecture_field(self, tmp_path):
        path = _write_gguf(tmp_path / 'dimensions.gguf', [
            ('qwen3.block_count', 32), ('qwen3.embedding_length', 4096),
            ('qwen3.attention.head_count', 32), ('qwen3.attention.head_count_kv', 8),
            ('qwen3.attention.key_length', 128), ('qwen3.attention.value_length', 128),
            ('ignored.block_count', 999), ('general.architecture', 'qwen3'),
        ])
        result = inspect_model_path(path)
        assert result['attention_dimensions'] == {
            'block_count': 32, 'embedding_length': 4096,
            'attention.head_count': 32, 'attention.head_count_kv': 8,
            'attention.key_length': 128, 'attention.value_length': 128,
        }

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


def test_operator_override_is_captured_in_hardware_snapshot(tmp_path, monkeypatch):
    from lib.local_serve import _probe
    monkeypatch.setenv('OLLAMA_NUM_PARALLEL', '7')
    monkeypatch.setattr(_probe, '_probe_gpus', lambda timeout: [])
    snapshot = _probe.probe_hardware(disk_path=str(tmp_path))
    monkeypatch.setenv('OLLAMA_NUM_PARALLEL', '9')
    assert snapshot['ollama_env'] == {'OLLAMA_NUM_PARALLEL': '7'}
