"""lib/local_serve/_plan.py — Deterministic engine selection and launch policy.

Given a model inspection (``_probe.inspect_model_path``) and a hardware
snapshot (``_probe.probe_hardware``) this module decides WHICH engine to run
and WITH WHAT ARGV/env. The policy is a hand-maintained table — the whole
point is that parameter choice is deterministic and testable, not improvised
by the chat model. The agent narrates this plan to the user; it does not
invent flags.

Engine selection
----------------
* HF directory + NVIDIA GPU  → vLLM (primary) / SGLang (alternative)
* HF directory + no GPU      → infeasible with guidance (vLLM-CPU is too
  fragile to auto-install; suggest a GGUF instead)
* .gguf file + NVIDIA GPU    → llama.cpp (primary) / Ollama (alternative)
* .gguf file + no GPU        → llama.cpp CPU / Ollama

Resource tiers (NVIDIA paths)
-----------------------------
Fit is estimated as ``weight_bytes * 1.2`` (weights + activation workspace)
against the GPU's FREE VRAM:

* ``comfortable`` (≤70% of total) — engine defaults, no context cap
* ``tight``       (≤92%)        — context capped at 16k, small batch
* ``extreme``     (≤100%)       — context 4k, batch 1–2, eager mode
* above 100%                     — infeasible: weights alone do not fit;
  the plan says to pick an AWQ/GPTQ/FP8 or GGUF variant instead

Every feasible plan carries an ``degrade`` ladder: ordered steps the process
supervisor applies one at a time when the server log shows an OOM signature
(see ``_process.OOM_PATTERNS``). Ladders only ever REDUCE memory pressure
(shorter context → no CUDA graphs → quantized KV cache); they never relax a
safety property like the loopback bind.

Flag provenance (verified 2026-08 against official docs, see project journal):
vLLM 0.28  — V1-only, ``--max-model-len auto`` adapts to VRAM, removed flags
             (``--enable-chunked-prefill``, ``calculate_kv_scales``,
             ``override_attention_dtype``, ``VLLM_USE_V1``, in-tree
             ``bitsandbytes``) must never be generated.
SGLang 0.5 — ``--mem-fraction-static``/``--chunked-prefill-size`` auto by
             default; ``--torchao-config`` removed; ``--enable-torch-compile``
             is unmaintained and never emitted.
Ollama     — flash-attn auto; ``OLLAMA_KV_CACHE_TYPE=q8_0`` is the official
             half-KV-VRAM lever; local GGUF needs a Modelfile + ``ollama
             create`` (setup_steps below).
llama.cpp  — ``llama-server -m <gguf>``; ``--webui-config`` is renamed
             ``--ui-config``; ``-fa on`` + ``--cache-type-* q8_0`` for tight
             VRAM.
"""

from __future__ import annotations

__all__ = ['ENGINE_SPECS', 'MANAGED_PORT_BASE', 'MANAGED_PORT_END', 'plan_launch']

GiB = 1 << 30

# Managed servers bind this loopback band only. It deliberately avoids the
# well-known engine ports (8000/30000/11434) so a managed instance can never
# shadow an operator-run engine or trip autodiscovery's coverage check.
MANAGED_PORT_BASE = 18100
MANAGED_PORT_END = 18199

# Install metadata consumed by _env.py. ``disk_need_bytes`` is the precheck
# figure INCLUDING interpreter/site-packages headroom, not the wheel size.
ENGINE_SPECS = {
    'vllm': {
        'display': 'vLLM',
        'install_kind': 'uv_venv',
        'packages': ['vllm==0.28.*'],
        'uv_args': ['--torch-backend', 'auto'],
        'disk_need_bytes': 9 * GiB,
        'needs_nvidia': True,
    },
    'sglang': {
        'display': 'SGLang',
        'install_kind': 'uv_venv',
        'packages': ['sglang[all]==0.5.*'],
        'uv_args': [],
        'disk_need_bytes': 9 * GiB,
        'needs_nvidia': True,
    },
    'ollama': {
        'display': 'Ollama',
        'install_kind': 'ollama_installer',
        'packages': [],
        'uv_args': [],
        'disk_need_bytes': 2 * GiB,
        'needs_nvidia': False,
    },
    'llamacpp': {
        'display': 'llama.cpp',
        'install_kind': 'github_release',
        'packages': [],
        'uv_args': [],
        'disk_need_bytes': 1 * GiB,
        'needs_nvidia': False,
    },
}

_ENGINE_ORDER = {
    ('hf', True): ['vllm', 'sglang'],
    ('hf', False): [],
    ('gguf', True): ['llamacpp', 'ollama'],
    ('gguf', False): ['llamacpp', 'ollama'],
}

_WEIGHT_OVERHEAD = 1.2   # weights + activation/workspace, vs raw weight bytes


