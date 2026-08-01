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
    """Anthropic models (Claude + Fable, including AWS/GCP-prefixed variants).

    ``fable`` is Anthropic's Fable line (Fable 5, May 2026) — it speaks the
    same Messages API shape as Claude (thinking.type='adaptive', cache
    1.25/0.10 multipliers, no assistant prefill), so every Claude-family
    code path treats it identically."""
    m = model.lower()
    return 'claude' in m or 'anthropic' in m or 'fable' in m


def claude_line_version(model: str, line: str) -> tuple[int, int] | None:
    """Extract ``(major, minor)`` for ONE Claude model line ('opus', 'sonnet',
    'haiku', 'fable').

    THE single version parser for every "Claude generation ≥ N" decision —
    the thinking-generation gate (:func:`is_claude_opus_47`) and the
    compaction 1M-context table (``tasks_pkg/compaction/_tokens``) both ride
    it, so a new bare alias (e.g. ``claude-sonnet-6``) can never again be
    visible to one consumer and invisible to the other.

    Minor-OPTIONAL and date-aware:

      claude-sonnet-5                     → (5, 0)   bare-major alias
      claude-sonnet-5-20250630            → (5, 0)   date suffix ≠ minor
      aws.claude-opus-4.7                 → (4, 7)
      us.anthropic.claude-opus-4-7-v1:0   → (4, 7)   gateway prefix + build tag
      yuju-claude-opus-5-evaDaily         → (5, 0)
      claude-opus-4-20250514              → (4, 0)   Opus 4.0 snapshot
      claude-3-opus-20240229              → None     gen-3 shape (version
                                                     BEFORE the line name)
      gpt-4o / deepseek-v4-flash          → None     not Claude family

    Version groups are capped at two digits with a digit-boundary lookahead,
    so an 8-digit YYYYMMDD snapshot suffix can never be misread as a version
    (the bug that let ``claude-opus-4-20250514`` pass as ≥ 4.7).
    """
    if not is_claude(model):
        return None
    m = model.lower()
    match = re.search(line + r'[-_.]?(\d{1,2})(?!\d)(?:[-_.](\d{1,2})(?!\d))?',
                      m)
    if not match:
        return None
    major = int(match.group(1))
    minor = int(match.group(2)) if match.group(2) else 0
    return (major, minor)


def is_claude_opus_47(model: str) -> bool:
    """Claude Opus 4.7+ (AWS-gateway / Bedrock / direct-API aliases).

    Opus 4.7 introduced breaking changes vs 4.6:
      • Sampling params (temperature/top_p/top_k) are silently ignored.
      • thinking.budget_tokens removed — only thinking.type='adaptive'.
      • Thinking content is HIDDEN by default — must send
        thinking.display='summarized' to surface the reasoning trace.
      • New 'xhigh' effort level between 'high' and 'max'.

    Version parsing rides :func:`claude_line_version`, so dated snapshots
    (``claude-opus-4-20250514`` → (4,0)) and gen-3 shapes
    (``claude-3-opus-20240229`` → None) can no longer masquerade as 4.7+.
    """
    v = claude_line_version(model, 'opus')
    return v is not None and v >= (4, 7)


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


def is_kimi_k3(model: str) -> bool:
    """Moonshot Kimi K3 specifically (kimi-k3, kimi-k3.1, …).

    K3 speaks a DIFFERENT thinking contract from the K2 line: top-level
    ``reasoning_effort`` (low/high/max, default max) and a fixed
    temperature=1.0 that rejects any other value with HTTP 400 (verified
    live against the sankuai gateway 2026-07-24). The K2-style
    ``thinking:{type:...}`` + temperature shape must NOT be sent to K3.
    """
    return 'kimi-k3' in model.lower()


def is_ernie(model: str) -> bool:
    """Baidu ERNIE models (ERNIE-5.0, ERNIE-X1, ERNIE-4.5, etc.)."""
    return 'ernie' in model.lower()


def is_gpt(model: str) -> bool:
    """OpenAI GPT models (gpt-4, gpt-4.1, gpt-4o, etc.)."""
    return 'gpt' in model.lower()


def is_gpt5(model: str) -> bool:
    """OpenAI GPT-5 family reasoning models (gpt-5, gpt-5.2, gpt-5.4, gpt-5.6,
    including -mini / -nano / -pro / -codex variants).

    These take the OpenAI-native ``reasoning_effort`` knob. Deliberately
    excludes ``gpt-oss`` (no 'gpt-5' substring) and the o-series / gpt-4o
    (which route through the plain-OpenAI branch unchanged).
    """
    return 'gpt-5' in model.lower()


def is_gpt_56(model: str) -> bool:
    """GPT-5.6+ — the first GPT generation to expose the ``ultra`` reasoning
    effort tier. Older GPT-5.x models clamp ``ultra`` down to ``high``.

    Extracts the minor version from ``gpt-5``/``gpt-5.6``/``gpt-5.6-mini`` and
    returns True iff minor >= 6 (``gpt-5`` alone == minor 0).
    """
    m = model.lower()
    if 'gpt-5' not in m:
        return False
    match = re.search(r'gpt-5(?:[.\-](\d+))?', m)
    if not match:
        return False
    minor = int(match.group(1)) if match.group(1) else 0
    return minor >= 6


def is_deepseek(model: str) -> bool:
    """DeepSeek models (deepseek-v4-pro/-flash, deepseek-chat, deepseek-reasoner)."""
    return 'deepseek' in model.lower()
