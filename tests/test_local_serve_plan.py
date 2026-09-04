#!/usr/bin/env python3
"""tests/test_local_serve_plan.py — launch policy unit tests.

Pins the deterministic engine-selection and resource-tier tables: which
engine wins for (format, GPU) combinations, the exact argv/env shape of
each tier, the OOM degradation ladders, and the infeasibility guidance the
agent relays to users. These tests are the executable spec for
lib/local_serve/_plan.py — if a table entry changes on purpose, change the
pin here in the same commit.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from lib.local_serve._plan import ENGINE_SPECS, plan_launch

pytestmark = pytest.mark.unit

GiB = 1 << 30


def _gpu(total=24 * GiB, free=None, index=0):
    return {'index': index, 'name': 'Test GPU',
            'vram_total_bytes': total,
            'vram_free_bytes': total if free is None else free,
            'driver': '560.0', 'compute_cap': '8.9'}


def _hw(gpus=(), ram_available=64 * GiB, cpu=8):
    return {'gpus': list(gpus), 'ram_total_bytes': 128 * GiB,
            'ram_available_bytes': ram_available, 'cpu_count': cpu,
            'disk_free_bytes': 500 * GiB}


def _hf(weight_bytes, ctx=40960):
    return {'format': 'hf', 'path': '/models/Qwen3-8B',
            'architecture': 'Qwen3ForCausalLM', 'model_type': 'qwen3',
            'max_context': ctx, 'dtype': 'bfloat16',
            'weight_bytes': weight_bytes, 'served_name': 'Qwen3-8B'}


def _gguf(weight_bytes, ctx=40960):
    return {'format': 'gguf', 'path': '/models/qwen3-8b-q4_k_m.gguf',
            'architecture': 'qwen3', 'max_context': ctx,
            'quantization': 'Q4_K_M', 'weight_bytes': weight_bytes,
            'served_name': 'qwen3-8b-q4_k_m'}


class TestEngineSelection:
    def test_hf_gpu_prefers_vllm(self):
        p = plan_launch(_hf(8 * GiB), _hw(gpus=[_gpu()]))
        assert p['ok'] and p['engine'] == 'vllm'
        assert p['alternatives'] == ['sglang']

    def test_hf_no_gpu_infeasible_with_guidance(self):
        p = plan_launch(_hf(8 * GiB), _hw())
        assert not p['ok']
        assert 'GGUF' in p['error']

    def test_gguf_gpu_prefers_llamacpp(self):
        p = plan_launch(_gguf(5 * GiB), _hw(gpus=[_gpu()]))
        assert p['ok'] and p['engine'] == 'llamacpp'
        assert p['alternatives'] == ['ollama']

    def test_gguf_no_gpu_llamacpp_cpu(self):
        p = plan_launch(_gguf(5 * GiB), _hw())
        assert p['ok'] and p['engine'] == 'llamacpp' and p['tier'] == 'cpu'

    def test_engine_pin_respected(self):
        p = plan_launch(_hf(8 * GiB), _hw(gpus=[_gpu()]), engine='sglang')
        assert p['ok'] and p['engine'] == 'sglang'

    def test_engine_pin_incompatible_format(self):
        p = plan_launch(_hf(8 * GiB), _hw(gpus=[_gpu()]), engine='ollama')
        assert not p['ok'] and 'ollama' in p['error'].lower() or 'Ollama' in p['error']
        p2 = plan_launch(_gguf(5 * GiB), _hw(gpus=[_gpu()]), engine='vllm')
        assert not p2['ok']

    def test_unknown_engine(self):
        p = plan_launch(_hf(8 * GiB), _hw(gpus=[_gpu()]), engine='foo')
        assert not p['ok']

    def test_uninspectable_model(self):
        p = plan_launch({'format': 'unknown', 'error': '路径不存在'}, _hw())
        assert not p['ok'] and p['error'] == '路径不存在'


class TestVllmTiers:
    def test_comfortable(self):
        # 8 GiB weights * 1.2 = 9.6 GiB ≤ 70% of 24 GiB → comfortable
        p = plan_launch(_hf(8 * GiB), _hw(gpus=[_gpu(24 * GiB)]))
        argv = p['argv']
        assert p['tier'] == 'comfortable'
        assert argv[:2] == ['vllm', 'serve']
        i = argv.index('--gpu-memory-utilization')
        assert argv[i + 1] == '0.90'
        i = argv.index('--max-model-len')
        assert argv[i + 1] == 'auto'
        assert '--enforce-eager' not in argv

    def test_tight(self):
        # 16 GiB * 1.2 = 19.2 GiB → 80% of 24 GiB → tight
        p = plan_launch(_hf(16 * GiB), _hw(gpus=[_gpu(24 * GiB)]))
        argv = p['argv']
        assert p['tier'] == 'tight'
        assert argv[argv.index('--max-model-len') + 1] == '16384'
        assert argv[argv.index('--max-num-seqs') + 1] == '8'
        assert p['degrade']  # ladder exists
        assert any('4096' in str(s.get('replace', {}).values())
                   for s in p['degrade'])

    def test_extreme(self):
        # 21 GiB * 1.2 = 25.2 GiB → 105%... just over; use 19 GiB → 95%
        p = plan_launch(_hf(19 * GiB), _hw(gpus=[_gpu(24 * GiB)]))
        assert p['tier'] == 'extreme'
        assert '--enforce-eager' in p['argv']
        assert p['argv'][p['argv'].index('--max-model-len') + 1] == '4096'

    def test_infeasible_overweight(self):
        p = plan_launch(_hf(24 * GiB), _hw(gpus=[_gpu(24 * GiB)]))
        assert not p['ok']
        assert '量化' in p['error'] or 'GGUF' in p['error']

    def test_free_vram_overrides_total(self):
        # Card is 24 GiB but 20 GiB is already eaten by another process.
        p = plan_launch(_hf(8 * GiB), _hw(gpus=[_gpu(24 * GiB, free=4 * GiB)]))
        assert not p['ok']

    def test_occupied_gpu_note(self):
        p = plan_launch(_hf(2 * GiB), _hw(gpus=[_gpu(24 * GiB, free=20 * GiB)]))
        assert p['ok']
        assert any('已被占用' in n for n in p['notes'])

    def test_ctx_cap_respects_model_limit(self):
        p = plan_launch(_hf(16 * GiB, ctx=8192), _hw(gpus=[_gpu(24 * GiB)]))
        assert p['argv'][p['argv'].index('--max-model-len') + 1] == '8192'

    def test_cuda_visible_devices_pins_gpu(self):
        p = plan_launch(_hf(8 * GiB), _hw(gpus=[_gpu(index=1)]))
        assert p['env']['CUDA_VISIBLE_DEVICES'] == '1'
        assert '--port' in p['argv']


class TestSglangTiers:
    def test_comfortable(self):
        p = plan_launch(_hf(8 * GiB), _hw(gpus=[_gpu()]), engine='sglang')
        argv = p['argv']
        assert argv[1:4] == ['-m', 'sglang.launch_server', '--model-path']
        assert argv[argv.index('--mem-fraction-static') + 1] == '0.85'

    def test_tight(self):
        p = plan_launch(_hf(16 * GiB), _hw(gpus=[_gpu()]), engine='sglang')
        argv = p['argv']
        assert argv[argv.index('--mem-fraction-static') + 1] == '0.80'
        assert argv[argv.index('--chunked-prefill-size') + 1] == '4096'

    def test_never_emits_removed_flags(self):
        for w in (8 * GiB, 16 * GiB, 19 * GiB):
            p = plan_launch(_hf(w), _hw(gpus=[_gpu()]), engine='sglang')
            joined = ' '.join(p['argv'])
            assert '--torchao-config' not in joined
            assert '--enable-torch-compile' not in joined


class TestLlamaCppTiers:
    def test_gpu_full_offload(self):
        p = plan_launch(_gguf(5 * GiB), _hw(gpus=[_gpu()]))
        argv = p['argv']
        assert argv[0] == 'llama-server'
        assert argv[argv.index('-ngl') + 1] == '999'
        assert p['env']['CUDA_VISIBLE_DEVICES'] == '0'

    def test_gpu_tight_uses_q8_kv(self):
        p = plan_launch(_gguf(16 * GiB), _hw(gpus=[_gpu(24 * GiB)]))
        assert p['tier'] in ('tight', 'extreme')
        assert '--cache-type-k' in p['argv']

    def test_cpu_threads_and_ctx(self):
        p = plan_launch(_gguf(5 * GiB), _hw(cpu=12))
        argv = p['argv']
        assert argv[argv.index('-ngl') + 1] == '0'
        assert argv[argv.index('--threads') + 1] == '11'  # cpu_count - 1
        assert argv[argv.index('-c') + 1] == '4096'
        assert any('CPU' in n for n in p['notes'])

    def test_cpu_ram_fit_refusal(self):
        p = plan_launch(_gguf(40 * GiB), _hw(ram_available=16 * GiB))
        assert not p['ok']
        assert '内存' in p['error']


class TestOllamaPlan:
    def test_env_and_setup_steps(self):
        p = plan_launch(_gguf(5 * GiB), _hw(gpus=[_gpu()]), engine='ollama')
        assert p['ok'] and p['engine'] == 'ollama'
        assert p['argv'] == ['ollama', 'serve']
        env = p['env']
        assert env['OLLAMA_HOST'] == '127.0.0.1:18100'
        assert env['OLLAMA_KV_CACHE_TYPE'] == 'q8_0'
        assert env['OLLAMA_FLASH_ATTENTION'] == '1'
        steps = p['setup_steps']
        assert steps[0]['kind'] == 'write_file'
        assert steps[0]['content'].startswith('FROM /models/')
        assert steps[1]['argv'][1] == 'create'

    def test_served_name_lowercased(self):
        p = plan_launch(_gguf(5 * GiB), _hw(gpus=[_gpu()]), engine='ollama')
        assert p['served_name'] == p['served_name'].lower()


class TestPlanEnvelope:
    def test_common_fields(self):
        p = plan_launch(_hf(8 * GiB), _hw(gpus=[_gpu()]), port=18123)
        assert p['base_url'] == 'http://127.0.0.1:18123/v1'
        assert p['port'] == 18123
        assert p['install_kind'] == ENGINE_SPECS['vllm']['install_kind']
        assert p['disk_need_bytes'] > 0
        assert p['model_path'] == '/models/Qwen3-8B'

    def test_loopback_only(self):
        for fmt_insp in (_hf(8 * GiB), _gguf(5 * GiB)):
            for eng in (None, 'sglang' if fmt_insp['format'] == 'hf' else 'ollama'):
                p = plan_launch(fmt_insp, _hw(gpus=[_gpu()]), engine=eng)
                joined = ' '.join(p['argv'])
                assert '0.0.0.0' not in joined
                assert '127.0.0.1' in joined or 'OLLAMA_HOST' in p['env']

    def test_removed_vllm_flags_never_generated(self):
        banned = ('--enable-chunked-prefill', 'calculate-kv-scales',
                  'override-attention-dtype', 'bitsandbytes')
        for w in (8 * GiB, 16 * GiB, 19 * GiB):
            p = plan_launch(_hf(w), _hw(gpus=[_gpu()]))
            joined = ' '.join(p['argv'])
            for b in banned:
                assert b not in joined
