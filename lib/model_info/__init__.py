# HOT_PATH — functions in this module are called per-request.
"""lib/model_info/ — Model family detection and per-model token limit management.

Facade package (split from the former single ``lib/model_info.py``). Every
public name is re-exported here so all existing ``from lib.model_info import X``
call sites keep working byte-identically.

Sub-modules:
  _family        — Model family detection helpers (is_claude, is_qwen, …)
  _capabilities  — Reasoning-effort + continue/resume replay + vision probes
  _max_output    — Per-model max output token limits (_MODEL_MAX_OUTPUT)
  _limits        — Auto-learned model limits + clamp/learn/parse
                   (OWNS the single _LEARNED_MODEL_LIMITS dict + _limits_lock)

All public names are also re-exported from ``lib.llm`` for convenience.

Contains:
  • Model family detection helpers (is_claude, is_qwen, is_gemini, etc.)
  • Per-model max output token limits (_MODEL_MAX_OUTPUT)
  • Auto-learned model limits (_learn_model_limit, _load_learned_limits)
  • Token limit error parsing (_parse_token_limit_from_error)

CRITICAL: the auto-learned shared state (_LEARNED_MODEL_LIMITS dict,
_limits_lock) lives in ._limits and is re-exported BY REFERENCE. The dict is
rebound to its loaded value exactly once, inside ._limits — it is NOT rebound
here (that would create a divergent object). Everyone shares the one dict.
"""

from lib.log import get_logger

logger = get_logger(__name__)

# ── Family detection helpers ──
from lib.model_info._family import (  # noqa: E402,F401
    claude_line_version,
    glm_line_version,
    is_claude,
    is_claude_opus_47,
    is_deepseek,
    is_doubao,
    is_ernie,
    is_gemini,
    is_glm,
    is_glm53,
    is_gpt,
    is_gpt5,
    is_gpt_56,
    is_kimi,
    is_kimi_k3,
    is_longcat,
    is_minimax,
    is_qwen,
)

# ── Capability probes (reasoning / replay / vision) ──
from lib.model_info._capabilities import (  # noqa: E402,F401
    _GEMINI_EFFORT_MAP,
    _GLM52_EFFORT_MAP,
    _GLM53_EFFORT_MAP,
    _GPT_EFFORT_MAP,
    _KIMI_K3_EFFORT_MAP,
    gemini_reasoning_effort,
    glm_reasoning_effort,
    gpt_reasoning_effort,
    kimi_k3_reasoning_effort,
    model_requires_reasoning_content_replay,
    model_requires_thinking_signature_replay,
    model_requires_thought_signature_on_tool_calls,
    model_supports_assistant_prefill,
    model_supports_vision,
)

# ── Per-model max output token limits ──
from lib.model_info._max_output import (  # noqa: E402,F401
    _DEFAULT_UNKNOWN_MAX_OUTPUT,
    _MODEL_MAX_OUTPUT,
    _ernie_max_output,
    _kimi_max_output,
    _minimax_max_output,
    _qwen_max_output,
)

# ── Auto-learned limits + clamp/learn/parse ──
# _LEARNED_MODEL_LIMITS and _limits_lock are re-exported BY REFERENCE — the
# dict is loaded/rebound only inside ._limits, never here.
from lib.model_info._limits import (  # noqa: E402,F401
    _LEARNED_MODEL_LIMITS,
    _clamp_max_tokens,
    _learn_model_limit,
    _limits_lock,
    _load_learned_limits,
    _parse_token_limit_from_error,
)

# ── Video-frame budget (send-time clamp) ──
from lib.model_info._video import (  # noqa: E402,F401
    aggregate_image_cap,
    video_frame_budget,
)

# ── Structured context-window knowledge ──
from lib.model_info._context import (  # noqa: E402,F401
    ContextProfile,
    context_profile,
    resolved_context_profile,
)

# ── Display-fold knowledge (picker alias/family folding) ──
from lib.model_info._folds import (  # noqa: E402,F401
    build_fold_index,
    family_key,
    version_tuple,
)

__all__ = [
    # family detection
    'claude_line_version', 'glm_line_version',
    'is_claude', 'is_claude_opus_47', 'is_deepseek', 'is_doubao', 'is_ernie',
    'is_gemini', 'is_glm', 'is_glm53', 'is_gpt', 'is_gpt5', 'is_gpt_56',
    'is_kimi', 'is_kimi_k3', 'is_longcat', 'is_minimax', 'is_qwen',
    # capabilities
    'gemini_reasoning_effort', '_GEMINI_EFFORT_MAP',
    'glm_reasoning_effort', '_GLM52_EFFORT_MAP', '_GLM53_EFFORT_MAP',
    'gpt_reasoning_effort', '_GPT_EFFORT_MAP',
    'kimi_k3_reasoning_effort', '_KIMI_K3_EFFORT_MAP',
    'model_requires_reasoning_content_replay',
    'model_requires_thinking_signature_replay',
    'model_requires_thought_signature_on_tool_calls',
    'model_supports_assistant_prefill', 'model_supports_vision',
    # max output limits
    '_MODEL_MAX_OUTPUT', '_DEFAULT_UNKNOWN_MAX_OUTPUT',
    '_qwen_max_output', '_minimax_max_output', '_ernie_max_output',
    '_kimi_max_output',
    # learned limits / clamp
    '_clamp_max_tokens', '_learn_model_limit', '_load_learned_limits',
    '_parse_token_limit_from_error', '_LEARNED_MODEL_LIMITS', '_limits_lock',
    # video frame budget
    'video_frame_budget', 'aggregate_image_cap',
    # display folds
    'build_fold_index', 'family_key', 'version_tuple',
    # context windows
    'ContextProfile', 'context_profile', 'resolved_context_profile',
]
