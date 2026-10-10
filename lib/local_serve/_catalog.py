"""lib/local_serve/_catalog.py — Curated open-model catalog and hardware fit.

The catalog is a hand-maintained shortlist of open-weight models that are
worth deploying on a personal machine, across size classes (1.7B → 32B) and
use-case tags (chat / coding / reasoning / vision / multilingual). Each
entry carries two variants: the HF BF16 weights (vLLM/SGLang on NVIDIA) and
the GGUF Q4_K_M quant (llama.cpp on GPU or CPU). Weight bytes are sizing
ESTIMATES derived from parameter count; once the user actually downloads
the model, ``_probe.inspect_model_path`` re-derives the exact number from
the real files and ``_plan.plan_launch`` recomputes the authoritative
launch plan. The catalog only decides what is worth RECOMMENDING.

``evaluate_hardware`` maps one hardware snapshot (``_probe.probe_hardware``)
onto the catalog: every model gets its best feasible variant
(engine + tier), infeasible ones are dropped, and the survivors are ranked
comfortable-first then bigger-params-first. ``cloud_alternative`` answers
the follow-up question — when the machine can only run something tiny (or
nothing), it recommends the cheapest capable API model instead of letting
the user fight hopeless hardware.
"""

from __future__ import annotations

from lib.pricing import pricing_for_model

from ._plan import _pick_gpu, _tier

__all__ = ['LOCAL_MODEL_CATALOG', 'PURPOSE_TAGS', 'evaluate_hardware',
           'cloud_alternative']

GiB = 1 << 30

#: Purpose → catalog tag. The chat tool and the settings page share this
#: vocabulary; unknown purposes fall back to the unfiltered catalog.
PURPOSE_TAGS = {
    'chat': 'chat',
    'coding': 'coding',
    'reasoning': 'reasoning',
    'vision': 'vision',
    'multilingual': 'multilingual',
}

_TIER_RANK = {'comfortable': 0, 'tight': 1, 'extreme': 2, 'cpu': 3}

#: Below this size a local model is a poor daily driver — the cloud
#: alternative becomes worth surfacing even when something technically fits.
_CAPABLE_PARAMS_B = 7.0

#: The cheap API escape hatch. deepseek-flash (V4.1 Flash, 2026-09-10) is
#: the cheapest thinking-capable model in MODEL_PRICING with a provider
#: template, so the settings page can deep-link the user straight into the
#: DeepSeek wizard.
_CLOUD_MODEL_ID = 'deepseek-flash'
_CLOUD_TEMPLATE_KEY = 'deepseek'


def _bf16(params_b: float) -> int:
    return int(params_b * 2 * GiB)


def _q4km(params_b: float) -> int:
    # Q4_K_M lands at roughly 4.8–5.6 bits per parameter plus tensor
    # overhead; 0.62 bytes/param is the observed midpoint.
    return int(params_b * 0.62 * GiB)


def _entry(entry_id: str, display: str, params_b: float, tags: list,
           context: int, hf_repo: str, gguf_repo: str, note: str,
           modelscope: str | None = None) -> dict:
    return {
        'id': entry_id,
        'display': display,
        'params_b': params_b,
        'tags': tags,
        'context': context,
        'note': note,
        'modelscope': modelscope,
        'variants': [
            {'kind': 'hf', 'label': 'BF16', 'weight_bytes': _bf16(params_b),
             'repo': hf_repo},
            {'kind': 'gguf', 'label': 'Q4_K_M', 'weight_bytes': _q4km(params_b),
             'repo': gguf_repo},
        ],
    }


