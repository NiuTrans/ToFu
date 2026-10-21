"""Structured, static release-date knowledge for model ids.

This registry describes vendor knowledge only: when the creator first
published the trained model.  It is projected onto the model-routing v2
authority at read time (``public_projection``) and is never persisted into
the owner aggregate — the same boundary ``_context.py`` keeps for context
windows.  Unknown models deliberately return ``None`` instead of guessing
from generation numbers; the UI hides the fact for them.

Granularity contract: ``YYYY-MM-DD`` where a vendor day is evidenced,
``YYYY-MM`` where only the release month is.  Needles are matched against
the lowercase id with ``.`` folded to ``-`` so wire respellings
(``aws.claude-opus-4.8``, ``doubao-seed-2-0-pro-260215``) hit the same rule
as the canonical id.  Order matters: a more specific id must precede any
prefix of it (``gemini-3.5-flash-lite`` before ``gemini-3.5-flash``).
"""

from __future__ import annotations

_RULES: tuple[tuple[str, str], ...] = (
    # ── Anthropic ────────────────────────────────────────────────
    # Opus 5.5 GA 2026-09-22 (platform.claude.com/docs/en/models/opus-5-5/overview).
    ('claude-opus-5-5', '2026-09-22'),
    # Sonnet 5.5 GA 2026-09-28, Haiku 5.5 GA 2026-10-07 (anthropic.com news index).
    ('claude-sonnet-5-5', '2026-09-28'),
    ('claude-haiku-5-5', '2026-10-07'),
    ('claude-opus-5', '2026-08'),
    ('fable-5-1', '2026-09-01'),
    ('claude-fable-5', '2026-08'),
    ('claude-opus-4-8', '2026-08'),
    ('claude-opus-4-7', '2026-07'),
    ('claude-opus-4-6', '2026-05'),
    ('claude-sonnet-4-6', '2026-04'),
    ('claude-haiku-4-5', '2026-02'),
    # ── OpenAI ───────────────────────────────────────────────────
    # https://learn.chatgpt.com/docs/changelog (checked 2026-09-30).
    ('gpt-6-1-sol', '2026-09-29'),
    # GPT-6 Sol/Luna GA 2026-09-22 (developers.openai.com model pages +
    # openai.com/index/introducing-gpt-6-sol-and-luna). Order: astra last so
    # the bare 'gpt-6' substring never steals a dated sibling's rule.
    ('gpt-6-sol', '2026-09-22'),
    ('gpt-6-luna', '2026-09-22'),
    ('gpt-6-astra', '2026-09-03'),
    ('gpt-5-6-sol', '2026-08'),
    ('gpt-5-6-terra', '2026-08'),
    ('gpt-5-6-luna', '2026-08'),
    ('gpt-5-6', '2026-08'),
    ('gpt-5-5', '2026-06'),
    ('gpt-5-4-mini', '2026-05'),
    ('gpt-5-4', '2026-05'),
    ('gpt-5-3-codex-spark', '2026-03'),
    ('gpt-4-1', '2025-04'),  # vendor launch 2025-04-14, covers -mini/-nano
    # GPT-Image-2.5 (Sunburst/Flare) — default snapshots dated 2026-09-08 per
    # developers.openai.com/api/docs/models/gpt-image-2.5-sunburst (2026-09-20).
    ('gpt-image-2-5', '2026-09-08'),
    ('gpt-image-2', '2026-04'),
    ('gpt-image-1-5', '2025-12'),
    ('text-embedding-3', '2024-01'),  # vendor launch 2025-01-25 → 2024-01-25
    # ── Google ───────────────────────────────────────────────────
    # Gemini 4 Argon GA flagship (ai.google.dev/gemini-api/docs/models /
    # models/gemini-4-argon: stable id, latest update October 2026).
    ('gemini-4-argon', '2026-10'),
    ('gemini-3-8-flash', '2026-09'),
    ('gemini-3-7-flash', '2026-08'),
    ('gemini-3-6-flash', '2026-06'),
    ('gemini-3-5-flash-lite', '2026-05'),
    ('gemini-3-5-flash', '2026-05'),
    ('gemini-3-1-flash-lite-preview', '2026-02'),
    ('gemini-3-1-flash-image-preview', '2026-03'),
    ('gemini-3-1-pro-preview', '2026-02'),
    ('gemini-3-pro-image-preview', '2026-01'),
    ('gemini-3-flash-preview', '2025-12'),
    ('gemini-2-5-flash-image', '2025-08'),
    ('gemini-2-5-pro', '2025-06'),
    ('gemini-2-5-flash', '2025-06'),
    # ── DeepSeek ─────────────────────────────────────────────────
    # V4.1 Flash GA (2026-09-10); canonical id. The legacy v4-flash ids
    # route to it; V4 Pro remains a separate, available model.
    ('deepseek-v4-1-flash', '2026-09-10'),
    ('deepseek-flash', '2026-09-10'),
    ('deepseek-v4-pro-0813', '2026-08-12'),
    ('deepseek-v4-pro', '2026-05'),
    ('deepseek-v4-flash', '2026-05'),
    ('deepseek-v3-2', '2025-09'),
    # ── Zhipu GLM ────────────────────────────────────────────────
    # glm-5.3 anchored by its vendor model card dated 2026-08-14
    # (lib/model_info/_family.py, _capabilities.py).
    ('glm-5-3-flash', '2026-08'),
    ('glm-5-3', '2026-08'),
    ('glm-5-2', '2026-06'),
    ('glm-5-1', '2026-04'),
    ('glm-5v-turbo', '2026-05'),
    # ── Moonshot Kimi ────────────────────────────────────────────
    # kimi-k3 was live on the sankuai gateway by 2026-07-17
    # (lib/model_info/_max_output.py).
    ('kimi-k2-7-code', '2026-06'),
    ('kimi-k3', '2026-07'),
    ('kimi-k2-6', '2026-03'),
    ('kimi-k2-thinking', '2025-11'),
    # ── Alibaba Qwen ─────────────────────────────────────────────
    # qwen3.8-omni-flash GA 2026-09-18 (qwen.ai/blog + qwencloud model page).
    ('qwen3-8-omni-flash', '2026-09-18'),
    ('qwen3-8-max', '2026-08'),
    ('qwen3-8-flash', '2026-08'),
    ('qwen3-7-plus', '2026-05'),
    ('qwen3-5-plus', '2026-03'),
    ('qwen3-max', '2025-09'),
    ('qwen-plus', '2025-07'),
    ('qwen-flash', '2025-08'),
    ('text-embedding-v4', '2025-10'),
    # ── MiniMax ──────────────────────────────────────────────────
    # Xiaomi MiMo V2.6 series GA 2026-09-22 (mimo.mi.com models page; weights
    # open-sourced 2026-09-21). Covers -pro / -flash / -pro-ultraspeed.
    ('mimo-v2-6', '2026-09-22'),
    # M3.1-Flash-Preview: live on platform.minimax.io by 2026-10-10 (Anthropic-compat
    # docs + agent.minimax.cn M Plan promo); M Plan / MiniMax Code only at launch.
    ('minimax-m3-1', '2026-10'),
    ('minimax-m3', '2026-06'),
    ('minimax-m2-7', '2026-03'),
    ('minimax-m2-5', '2025-12'),
    # ── ByteDance Doubao ─────────────────────────────────────────
    # 260215 in the seed-2.0 wire ids is the vendor launch stamp.
    ('doubao-seed-asr-2-0', '2026-02'),
    # 260628 in the seed-2.1 pro wire id is the vendor snapshot stamp.
    ('doubao-seed-2-1', '2026-06'),
    ('doubao-seed-2-0', '2026-02'),
    # ── Tencent Hunyuan ──────────────────────────────────────────
    ('hy4-preview', '2026-07'),
    ('hy3-preview', '2026-04'),
    # ── Meituan LongCat ──────────────────────────────────────────
    # 2601/2603 in the flash ids are the vendor iteration stamps.
    ('longcat-2-0', '2026-06'),
    ('longcat-flash-omni-2603', '2026-03'),
    ('longcat-flash-thinking-2601', '2026-01'),
    # ── Meta ─────────────────────────────────────────────────────
    # muse-spark-1.3 was listed on the Meta Model API catalogue
    # 2026-09-02 (standard + contributor SKUs same day).
    ('muse-spark-1-3', '2026-09-02'),
    # ── Mistral ────────────────────────────────────────────────────
    # Mistral Large 4 Public Preview 2026-10-06 (docs model page, v26.10).
    ('mistral-large-4-0', '2026-10-06'),
    # ── xAI ──────────────────────────────────────────────────────
    # grok-4.7 (GA 2026-09-21, current flagship): docs.x.ai release notes.
    ('grok-4-7', '2026-09-21'),
    ('grok-4-6', '2026-08'),
    # xAI March release notes list the multi-agent variant separately;
    # its unversioned alias still lacks confirmed context and pricing.
    ('grok-4-20-multi-agent', '2026-03'),
    ('grok-4-20', '2026-06'),
    ('grok-4-1-mini', '2026-04'),
)


def _fold(model: str) -> str:
    return (model or '').strip().lower().replace('.', '-')


def release_date(model: str) -> str | None:
    """Return the vendor release date (``YYYY-MM[-DD]``) for *model*.

    Matching is a substring probe over the folded id so provider wire
    respellings (region prefixes, namespaces, snapshot stamps) resolve to
    the trained model's date.  ``None`` means no evidenced date.
    """
    folded = _fold(model)
    if not folded:
        return None
    for needle, date in _RULES:
        if needle in folded:
            return date
    return None


__all__ = ['release_date']
