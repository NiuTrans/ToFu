"""lib/local_serve/_diagnose.py — Failure classification for managed deploys.

When a deployment lands in ``failed`` the agent should not have to free-style
over a raw log tail: this module maps the ledger row's ``last_error`` plus
the bounded log tail onto a small set of known failure kinds, each with a
one-line summary and concrete next steps. The output is embedded in
``api.deployment_status`` as ``diagnosis`` so both the chat tool and the
settings page relay the SAME guidance.

``retryable`` answers the agent's first question — "is calling
local_serve_deploy again right now worthwhile?" — True only when the cause
is transient (a busy GPU, a full disk the user can clean, a slow model
load) rather than material (wrong architecture, missing driver, hopeless
VRAM).
"""

from __future__ import annotations

import re

from ._process import OOM_PATTERNS

__all__ = ['diagnose_failure']


def _rx(*patterns: str):
    return tuple(re.compile(p, re.IGNORECASE) for p in patterns)


_CUDA_PATTERNS = _rx(
    r'libcuda\.so', r'cuInit', r'no CUDA-capable device',
    r'CUDA driver version', r'CUDA error', r'found no NVIDIA driver',
    r'compute capability .+ is not compatible',
    r'The detected CUDA version', r'was compiled with CUDA',
)
_ARCH_PATTERNS = _rx(
    r'is not supported', r'does not support', r'unsupported model',
    r'Unrecognized configuration', r'architectures .+ not',
    r'Failed to (?:load|create) (?:the )?model',
)
_DISK_FULL_PATTERNS = _rx(r'No space left on device')
_PORT_PATTERNS = _rx(r'Address already in use', r'attempting to bind')
_NETWORK_PATTERNS = _rx(
    r'timed? ?out', r'Connection (?:refused|reset|aborted)',
    r'Failed to fetch', r'Temporary failure in name resolution',
    r'Network is unreachable', r'SSLError', r'Could not resolve',
)


def _any(patterns, text: str) -> bool:
    return bool(text) and any(p.search(text) for p in patterns)


def _diag(kind: str, summary: str, suggestions: list,
          retryable: bool) -> dict:
    return {'kind': kind, 'summary': summary, 'suggestions': suggestions,
            'retryable': retryable}


def diagnose_failure(record: dict, log_tail: str = '') -> dict:
    """Classify one failed deployment; never raises.

    Install-stage failures are read from ``last_error`` (the log does not
    exist yet); run-stage failures primarily from the log tail.
    """
    err = str((record or {}).get('last_error') or '')
    tail = str(log_tail or '')
    engine = str((record or {}).get('engine') or '')

    # ── install stage ────────────────────────────────────────────────
    if '将超出预算' in err or '磁盘余量不足' in err:
        return _diag('disk_budget',
                     '本地部署目录的磁盘预算不足，引擎环境装不下',
                     ['删除不再使用的引擎环境（data/local_serve/envs/ 下）',
                      '或调大 TOFU_LOCAL_SERVE_BUDGET_GB 后重新部署'],
                     retryable=True)
    if 'No space left on device' in err or _any(_DISK_FULL_PATTERNS, tail):
        return _diag('disk_full',
                     '磁盘已写满，安装或模型加载中断',
                     ['清理数据盘（模型权重与引擎环境都很占空间）后重新部署'],
                     retryable=True)
    if '无法准备 uv' in err:
        return _diag('installer_missing',
                     '缺少 uv 安装器且自动引导失败',
                     ['手动安装 uv（https://docs.astral.sh/uv/）后重新部署'],
                     retryable=True)
    if '预编译包' in err or '解包后未找到' in err:
        return _diag('platform',
                     '当前平台没有可用的 %s 预编译包' % engine,
                     ['手动安装该引擎后通过「接入已有端点」使用',
                      '或改选 llama.cpp / Ollama 路线（GGUF 模型）'],
                     retryable=False)
    if '无法查询 llama.cpp 发布信息' in err:
        return _diag('network',
                     '无法访问 GitHub 查询 llama.cpp 发布信息',
                     ['在 设置→网络 配置可用代理后重新部署',
                      '或改用 Ollama（安装源不同）'],
                     retryable=True)
    if ('安装失败' in err or '创建隔离环境失败' in err) \
            and _any(_NETWORK_PATTERNS, err):
        return _diag('network',
                     '引擎安装包下载失败（网络问题）',
                     ['在 设置→网络 配置可用代理，或为 uv 配置镜像源后重新部署'],
                     retryable=True)

    # ── run stage ────────────────────────────────────────────────────
    if _any(OOM_PATTERNS, tail) or '显存不足' in err:
        return _diag('oom_exhausted',
                     '显存不足，且自动降级链已用尽',
                     ['关闭其它占用显存的程序后直接重新部署（重试零成本）',
                      '换更小的量化版本（如 Q4_K_M → IQ4_XS）或 GGUF 模型',
                      '换参数更小的模型（让 Agent 重新推荐）',
                      '硬件确实不够时改用 DeepSeek V4 Flash 等低价 API'],
                     retryable=True)
    if _any(_CUDA_PATTERNS, tail) or _any(_CUDA_PATTERNS, err):
        return _diag('cuda_driver',
                     'NVIDIA 驱动 / CUDA 运行时异常',
                     ['运行 nvidia-smi 确认驱动可见显卡；不可见则安装或更新驱动',
                      '驱动过旧时升级到引擎要求的最低版本后重新部署',
                      '无法解决时改走 llama.cpp CPU 路线（GGUF 模型）'],
                     retryable=False)
    if _any(_ARCH_PATTERNS, tail):
        return _diag('arch_unsupported',
                     '%s 不支持该模型架构' % (engine or '当前引擎'),
                     ['换引擎重新部署（vLLM ↔ SGLang 支持面不同）',
                      '或改用该模型的 GGUF 量化版走 llama.cpp / Ollama'],
                     retryable=False)
    if _any(_PORT_PATTERNS, tail):
        return _diag('port_conflict',
                     '托管端口被其它进程占用',
                     ['用 local_serve_list 检查是否有残留实例并停止',
                      '若不是托管实例占用，找到占用进程关闭后重新部署'],
                     retryable=True)
    if err.startswith('初始化命令失败'):
        return _diag('ollama_setup',
                     'Ollama 模型导入（ollama create）失败',
                     ['GGUF 文件可能损坏或不完整，重新下载后部署',
                      '或改用 llama.cpp 直接加载该 GGUF（无需导入）'],
                     retryable=False)
    if '等待就绪超时' in err:
        return _diag('start_timeout',
                     '服务在就绪超时内没有起来（常见于大模型 + 慢磁盘）',
                     ['调大 TOFU_LOCAL_SERVE_READY_TIMEOUT_SEC 后重新部署',
                      '反复超时时把日志尾部贴给 Agent 进一步分析'],
                     retryable=True)
    if 'Tofu 服务重启导致部署中断' in err:
        return _diag('interrupted',
                     '部署被 Tofu 服务重启打断，并非模型或硬件问题',
                     ['直接重新调用 local_serve_deploy 即可'],
                     retryable=True)
    return _diag('unknown',
                 '启动失败，未匹配到已知模式',
                 ['把完整日志尾部转述给用户，由 Agent 结合日志进一步分析',
                  '确认模型文件完整（下载中断的权重常表现为奇怪报错）'],
                 retryable=False)
