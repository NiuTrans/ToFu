"""lib/local_serve/_probe.py — Model-path inspection and hardware probing.

Everything in this module is read-only and never raises on bad input: the
chat agent shows these dicts to the user verbatim, so every failure is a
structured ``error`` field, not a traceback.

Model inspection
----------------
``inspect_model_path`` recognises two formats:

* **HF directory** — a folder with ``config.json`` (transformers layout).
  We read the architecture/context/dtype from the config (descending one
  level into ``text_config`` for composite models like Gemma 3 / Llama 4)
  and sum the actual weight bytes from the safetensors index (preferred)
  or the shard files on disk. Parameter COUNT is only estimated
  (weight_bytes / dtype_size); it is a sizing hint for the planner, not a
  spec-sheet number.
* **GGUF file** — the header metadata is parsed directly (no dependency):
  magic ``GGUF``, version, then the metadata KV section which carries
  ``general.architecture``, ``general.name``, ``general.file_type`` (the
  quant label) and ``<arch>.context_length``.

Hardware probing
----------------
``probe_hardware`` reports NVIDIA GPUs via ``nvidia-smi`` (bounded 5 s
subprocess), RAM via the cgroup authority with a ``/proc/meminfo``
fallback, disk headroom on the data volume, and the allowed CPU count.
A machine without nvidia-smi simply yields ``gpus: []`` — the planner
turns that into a CPU/llama.cpp plan rather than an error.
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess

from lib.cgroup_guard import mem_limit_bytes, mem_usage_bytes
from lib.log import get_logger
from lib.runtime_paths import data_root

logger = get_logger(__name__)

__all__ = ['inspect_model_path', 'probe_hardware']

_NVIDIA_SMI_TIMEOUT = 5
_MAX_GGUF_KV = 10000        # metadata section is always far smaller; a bound, not an expectation
_MAX_GGUF_KEY = 1024
_MAX_GGUF_ARRAY = 1_000_000
_MAX_GGUF_STRING = 1 << 20

# GGUF metadata value type tags (little-endian section, gguf v3 layout).
_GGUF_SCALAR_SIZE = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1,
                     10: 8, 11: 8, 12: 8}
_GGUF_FMT = {0: '<B', 1: '<b', 2: '<H', 3: '<h', 4: '<I', 5: '<i',
             6: '<f', 7: '<?', 10: '<Q', 11: '<q', 12: '<d'}

# llama.cpp file_type enum → human quant label (common values only).
_GGUF_FILE_TYPES = {
    0: 'F32', 1: 'F16', 2: 'Q4_0', 3: 'Q4_1', 7: 'Q8_0',
    10: 'Q2_K', 11: 'Q3_K_S', 12: 'Q3_K_M', 13: 'Q3_K_L',
    14: 'Q4_K_S', 15: 'Q4_K_M', 16: 'Q5_K_S', 17: 'Q5_K_M', 18: 'Q6_K',
    19: 'IQ2_XXS', 20: 'IQ2_XS', 21: 'Q2_K_S', 22: 'IQ3_XS', 23: 'IQ3_XXS',
    24: 'IQ1_S', 25: 'IQ4_NL', 26: 'IQ3_S', 27: 'IQ3_M', 28: 'IQ2_S',
    29: 'IQ2_M', 30: 'IQ4_XS', 31: 'IQ1_M', 32: 'BF16',
    38: 'MXFP4',
}


# ─────────────────────────── HF directory ───────────────────────────

def _dtype_size(dtype: str) -> int:
    d = (dtype or '').lower()
    if d in ('float16', 'fp16', 'bfloat16', 'bf16', 'float16_e5m10'):
        return 2
    if d in ('float64', 'fp64', 'double'):
        return 8
    if d in ('float8_e4m3fn', 'float8_e5m2', 'fp8'):
        return 1
    return 4  # float32 default


def _hf_weight_bytes(path: str) -> tuple[int, str]:
    """Total on-disk weight bytes; prefers the safetensors index."""
    index = os.path.join(path, 'model.safetensors.index.json')
    try:
        with open(index, 'r', encoding='utf-8') as f:
            total = int(json.load(f).get('metadata', {}).get('total_size', 0))
        if total > 0:
            return total, 'safetensors-index'
    except (OSError, ValueError, TypeError) as e:
        logger.debug('[LocalServe] unreadable safetensors index %s: %s', index, e)
    total = 0
    try:
        for name in os.listdir(path):
            if name.endswith(('.safetensors', '.bin', '.pt', '.pth')):
                total += os.path.getsize(os.path.join(path, name))
    except OSError as e:
        logger.debug('[LocalServe] weight scan of %s failed: %s', path, e)
    return total, 'shard-files'


def _inspect_hf_dir(path: str) -> dict:
    cfg_path = os.path.join(path, 'config.json')
    if not os.path.isfile(cfg_path):
        return {'format': 'unknown', 'path': path,
                    'error': '目录中没有 config.json（既不是 HF 模型目录，也不是 .gguf 文件）'}
    try:
        with open(cfg_path, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        return {'format': 'unknown', 'path': path,
                'error': 'config.json 解析失败: %s' % e}
    if not isinstance(cfg, dict):
        return {'format': 'unknown', 'path': path, 'error': 'config.json 不是 JSON 对象'}

    # Composite models (Gemma 3, Llama 4, …) nest the LM fields one level
    # down under text_config; merge with the top level taking precedence.
    text = cfg.get('text_config') if isinstance(cfg.get('text_config'), dict) else {}

    def _field(*names):
        for n in names:
            if cfg.get(n) is not None:
                return cfg[n]
            if text.get(n) is not None:
                return text[n]
        return None

    archs = cfg.get('architectures')
    arch = archs[0] if isinstance(archs, list) and archs else None
    dtype = str(_field('torch_dtype', 'dtype') or '').lower()
    weight_bytes, weight_src = _hf_weight_bytes(path)
    quant_cfg = cfg.get('quantization_config')
    quant = None
    if isinstance(quant_cfg, dict):
        quant = str(quant_cfg.get('quant_method') or quant_cfg.get('fmt') or 'quantized')
    out = {
        'format': 'hf',
        'path': path,
        'architecture': arch,
        'model_type': _field('model_type'),
        'max_context': _field('max_position_embeddings', 'max_sequence_length'),
        'hidden_size': _field('hidden_size'),
        'num_hidden_layers': _field('num_hidden_layers'),
        'dtype': dtype or None,
        'quantization': quant,
        'weight_bytes': weight_bytes or None,
        'weight_source': weight_src,
        'served_name': os.path.basename(os.path.normpath(path)),
    }
    dsz = _dtype_size(dtype)
    if weight_bytes and dsz:
        out['param_count_estimate'] = weight_bytes // dsz
    if not weight_bytes:
        out['error'] = '未找到权重文件（*.safetensors / *.bin），目录可能不完整'
    return out


# ─────────────────────────── GGUF file ───────────────────────────

def _gguf_read(f, fmt: str):
    size = struct.calcsize(fmt)
    buf = f.read(size)
    if len(buf) < size:
        raise ValueError('unexpected EOF')
    return struct.unpack(fmt, buf)[0]


def _gguf_string(f, max_len: int) -> str:
    n = _gguf_read(f, '<Q')
    if n > max_len:
        raise ValueError('string too long: %d' % n)
    return f.read(n).decode('utf-8', 'replace')


def _gguf_skip_value(f, vtype: int, depth: int = 0) -> None:
    if depth > 2:
        raise ValueError('nested array too deep')
    if vtype == 8:
        _gguf_string(f, _MAX_GGUF_STRING)
        return
    if vtype == 9:
        etype = _gguf_read(f, '<I')
        count = _gguf_read(f, '<Q')
        if count > _MAX_GGUF_ARRAY:
            raise ValueError('array too long: %d' % count)
        if etype == 8:
            for _ in range(count):
                _gguf_string(f, _MAX_GGUF_STRING)
        elif etype == 9:
            for _ in range(count):
                _gguf_skip_value(f, etype, depth + 1)
        else:
            size = _GGUF_SCALAR_SIZE.get(etype)
            if size is None:
                raise ValueError('bad array elem type: %d' % etype)
            f.seek(size * count, os.SEEK_CUR)
        return
    size = _GGUF_SCALAR_SIZE.get(vtype)
    if size is None:
        raise ValueError('bad value type: %d' % vtype)
    f.seek(size, os.SEEK_CUR)


def _inspect_gguf(path: str) -> dict:
    wanted_scalars = ('general.file_type',)
    try:
        with open(path, 'rb') as f:
            if f.read(4) != b'GGUF':
                return {'format': 'unknown', 'path': path,
                        'error': '文件不是 GGUF（magic 不匹配）'}
            version = _gguf_read(f, '<I')
            if version not in (2, 3):
                return {'format': 'unknown', 'path': path,
                        'error': '不支持的 GGUF 版本: %d' % version}
            _gguf_read(f, '<Q')  # tensor_count
            kv_count = _gguf_read(f, '<Q')
            if kv_count > _MAX_GGUF_KV:
                return {'format': 'unknown', 'path': path,
                        'error': 'GGUF 元数据条目数异常: %d' % kv_count}
            meta = {}
            for _ in range(kv_count):
                key = _gguf_string(f, _MAX_GGUF_KEY)
                vtype = _gguf_read(f, '<I')
                if key.endswith('.context_length') or key in (
                        'general.architecture', 'general.name',
                        'general.size_label') or key in wanted_scalars:
                    if vtype == 8:
                        meta[key] = _gguf_string(f, _MAX_GGUF_STRING)
                    elif vtype in _GGUF_FMT and vtype != 9:
                        meta[key] = _gguf_read(f, _GGUF_FMT[vtype])
                    else:
                        _gguf_skip_value(f, vtype)
                else:
                    _gguf_skip_value(f, vtype)
    except (OSError, ValueError, struct.error) as e:
        return {'format': 'unknown', 'path': path,
                'error': 'GGUF 头解析失败: %s' % e}

    arch = meta.get('general.architecture')
    ctx = meta.get('%s.context_length' % arch) if arch else None
    file_type = meta.get('general.file_type')
    weight_bytes = None
    try:
        weight_bytes = os.path.getsize(path)
    except OSError:
        pass
    return {
        'format': 'gguf',
        'path': path,
        'architecture': arch,
        'model_type': arch,
        'max_context': ctx,
        'quantization': _GGUF_FILE_TYPES.get(file_type, 'FT%s' % file_type
                                             if file_type is not None else None),
        'weight_bytes': weight_bytes,
        'served_name': os.path.splitext(os.path.basename(path))[0],
    }


def inspect_model_path(path: str) -> dict:
    """Inspect a user-supplied model path; never raises.

    Returns a dict whose ``format`` is ``hf`` | ``gguf`` | ``unknown``.
    On ``unknown`` (or a partial read) ``error`` carries a user-facing
    Chinese explanation — the agent relays it verbatim.
    """
    p = os.path.abspath(os.path.expanduser((path or '').strip()))
    if not p or p == os.path.abspath(os.sep):
        return {'format': 'unknown', 'path': path, 'error': '路径为空'}
    if not os.path.exists(p):
        return {'format': 'unknown', 'path': p, 'error': '路径不存在: %s' % p}
    if os.path.isdir(p):
        return _inspect_hf_dir(p)
    if p.lower().endswith('.gguf'):
        return _inspect_gguf(p)
    return {'format': 'unknown', 'path': p,
            'error': '无法识别的模型格式：请提供 HF 模型目录（含 config.json）或 .gguf 文件'}


# ─────────────────────────── hardware ───────────────────────────

def _probe_gpus(timeout: float) -> list:
    try:
        proc = subprocess.run(
            ['nvidia-smi',
             '--query-gpu=index,name,memory.total,memory.free,driver_version,compute_cap',
             '--format=csv=noheader,nounits'],
            capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug('[LocalServe] nvidia-smi unavailable: %s', e)
        return []
    if proc.returncode != 0:
        logger.debug('[LocalServe] nvidia-smi rc=%d: %s',
                     proc.returncode, (proc.stderr or '').strip()[:200])
        return []
    gpus = []
    for line in (proc.stdout or '').splitlines():
        parts = [c.strip() for c in line.split(',')]
        if len(parts) < 6:
            continue
        try:
            gpus.append({
                'index': int(parts[0]),
                'name': parts[1],
                'vram_total_bytes': int(float(parts[2])) * 1024 * 1024,
                'vram_free_bytes': int(float(parts[3])) * 1024 * 1024,
                'driver': parts[4],
                'compute_cap': parts[5],
            })
        except (ValueError, TypeError):
            continue
    return gpus


def _probe_ram() -> dict:
    limit = mem_limit_bytes()
    usage = mem_usage_bytes()
    if limit:
        return {'ram_total_bytes': limit,
                'ram_available_bytes': max(0, limit - (usage or 0)),
                'source': 'cgroup'}
    try:
        info = {}
        with open('/proc/meminfo', 'r', encoding='utf-8') as f:
            for line in f:
                key, _, rest = line.partition(':')
                info[key.strip()] = rest.strip()
        total = int(info.get('MemTotal', '0 kB').split()[0]) * 1024
        avail = int(info.get('MemAvailable', '0 kB').split()[0]) * 1024
        if total:
            return {'ram_total_bytes': total, 'ram_available_bytes': avail,
                    'source': 'procfs'}
    except (OSError, ValueError, IndexError) as e:
        logger.debug('[LocalServe] /proc/meminfo unreadable: %s', e)
    return {'ram_total_bytes': None, 'ram_available_bytes': None, 'source': 'unknown'}


def _allowed_cpu_count() -> int:
    try:
        return max(1, len(os.sched_getaffinity(0)))  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return max(1, os.cpu_count() or 1)


def probe_hardware(*, nvidia_timeout: float = _NVIDIA_SMI_TIMEOUT,
                   disk_path: str | None = None) -> dict:
    """One hardware snapshot; individual facets degrade to None/[] independently."""
    ram = _probe_ram()
    target = disk_path or data_root()
    disk = {'disk_free_bytes': None, 'disk_total_bytes': None}
    try:
        du = shutil.disk_usage(target)
        disk = {'disk_free_bytes': du.free, 'disk_total_bytes': du.total,
                'disk_path': target}
    except OSError as e:
        logger.debug('[LocalServe] disk_usage(%s) failed: %s', target, e)
    return {
        'gpus': _probe_gpus(nvidia_timeout),
        **ram,
        **disk,
        'cpu_count': _allowed_cpu_count(),
    }