LOCAL_MODEL_CATALOG = [
    _entry('qwen3-1.7b', 'Qwen3-1.7B', 1.7, ['chat', 'multilingual'], 32768,
           'Qwen/Qwen3-1.7B', 'Qwen/Qwen3-1.7B-GGUF',
           '最小的 Qwen3：树莓派级硬件也能跑，适合体验流程',
           modelscope='Qwen/Qwen3-1.7B'),
    _entry('qwen3-4b-instruct', 'Qwen3-4B-Instruct-2507', 4.0,
           ['chat', 'multilingual'], 262144,
           'Qwen/Qwen3-4B-Instruct-2507', 'Qwen/Qwen3-4B-Instruct-2507-GGUF',
           '4B 档位口碑最好的通用指令模型，256k 上下文',
           modelscope='Qwen/Qwen3-4B-Instruct-2507'),
    _entry('qwen3-4b-thinking', 'Qwen3-4B-Thinking-2507', 4.0,
           ['reasoning'], 262144,
           'Qwen/Qwen3-4B-Thinking-2507', 'Qwen/Qwen3-4B-Thinking-2507-GGUF',
           '小参数推理模型，数学/逻辑任务明显强于同尺寸指令版',
           modelscope='Qwen/Qwen3-4B-Thinking-2507'),
    _entry('gemma-3-4b-it', 'Gemma 3 4B IT', 4.0,
           ['chat', 'vision', 'multilingual'], 131072,
           'google/gemma-3-4b-it', 'google/gemma-3-4b-it-qat-q4_0-gguf',
           '多模态（图文）小模型；HF 需先接受 Gemma 许可'),
    _entry('qwen3-8b', 'Qwen3-8B', 8.0, ['chat', 'multilingual'], 32768,
           'Qwen/Qwen3-8B', 'Qwen/Qwen3-8B-GGUF',
           '8B 通用主力，消费级显卡的甜点位',
           modelscope='Qwen/Qwen3-8B'),
    _entry('llama-3.1-8b', 'Llama 3.1 8B Instruct', 8.0, ['chat'], 131072,
           'meta-llama/Llama-3.1-8B-Instruct',
           'unsloth/Meta-Llama-3.1-8B-Instruct-GGUF',
           '英文生态最成熟；HF 需先接受 Meta 许可'),
    _entry('deepseek-r1-distill-7b', 'DeepSeek-R1-Distill-Qwen-7B', 7.0,
           ['reasoning'], 131072,
           'deepseek-ai/DeepSeek-R1-Distill-Qwen-7B',
           'deepseek-ai/DeepSeek-R1-Distill-Qwen-7B-GGUF',
           'R1 蒸馏推理模型，7B 即可体验思维链',
           modelscope='deepseek-ai/DeepSeek-R1-Distill-Qwen-7B'),
    _entry('qwen3-14b', 'Qwen3-14B', 14.0, ['chat', 'multilingual'], 32768,
           'Qwen/Qwen3-14B', 'Qwen/Qwen3-14B-GGUF',
           '14B 通用模型，12G 显存可用 Q4 量化流畅运行',
           modelscope='Qwen/Qwen3-14B'),
    _entry('deepseek-r1-distill-14b', 'DeepSeek-R1-Distill-Qwen-14B', 14.0,
           ['reasoning'], 131072,
           'deepseek-ai/DeepSeek-R1-Distill-Qwen-14B',
           'deepseek-ai/DeepSeek-R1-Distill-Qwen-14B-GGUF',
           '14B 推理蒸馏，中端显卡上的推理上限',
           modelscope='deepseek-ai/DeepSeek-R1-Distill-Qwen-14B'),
    _entry('gemma-3-12b-it', 'Gemma 3 12B IT', 12.0,
           ['chat', 'vision', 'multilingual'], 131072,
           'google/gemma-3-12b-it', 'google/gemma-3-12b-it-qat-q4_0-gguf',
           '多模态中杯；HF 需先接受 Gemma 许可'),
    _entry('qwen3-coder-30b-a3b', 'Qwen3-Coder-30B-A3B-Instruct', 30.0,
           ['coding'], 262144,
           'Qwen/Qwen3-Coder-30B-A3B-Instruct',
           'Qwen/Qwen3-Coder-30B-A3B-Instruct-GGUF',
           'MoE 编程模型：30B 总量仅 3B 激活，Q4 量化在 24G 显卡上飞快',
           modelscope='Qwen/Qwen3-Coder-30B-A3B-Instruct'),
    _entry('qwen3-30b-a3b-instruct', 'Qwen3-30B-A3B-Instruct-2507', 30.0,
           ['chat', 'coding', 'multilingual'], 262144,
           'Qwen/Qwen3-30B-A3B-Instruct-2507',
           'Qwen/Qwen3-30B-A3B-Instruct-2507-GGUF',
           'MoE 通用模型：3B 激活速度 + 30B 级知识，24G 显卡甜点',
           modelscope='Qwen/Qwen3-30B-A3B-Instruct-2507'),
    _entry('qwen3-32b', 'Qwen3-32B', 32.0, ['chat', 'multilingual'], 32768,
           'Qwen/Qwen3-32B', 'Qwen/Qwen3-32B-GGUF',
           '稠密 32B，Q4 量化需要约 24G 显存',
           modelscope='Qwen/Qwen3-32B'),
    _entry('deepseek-r1-distill-32b', 'DeepSeek-R1-Distill-Qwen-32B', 32.0,
           ['reasoning'], 131072,
           'deepseek-ai/DeepSeek-R1-Distill-Qwen-32B',
           'deepseek-ai/DeepSeek-R1-Distill-Qwen-32B-GGUF',
           '32B 推理蒸馏，单卡 24G 的推理天花板',
           modelscope='deepseek-ai/DeepSeek-R1-Distill-Qwen-32B'),
    _entry('gemma-3-27b-it', 'Gemma 3 27B IT', 27.0,
           ['chat', 'vision', 'multilingual'], 131072,
           'google/gemma-3-27b-it', 'google/gemma-3-27b-it-qat-q4_0-gguf',
           '多模态大杯；HF 需先接受 Gemma 许可'),
]


