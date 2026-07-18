# HOT_PATH — functions in this module are called per-request.
"""lib/model_info/_family.py — Model family detection predicates.

Pure name-based family checks (is_claude / is_qwen / is_gemini / …). These
have no dependencies on other model_info sub-modules and sit at the bottom of
the dependency graph (_capabilities and _max_output both import from here).
"""

import re

from lib.log import get_logger

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════
#  Model Detection Helpers
# ══════════════════════════════════════════════════════════

def is_claude(model: str) -> bool:
    """Anthropic Claude models (including AWS/GCP-prefixed variants)."""
    m = model.lower()
    return 'claude' in m or 'anthropic' in m


def is_claude_opus_47(model: str) -> bool:
    """Claude Opus 4.7+ (AWS-gateway / Bedrock / direct-API aliases).

    Opus 4.7 introduced breaking changes vs 4.6:
      • Sampling params (temperature/top_p/top_k) are silently ignored.
      • thinking.budget_tokens removed — only thinking.type='adaptive'.
      • Thinking content is HIDDEN by default — must send
        thinking.display='summarized' to surface the reasoning trace.
      • New 'xhigh' effort level between 'high' and 'max'.

    Detects these id variants:
      - claude-opus-4-7
      - aws.claude-opus-4.7
      - us.anthropic.claude-opus-4-7-v1:0
      - (future) claude-opus-4-8, etc.
    """
    m = model.lower()
    if 'claude' not in m and 'anthropic' not in m:
        return False
    # Extract (major, minor) from "opus-X-Y" or "opus-X.Y" — returns True iff
    # (major, minor) >= (4, 7).  Handles opus-4-7, opus-4.8, opus-5-0, etc.
    match = re.search(r'opus[-_.]?(\d+)[-_.](\d+)', m)
    if not match:
        return False
    major, minor = int(match.group(1)), int(match.group(2))
    return (major, minor) >= (4, 7)


def is_longcat(model: str) -> bool:
    """Internal LongCat models (Flash, MoE, etc.)."""
    return 'longcat' in model.lower()


def is_qwen(model: str) -> bool:
    """Alibaba Qwen models (including qwq/qvq reasoning variants)."""
    m = model.lower()
    return 'qwen' in m or 'qwq' in m or 'qvq' in m


def is_gemini(model: str) -> bool:
    """Google Gemini models."""
    return 'gemini' in model.lower()


def is_minimax(model: str) -> bool:
    """MiniMax models (M2, M2.5, M2.7, M2-her, etc.)."""
    m = model.lower()
    return 'minimax' in m or m == 'm2-her'


def is_doubao(model: str) -> bool:
    """ByteDance Doubao / Seed models."""
    m = model.lower()
    return 'doubao' in m or 'seed' in m


def is_glm(model: str) -> bool:
    """Zhipu GLM models (GLM-4, GLM-5, etc.)."""
    return 'glm' in model.lower()


def is_kimi(model: str) -> bool:
    """Moonshot Kimi models (kimi-k2, kimi-k2.5, kimi-k2.6, moonshot-v1, etc.)."""
    m = model.lower()
    return 'kimi' in m or 'moonshot' in m


def is_ernie(model: str) -> bool:
    """Baidu ERNIE models (ERNIE-5.0, ERNIE-X1, ERNIE-4.5, etc.)."""
    return 'ernie' in model.lower()


def is_gpt(model: str) -> bool:
    """OpenAI GPT models (gpt-4, gpt-4.1, gpt-4o, etc.)."""
    return 'gpt' in model.lower()


def is_deepseek(model: str) -> bool:
    """DeepSeek models (deepseek-v4-pro/-flash, deepseek-chat, deepseek-reasoner)."""
    return 'deepseek' in model.lower()
