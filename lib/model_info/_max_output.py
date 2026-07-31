# HOT_PATH — functions in this module are called per-request.
"""lib/model_info/_max_output.py — Per-model max output token limits.

Family-level output ceilings (_MODEL_MAX_OUTPUT) plus the per-model lookup
helpers for the families whose limit varies by variant (Qwen / MiniMax /
ERNIE / Kimi). Also holds the conservative unknown-family default
(_DEFAULT_UNKNOWN_MAX_OUTPUT).

Depends on the family predicates in ._family (acyclic). _limits._clamp_max_tokens
imports _MODEL_MAX_OUTPUT + _DEFAULT_UNKNOWN_MAX_OUTPUT from here.
"""

from lib.log import get_logger
from lib.model_info._family import (
    is_claude,
    is_doubao,
    is_ernie,
    is_gemini,
    is_glm,
    is_gpt,
    is_kimi,
    is_longcat,
    is_minimax,
    is_qwen,
)

logger = get_logger(__name__)


# ── Per-model max output token limits ──
# If the API rejects max_tokens > N, list the model family here.
# build_body() will clamp automatically.

def _qwen_max_output(model: str) -> int:
    """Return the max output token limit for a specific Qwen model.

    DashScope enforces strict per-model max_tokens limits:
      - qwen-turbo:    16,384
      - qwen-plus:     32,768
      - qwen3.5-plus:  32,768
      - qwen3.6-plus:  32,768
      - qwen3.5-flash: 32,768
      - qwen-max:      32,768
      - qwen3-max:     32,768
      - qwen3-vl-*:    32,768
      - qwq-plus:      65,536  (reasoning model)
      - qvq-max/plus:  32,768  (visual reasoning)
      - qwen3-coder-*: 65,536
      - qwen-long:     16,384
      - Default:       16,384  (safe minimum for unknown variants)
    """
    m = model.lower()
    # Reasoning models — higher limits
    if 'qwq' in m:
        return 65536
    # Visual reasoning models (QVQ)
    if 'qvq' in m:
        return 32768
    # Coder models — higher limits
    if 'coder' in m:
        return 65536
    # qwen-turbo / qwen3-turbo — lowest limit
    if 'turbo' in m:
        return 16384
    # qwen-plus / qwen3-plus / qwen3.6-plus — medium limit
    if 'plus' in m:
        return 32768
    # qwen-max / qwen3-max — medium limit
    if 'max' in m:
        return 32768
    # qwen-flash / qwen3.5-flash — medium limit
    if 'flash' in m:
        return 32768
    # qwen-vl — medium limit
    if 'vl' in m:
        return 32768
    # Unknown Qwen variant — use safe minimum
    return 16384


def _minimax_max_output(model: str) -> int:
    """Return the max output token limit for a specific MiniMax model.

    M2-her has a strict 2048 max_tokens limit.
    M2/M2.1/M2.5/M2.7 variants support up to 65536.
    """
    if 'her' in model.lower():
        return 2048
    return 65536


def _ernie_max_output(model: str) -> int:
    """Return the max output token limit for a specific Baidu ERNIE model.

    Per Qianfan V2 API model list (2026-04):
      - ERNIE 5.0 / thinking variants:   65,536
      - ERNIE X1.1:                       65,536
      - ERNIE X1 Turbo:                   28,160
      - ERNIE 4.5 Turbo (128k/32k):      12,288
      - ERNIE 4.5 Turbo VL:              16,384
      - ERNIE Speed / Lite (pro-128k):    4,096
      - Default:                          16,384
    """
    m = model.lower()
    if '5.0' in m or 'x1.1' in m:
        return 65536
    if 'x1' in m and 'turbo' in m:
        return 28160
    if 'speed' in m or 'lite' in m:
        return 4096
    if 'vl' in m:
        return 16384
    if '4.5' in m and 'turbo' in m:
        return 12288
    return 16384


def _kimi_max_output(model: str) -> int:
    """Return the max output token limit for a specific Kimi model.

    Moonshot Kimi model limits:
      - kimi-k3:                   32,768  (1000K context window, 2026-07-17)
      - kimi-k2.6:                 32,768  (default for K2.6 per docs)
      - kimi-k2.5:                 32,768  (default for K2.5)
      - kimi-k2-turbo-preview:     32,768
      - kimi-k2-thinking-turbo:    32,768
      - kimi-k2-*-preview:         32,768
      - kimi-k2-thinking:          32,768
      - moonshot-v1-*:             16,384  (legacy models)
      - Default:                   32,768
    """
    m = model.lower()
    if 'moonshot-v1' in m:
        return 16384
    return 32768


_MODEL_MAX_OUTPUT = {
    # (checker_fn, limit) — limit can be int or callable(model) → int
    'longcat': (is_longcat, 65536),
    'qwen':    (is_qwen,    _qwen_max_output),  # per-model lookup
    'gemini':  (is_gemini,  65536),
    'minimax': (is_minimax, _minimax_max_output),
    'kimi':    (is_kimi,    _kimi_max_output),    # per-model lookup
    'doubao':  (is_doubao,  16384),
    'ernie':   (is_ernie,   _ernie_max_output),   # per-model lookup
    'gpt':     (is_gpt,     32768),
    'glm':     (is_glm,     131072),
    # Claude: 128000 output limit — matches build_body's default. Listed
    # EXPLICITLY (rather than relying on no-clamp passthrough) so Claude is
    # NOT swept into the conservative unknown-family default below — long-form
    # paths deliberately pass max_tokens=128000 to Claude.
    'claude':  (is_claude,  128000),
}

# Conservative default output ceiling for model families we don't recognise.
# Without it, an unknown model used on a long-form path (max_tokens=128000)
# sends the full value and earns a guaranteed HTTP 400 on the FIRST call,
# relying on the post-400 auto-learn (_learn_model_limit) to recover — i.e.
# "the first call always fails". Clamping unknown models to the common family
# floor (16384, == the Qwen/Doubao/ERNIE minimum) lets the first call succeed;
# if a model actually supports more, add its family entry or let auto-learn
# raise it after a 400.
_DEFAULT_UNKNOWN_MAX_OUTPUT = 16384