def _gib(n) -> float:
    return (n or 0) / float(GiB)


def _sanitize_name(raw: str) -> str:
    out = []
    for ch in (raw or 'model').strip():
        out.append(ch if (ch.isalnum() or ch in '.-_') else '-')
    name = ''.join(out).strip('-.') or 'model'
    return name[:64]


def _pick_gpu(hardware: dict) -> dict | None:
    gpus = hardware.get('gpus') or []
    if not gpus:
        return None
    # The GPU with the most free VRAM is the least likely to OOM.
    return max(gpus, key=lambda g: g.get('vram_free_bytes') or 0)


def _tier(weight_bytes: int, vram_total: int) -> str | None:
    need = weight_bytes * _WEIGHT_OVERHEAD
    if need <= vram_total * 0.70:
        return 'comfortable'
    if need <= vram_total * 0.92:
        return 'tight'
    if need <= vram_total * 1.00:
        return 'extreme'
    return None


def _ctx_cap(model_ctx, cap: int) -> int:
    if isinstance(model_ctx, int) and model_ctx > 0:
        return max(512, min(model_ctx, cap))
    return cap


# ─────────────────────────── per-engine builders ───────────────────────────

def _plan_vllm(spec: dict, insp: dict, gpu: dict, port: int) -> dict:
    w = insp.get('weight_bytes') or 0
    total = gpu['vram_total_bytes']
    free = gpu.get('vram_free_bytes') or total
    tier = _tier(w, min(total, free)) if w else 'comfortable'
    if tier is None:
        return {'ok': False,
                'error': '权重约 %.1f GiB，超过 GPU %.1f GiB 显存的装载上限。'
                         '请换 AWQ/GPTQ/FP8 量化版或 GGUF 模型。'
                         % (_gib(w), _gib(total))}
    name = _sanitize_name(insp.get('served_name') or 'model')
    argv = ['vllm', 'serve', insp['path'],
            '--served-model-name', name,
            '--host', '127.0.0.1', '--port', str(port)]
    env = {'CUDA_VISIBLE_DEVICES': str(gpu['index'])}
    notes = []
    degrade = []
    if tier == 'comfortable':
        argv += ['--gpu-memory-utilization', '0.90', '--max-model-len', 'auto']
    elif tier == 'tight':
        argv += ['--gpu-memory-utilization', '0.90',
                 '--max-model-len', str(_ctx_cap(insp.get('max_context'), 16384)),
                 '--max-num-seqs', '8']
        notes.append('显存偏紧：上下文上限压到 16k，并发压到 8')
        degrade.append({'note': 'OOM 降级：上下文降到 4k，并发降到 2',
                        'replace': {'--max-model-len': '4096', '--max-num-seqs': '2'}})
        degrade.append({'note': 'OOM 降级：关闭 CUDA graph（--enforce-eager）',
                        'append': ['--enforce-eager']})
    else:  # extreme
        argv += ['--gpu-memory-utilization', '0.92',
                 '--max-model-len', '4096', '--max-num-seqs', '2',
                 '--enforce-eager']
        notes.append('显存极限：上下文 4k、并发 2、关闭 CUDA graph；'
                     '建议改用量化版模型以获得可用速度')
        degrade.append({'note': 'OOM 降级：KV cache 量化 fp8（需 sm89+）',
                        'append': ['--kv-cache-dtype', 'fp8']})
    degrade.append({'note': '仍 OOM：权重超出该卡能力，请换量化版或 GGUF',
                    'terminal': True})
    return {'ok': True, 'tier': tier, 'argv': argv, 'env': env,
            'served_name': name, 'notes': notes, 'degrade': degrade,
            'setup_steps': []}


