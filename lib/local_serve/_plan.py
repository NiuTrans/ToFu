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

Resource estimates and physical attempts
---------------------------------------
The existing weights/workspace estimate ranks recommendations and describes
startup pressure. It never denies a compatible requested deployment. Engines
own physical memory allocation: vLLM uses automatic context sizing; SGLang
owns its memory fraction, token pool and running-request defaults. Estimated
GPU overflow can use CPU weight offloading sized from observed RAM headroom.
No Tofu-generated request-count cap is added. Genuine OOM enters an ordered
recovery ladder (CUDA graph removal and quantized KV cache), with actual
failure evidence preserved by the process supervisor.

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

import math

__all__ = ['ENGINE_SPECS', 'MANAGED_PORT_BASE', 'plan_launch']

GiB = 1 << 30

# Default port for standalone plan previews. Managed deployments ask the OS
# for a free loopback endpoint; this is a hint, not a managed range boundary.
MANAGED_PORT_BASE = 18100

# Install metadata consumed by _env.py. ``disk_need_bytes`` is a diagnostic estimate
# INCLUDING interpreter/site-packages headroom, not the wheel size.
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
        return min(model_ctx, cap)
    return cap


# ─────────────────────────── per-engine builders ───────────────────────────

def _weight_offload_gib(weight_bytes: int, free_vram: int, hardware: dict) -> int:
    """Estimate CPU weight placement from current GPU and RAM headroom.

    This is an allocation hint, not an admission rule. Missing RAM evidence
    yields no offload hint; the engine still gets a physical launch attempt.
    Both engines express cpu-offload-gb in GiB. Do not request more observed
    host headroom than exists, and do not reserve it inside Tofu.
    """
    available_ram = hardware.get('ram_available_bytes')
    if type(available_ram) is not int or available_ram <= 0:
        return 0
    needed = max(0, math.ceil((weight_bytes * _WEIGHT_OVERHEAD
                              - free_vram * 0.90) / GiB))
    return min(needed, available_ram // GiB)


def _plan_hf_engine(insp: dict, gpu: dict, hardware: dict,
                    port: int, engine: str) -> dict:
    """Compatible HF launch; estimates tune placement, never deny execution."""
    weight = insp.get('weight_bytes') or 0
    total = gpu['vram_total_bytes']
    free = gpu.get('vram_free_bytes')
    free = total if free is None else free
    estimated_tier = _tier(weight, min(total, free)) if weight else 'comfortable'
    tier = estimated_tier or 'extreme'
    name = _sanitize_name(insp.get('served_name') or 'model')
    if engine == 'vllm':
        argv = ['vllm', 'serve', insp['path'], '--served-model-name', name,
                '--host', '127.0.0.1', '--port', str(port),
                '--gpu-memory-utilization', '0.90', '--max-model-len', 'auto']
        degrade = [
            {'note': 'OOM 降级：关闭 CUDA graph', 'append': ['--enforce-eager']},
            {'note': 'OOM 降级：量化 KV cache', 'append': ['--kv-cache-dtype', 'fp8']},
        ]
    else:
        argv = ['python', '-m', 'sglang.launch_server', '--model-path', insp['path'],
                '--served-model-name', name, '--host', '127.0.0.1', '--port', str(port)]
        degrade = [
            {'note': 'OOM 降级：关闭 CUDA graph', 'append': ['--disable-cuda-graph']},
            {'note': 'OOM 降级：量化 KV cache', 'append': ['--kv-cache-dtype', 'fp8_e4m3']},
        ]
    notes = []
    if estimated_tier is None:
        notes.append('估算显存偏紧：尝试实际加载，由引擎报告内存分配结果')
        offload = _weight_offload_gib(weight, free, hardware)
        if offload:
            argv += ['--cpu-offload-gb', str(offload)]
            notes.append('按可用 RAM 估算，将最多 %d GiB 权重卸载到 CPU；速度取决于互连' % offload)
    if free < total * 0.9:
        notes.append('该 GPU 已被占用一部分显存：当前可用 %.1f / %.1f GiB'
                     % (_gib(free), _gib(total)))
    degrade.append({'note': '实际加载仍 OOM：请更换量化、卸载或分片配置', 'terminal': True})
    return {'ok': True, 'tier': tier, 'argv': argv,
            'env': {'CUDA_VISIBLE_DEVICES': str(gpu['index'])},
            'served_name': name, 'notes': notes, 'degrade': degrade, 'setup_steps': []}


def _plan_llamacpp(spec: dict, insp: dict, gpu: dict | None,
                   hardware: dict, port: int) -> dict:
    w = insp.get('weight_bytes') or 0
    name = _sanitize_name(insp.get('served_name') or 'model')
    notes = []
    degrade = []
    if gpu:
        total = gpu['vram_total_bytes']
        free = gpu.get('vram_free_bytes')
        free = total if free is None else free
        tier = (_tier(w, min(total, free)) or 'extreme') if w else 'comfortable'
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
        tier = 'cpu'
        threads = max(1, hardware.get('cpu_count') or 1)
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


def _ollama_parallelism(insp: dict, hardware: dict, gpu: dict | None,
                        context: int) -> dict:
    """Size physical KV allocation from known dense-attention metadata.

    This is a startup sizing estimate, never task admission. Unknown layouts
    retain lean startup instead of inventing cache dimensions. Ollama owns
    actual model placement, execution and allocation failures.
    """
    explicit = (hardware.get('ollama_env') or {}).get('OLLAMA_NUM_PARALLEL')
    if isinstance(explicit, str) and explicit:
        try:
            requested = int(explicit)
        except ValueError:
            requested = 1
        # Preserve the operator's literal setting. Invalid uint64 values are
        # handled by Ollama itself; they do not create local task refusal.
        if not explicit.isascii() or not explicit.isdecimal() or requested >= 1 << 64:
            requested = 1
        return {'parallel_requests': max(1, requested), 'source': 'operator_override',
                'env_value': explicit}
    evidence = {'parallel_requests': 1, 'source': 'unknown_attention_layout'}
    if insp.get('architecture') not in ('llama', 'qwen2', 'qwen3'):
        return evidence
    dims = insp.get('attention_dimensions') or {}
    layers = dims.get('block_count')
    heads = dims.get('attention.head_count')
    kv_heads = dims.get('attention.head_count_kv', heads)
    if kv_heads is None:
        kv_heads = heads
    embedding = dims.get('embedding_length')
    values = (layers, heads, kv_heads, embedding)
    if any(type(value) is not int or value <= 0 for value in values):
        return evidence
    if kv_heads > heads or embedding % heads:
        return evidence
    key_width = dims.get('attention.key_length')
    value_width = dims.get('attention.value_length')
    key_width = embedding // heads if key_width is None else key_width
    value_width = embedding // heads if value_width is None else value_width
    if any(type(value) is not int or value <= 0 or value % 32
           for value in (key_width, value_width)):
        return evidence
    # GGML q8_0 uses 34 bytes per block of 32 elements (including scale).
    kv_bytes_per_request = (layers * kv_heads * context
                            * ((key_width + value_width) // 32) * 34)
    weight_bytes = insp.get('weight_bytes')
    free_bytes = (gpu.get('vram_free_bytes') if gpu else
                  hardware.get('ram_available_bytes'))
    if (type(weight_bytes) is not int or weight_bytes <= 0
            or type(free_bytes) is not int or free_bytes <= 0):
        return evidence
    # Reuse the launch planner's weights/workspace estimate, without a fit
    # refusal. A small or unknown budget still starts one physical runner.
    workspace_bytes = math.ceil(weight_bytes * _WEIGHT_OVERHEAD)
    parallel = max(1, (free_bytes - workspace_bytes) // kv_bytes_per_request)
    return {'parallel_requests': parallel, 'source': 'dense_q8_0_resource_estimate',
            'kv_bytes_per_request': kv_bytes_per_request,
            'available_bytes': free_bytes, 'weight_workspace_bytes': workspace_bytes}


def _plan_ollama(spec: dict, insp: dict, gpu: dict | None,
                 hardware: dict, port: int) -> dict:
    w = insp.get('weight_bytes') or 0
    name = _sanitize_name(insp.get('served_name') or 'model').lower()
    ctx = _ctx_cap(insp.get('max_context'), 8192 if gpu else 4096)
    parallelism = _ollama_parallelism(insp, hardware, gpu, ctx)
    env = {
        # Managed instance lives on its own loopback port so it never fights
        # an operator-run ollama on 11434.
        'OLLAMA_HOST': '127.0.0.1:%d' % port,
        'OLLAMA_KV_CACHE_TYPE': 'q8_0',
        'OLLAMA_FLASH_ATTENTION': '1',
        'OLLAMA_NUM_PARALLEL': parallelism.get('env_value', str(parallelism['parallel_requests'])),
        'OLLAMA_CONTEXT_LENGTH': str(ctx),
    }
    if gpu:
        env['CUDA_VISIBLE_DEVICES'] = str(gpu['index'])
    notes = ['KV cache q8_0 + flash-attn：官方推荐的小显存组合']

    if gpu and w and w * _WEIGHT_OVERHEAD > (gpu.get('vram_free_bytes') or 0):
        notes.append('模型大于剩余显存：Ollama 会自动把部分层卸载到内存，'
                     '生成速度会明显下降')
    degrade = []
    reduced_parallel = parallelism['parallel_requests']
    while reduced_parallel > 1:
        reduced_parallel = max(1, reduced_parallel // 2)
        degrade.append({'note': 'OOM 降级：并行降到 %s' % reduced_parallel,
                        'env_replace': {'OLLAMA_NUM_PARALLEL': str(reduced_parallel)}})
    degrade += [{'note': 'OOM 降级：上下文降到 %s' % min(ctx, 4096),
                'env_replace': {'OLLAMA_CONTEXT_LENGTH': str(min(ctx, 4096))}},
               {'note': '仍 OOM：请选更小的量化版本', 'terminal': True}]
    modelfile = 'FROM %s\n' % insp['path']
    return {
        'ok': True, 'tier': 'comfortable' if gpu else 'cpu',
        'parallelism': parallelism,
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
        plan = _plan_hf_engine(inspection, gpu, hardware, port, 'vllm')
    elif chosen == 'sglang':
        plan = _plan_hf_engine(inspection, gpu, hardware, port, 'sglang')
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