def _fit_options(entry: dict, gpu: dict | None,
                 ram_available: int | None) -> list:
    """Every (variant, engine, tier) the hardware can actually run."""
    options = []
    for variant in entry['variants']:
        weight = variant['weight_bytes']
        if gpu:
            budget = min(gpu.get('vram_total_bytes') or 0,
                         gpu.get('vram_free_bytes') or 0)
            tier = _tier(weight, budget) if weight else 'comfortable'
            if tier is not None:
                options.append({
                    'variant': variant['label'],
                    'kind': variant['kind'],
                    'engine': 'vllm' if variant['kind'] == 'hf' else 'llamacpp',
                    'tier': tier,
                    'accel': 'gpu',
                    'weight_bytes': weight,
                    'repo': variant['repo'],
                })
        if variant['kind'] == 'gguf' and ram_available:
            # Mirrors the llama.cpp CPU feasibility rule in _plan.
            if weight * 1.1 <= ram_available * 0.8:
                options.append({
                    'variant': variant['label'],
                    'kind': 'gguf',
                    'engine': 'llamacpp',
                    'tier': 'cpu',
                    'accel': 'cpu',
                    'weight_bytes': weight,
                    'repo': variant['repo'],
                })
    return options


def _best_option(options: list) -> dict:
    def _key(option):
        # Same tier: HF/vLLM beats GGUF on a GPU (faster kernels); a GPU tier
        # always beats CPU regardless of variant.
        prefer = 0 if option['kind'] == 'hf' else 1
        return (_TIER_RANK[option['tier']], prefer)
    return min(options, key=_key)


def _download_hint(entry: dict, option: dict) -> dict:
    hint = {'hf_repo': option['repo'],
            'hf_cmd': 'huggingface-cli download %s' % option['repo']}
    if entry.get('modelscope') and option['kind'] == 'hf':
        hint['modelscope_cmd'] = ('modelscope download --model %s'
                                  % entry['modelscope'])
    return hint


def evaluate_hardware(hardware: dict, *, purpose: str | None = None,
                      limit: int = 6) -> list:
    """Rank catalog models against one hardware snapshot.

    Returns at most ``limit`` candidates, best first; each carries the
    winning ``best`` option plus every other feasible variant under
    ``alternatives``. An empty list means nothing in the catalog runs.
    """
    gpu = _pick_gpu(hardware)
    ram_available = hardware.get('ram_available_bytes')
    tag = PURPOSE_TAGS.get(str(purpose or '').strip())
    entries = LOCAL_MODEL_CATALOG
    if tag:
        filtered = [e for e in entries if tag in e['tags']]
        # A purpose with no matches must not yield an empty recommendation —
        # the unfiltered catalog is the honest fallback.
        if filtered:
            entries = filtered
    candidates = []
    for entry in entries:
        options = _fit_options(entry, gpu, ram_available)
        if not options:
            continue
        best = _best_option(options)
        candidates.append({
            'id': entry['id'],
            'display': entry['display'],
            'params_b': entry['params_b'],
            'tags': entry['tags'],
            'context': entry['context'],
            'note': entry['note'],
            'tier': best['tier'],
            'best': {**best, 'download': _download_hint(entry, best)},
            'alternatives': [
                {**opt, 'download': _download_hint(entry, opt)}
                for opt in options if opt is not best],
        })
    candidates.sort(key=lambda c: (_TIER_RANK[c['tier']], -c['params_b']))
    return candidates[:max(1, limit)]


def cloud_alternative(hardware: dict, candidates: list) -> dict | None:
    """The cheap-API escape hatch, or None when local is genuinely good."""
    if candidates:
        top = candidates[0]
        if (_TIER_RANK[top['tier']] <= _TIER_RANK['tight']
                and top['params_b'] >= _CAPABLE_PARAMS_B):
            return None
        reason = ('本机只能勉强运行 %dB 以下的小模型，日常问答与 Agent '
                  '任务的体验会明显受限' % _CAPABLE_PARAMS_B)
    else:
        reason = '当前硬件（无可用 NVIDIA GPU 或内存不足）无法运行目录中的任何模型'
    pricing = pricing_for_model(_CLOUD_MODEL_ID) or {}
    return {
        'kind': 'cloud_api',
        'template_key': _CLOUD_TEMPLATE_KEY,
        'model_id': _CLOUD_MODEL_ID,
        'display': 'DeepSeek V4.1 Flash',
        'reason': reason,
        'pricing': {
            'input': pricing.get('input'),
            'output': pricing.get('output'),
            'currency': 'USD',
            'unit': 'per_million_tokens',
            'cache_read_mul': pricing.get('cacheReadMul'),
        },
    }