def _plan_sglang(spec: dict, insp: dict, gpu: dict, port: int) -> dict:
    w = insp.get('weight_bytes') or 0
    total = gpu['vram_total_bytes']
    free = gpu.get('vram_free_bytes') or total
    tier = _tier(w, min(total, free)) if w else 'comfortable'
    if tier is None:
        return {'ok': False,
                'error': '权重约 %.1f GiB，超过 GPU %.1f GiB 显存的装载上限。'
                         '请换 AWQ/GPTQ/FP8 量化版或 GGUF 模型。'
                         % (_gib(w), _gib(total))}
    name = _sanitize_name(insp.get('served_name') or 'model')
    argv = ['python', '-m', 'sglang.launch_server',
            '--model-path', insp['path'],
            '--served-model-name', name,
            '--host', '127.0.0.1', '--port', str(port)]
    env = {'CUDA_VISIBLE_DEVICES': str(gpu['index'])}
    notes = []
    degrade = []
    if tier == 'comfortable':
        argv += ['--mem-fraction-static', '0.85']
    elif tier == 'tight':
        argv += ['--mem-fraction-static', '0.80',
                 '--chunked-prefill-size', '4096',
                 '--max-running-requests', '8',
                 '--max-total-tokens', str(_ctx_cap(insp.get('max_context'), 16384) * 8)]
        notes.append('显存偏紧：mem-fraction 0.80、chunked prefill 4096、并发 8')
        degrade.append({'note': 'OOM 降级：关闭 CUDA graph',
                        'append': ['--disable-cuda-graph']})
        degrade.append({'note': 'OOM 降级：并发降到 2',
                        'replace': {'--max-running-requests': '2'}})
    else:
        argv += ['--mem-fraction-static', '0.80',
                 '--chunked-prefill-size', '2048',
                 '--max-running-requests', '2',
                 '--max-total-tokens', '32768',
                 '--disable-cuda-graph']
        notes.append('显存极限：prefill 2048、并发 2、关闭 CUDA graph；'
                     '建议改用量化版模型')
        degrade.append({'note': 'OOM 降级：KV cache 量化 fp8_e4m3',
                        'append': ['--kv-cache-dtype', 'fp8_e4m3']})
    degrade.append({'note': '仍 OOM：权重超出该卡能力，请换量化版或 GGUF',
                    'terminal': True})
    return {'ok': True, 'tier': tier, 'argv': argv, 'env': env,
            'served_name': name, 'notes': notes, 'degrade': degrade,
            'setup_steps': []}


def _plan_llamacpp(spec: dict, insp: dict, gpu: dict | None,
                   hardware: dict, port: int) -> dict:
    w = insp.get('weight_bytes') or 0
    name = _sanitize_name(insp.get('served_name') or 'model')
    notes = []
    degrade = []
    if gpu:
        total = gpu['vram_total_bytes']
        free = gpu.get('vram_free_bytes') or total
        tier = _tier(w, min(total, free)) if w else 'comfortable'
        ctx = _ctx_cap(insp.get('max_context'),
                       8192 if tier in ('comfortable', 'tight') else 4096)
        argv = ['llama-server', '-m', insp['path'],
                '--host', '127.0.0.1', '--port', str(port),
                '--alias', name,
                '-ngl', '999', '-fa', 'on', '-c', str(ctx)]
        env = {'CUDA_VISIBLE_DEVICES': str(gpu['index'])}
        if tier in ('tight', 'extreme'):
            argv += ['--cache-type-k', 'q8_0', '--cache-type-v', 'q8_0']
            notes.append('显存偏紧：KV cache 量化 q8_0，上下文 %d' % ctx)
        if tier == 'extreme':
            notes.append('显存极限：若启动失败会自动把部分层卸载到内存')
            degrade.append({'note': 'OOM 降级：上下文降到 2048',
                            'replace': {'-c': '2048'}})
            degrade.append({'note': 'OOM 降级：一半层卸载到内存（速度会明显下降）',
                            'replace': {'-ngl': '20'}})
        degrade.append({'note': '仍 OOM：请选更小的量化（如 Q4_K_M → IQ4_XS）',
                        'terminal': True})
    else:
        avail = hardware.get('ram_available_bytes')
        if w and avail and w * 1.1 > avail * 0.8:
            return {'ok': False,
                    'error': '模型文件 %.1f GiB，可用内存约 %.1f GiB，装载余量不足。'
                             '请选更小的量化版本。' % (_gib(w), _gib(avail))}
        tier = 'cpu'
        threads = max(1, (hardware.get('cpu_count') or 4) - 1)
        ctx = _ctx_cap(insp.get('max_context'), 4096)
        argv = ['llama-server', '-m', insp['path'],
                '--host', '127.0.0.1', '--port', str(port),
                '--alias', name,
                '-ngl', '0', '--threads', str(threads), '-c', str(ctx)]
        env = {}
        notes.append('纯 CPU 推理：线程 %d，上下文 %d；生成速度较慢属预期'
                     % (threads, ctx))
        degrade.append({'note': '内存不足降级：上下文降到 2048',
                        'replace': {'-c': '2048'}})
    return {'ok': True, 'tier': tier, 'argv': argv, 'env': env,
            'served_name': name, 'notes': notes, 'degrade': degrade,
            'setup_steps': []}


def _plan_ollama(spec: dict, insp: dict, gpu: dict | None,
                 hardware: dict, port: int) -> dict:
    w = insp.get('weight_bytes') or 0
    name = _sanitize_name(insp.get('served_name') or 'model').lower()
    ctx = _ctx_cap(insp.get('max_context'), 8192 if gpu else 4096)
    env = {
        # Managed instance lives on its own loopback port so it never fights
        # an operator-run ollama on 11434.
        'OLLAMA_HOST': '127.0.0.1:%d' % port,
        'OLLAMA_KV_CACHE_TYPE': 'q8_0',
        'OLLAMA_FLASH_ATTENTION': '1',
        'OLLAMA_NUM_PARALLEL': '1',
        'OLLAMA_MAX_LOADED_MODELS': '1',
        'OLLAMA_CONTEXT_LENGTH': str(ctx),
    }
    if gpu:
        env['CUDA_VISIBLE_DEVICES'] = str(gpu['index'])
    notes = ['KV cache q8_0 + flash-attn：官方推荐的小显存组合']

    if gpu and w and w * _WEIGHT_OVERHEAD > (gpu.get('vram_free_bytes') or 0):
        notes.append('模型大于剩余显存：Ollama 会自动把部分层卸载到内存，'
                     '生成速度会明显下降')
    degrade = [{'note': 'OOM 降级：上下文降到 4096',
                'env_replace': {'OLLAMA_CONTEXT_LENGTH': '4096'}},
               {'note': '仍 OOM：请选更小的量化版本', 'terminal': True}]
    modelfile = 'FROM %s\n' % insp['path']
    return {
        'ok': True, 'tier': 'comfortable' if gpu else 'cpu',
        'argv': ['ollama', 'serve'], 'env': env,
        'served_name': name, 'notes': notes, 'degrade': degrade,
        # Ollama cannot serve a bare .gguf path: import it under a managed
        # name first, then the OpenAI shim exposes it at /v1.
        'setup_steps': [
            {'kind': 'write_file', 'name': 'modelfile',
             'content': modelfile},
            {'kind': 'run', 'argv': ['ollama', 'create', name, '-f',
                                     '{modelfile}'], 'timeout': 600},
        ],
    }


# ─────────────────────────── entry point ───────────────────────────

def plan_launch(inspection: dict, hardware: dict, *,
                engine: str | None = None,
                port: int = MANAGED_PORT_BASE) -> dict:
    """Build one deterministic launch plan; never raises.

    ``engine`` may pin a specific engine (user asked for it); otherwise the
    first entry of the format/hardware order wins and the rest are reported
    as ``alternatives``. The returned dict is JSON-serialisable and shown to
    the user by the agent before any install/launch approval gate.
    """
    if inspection.get('format') not in ('hf', 'gguf'):
        return {'ok': False,
                'error': inspection.get('error') or '无法识别的模型格式'}
    gpu = _pick_gpu(hardware)
    fmt = inspection['format']
    order = list(_ENGINE_ORDER.get((fmt, bool(gpu)), ()))
    if not order:
        return {'ok': False,
                'error': '未检测到 NVIDIA GPU，HF 格式模型无法在本机直接推理。'
                         '建议下载该模型的 GGUF 量化版（Q4_K_M 起），'
                         '或在有 GPU 的机器上部署后通过 API 接入。'}
    if engine:
        if engine not in ENGINE_SPECS:
            return {'ok': False, 'error': '未知引擎: %s' % engine}
        if engine not in order:
            compat = {'hf': 'vLLM / SGLang', 'gguf': 'llama.cpp / Ollama'}[fmt]
            return {'ok': False,
                    'error': '引擎 %s 不支持 %s 格式模型；该格式可用: %s'
                             % (ENGINE_SPECS[engine]['display'], fmt, compat)}
        chosen = engine
    else:
        chosen = order[0]
    spec = ENGINE_SPECS[chosen]
    if spec['needs_nvidia'] and not gpu:
        return {'ok': False, 'error': '%s 需要 NVIDIA GPU' % spec['display']}

    if chosen == 'vllm':
        plan = _plan_vllm(spec, inspection, gpu, port)
    elif chosen == 'sglang':
        plan = _plan_sglang(spec, inspection, gpu, port)
    elif chosen == 'llamacpp':
        plan = _plan_llamacpp(spec, inspection, gpu, hardware, port)
    else:
        plan = _plan_ollama(spec, inspection, gpu, hardware, port)
    if not plan.get('ok'):
        return plan

    plan.update({
        'engine': chosen,
        'engine_display': spec['display'],
        'install_kind': spec['install_kind'],
        'disk_need_bytes': spec['disk_need_bytes'],
        'port': port,
        'base_url': 'http://127.0.0.1:%d/v1' % port,
        'alternatives': [e for e in order if e != chosen],
        'format': fmt,
        'model_path': inspection['path'],
    })
    if gpu and gpu.get('vram_free_bytes', 0) < gpu.get('vram_total_bytes', 0) * 0.9:
        plan['notes'].insert(
            0, 'GPU%d（%s）已被占用 %.1f/%.1f GiB，按剩余显存规划'
            % (gpu['index'], gpu.get('name', '?'),
               _gib(gpu['vram_total_bytes'] - gpu.get('vram_free_bytes', 0)),
               _gib(gpu['vram_total_bytes'])))
    return plan
