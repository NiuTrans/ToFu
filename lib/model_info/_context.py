"""Structured, static context-window knowledge for model ids.

This registry describes knowledge only. Runtime learned limits are composed by
callers and never written back here. Unknown models deliberately remain
``window=None`` instead of inheriting an operational safety fallback.
"""

from __future__ import annotations

from dataclasses import dataclass

from lib.log import get_logger
from lib.model_info._family import claude_line_version
from lib.model_info._openai_gpt56 import (
    GPT56_CONTEXT_WINDOW,
    is_official_gpt56_model,
)

logger = get_logger(__name__)


@dataclass(frozen=True)
class ContextProfile:
    window: int | None
    source: str
    exact: bool

    def as_dict(self) -> dict:
        return {'window': self.window, 'source': self.source, 'exact': self.exact}


_UNKNOWN = ContextProfile(None, 'unknown', False)

# Migrated from the previously operational hardcode in compaction/_tokens.py.
# ``repository_verified`` means the value is backed by an existing repository
# comment/test/template; values inferred only at family level are estimates.
_EXACT_RULES: tuple[tuple[str, int, str], ...] = (
    ('deepseek-v4-pro', 1_000_000, 'repository_verified'),
    ('deepseek-v4-flash', 1_000_000, 'repository_verified'),
    ('kimi-k3', 1_000_000, 'repository_verified'),
    # ChatGPT/Codex subscription ids: the oauth_codex provider's model rows
    # are rebuilt from the remote catalogue every refresh, so vendor-documented
    # windows must live here (same reason the gpt-5.6 ids ride the official
    # contract above). Order matters: 'gpt-5.4-mini' before 'gpt-5.4'.
    ('gpt-5.5', 1_050_000, 'vendor_official'),
    ('gpt-5.4-mini', 400_000, 'vendor_official'),
    ('gpt-5.4', 1_000_000, 'vendor_official'),
    ('gpt-5.3-codex-spark', 128_000, 'vendor_official'),
    # Sankuai/Meituan gateway catalogue ids (2026-08-14 research, owner-
    # reviewed): vendor-documented windows promoted from the runtime-data
    # backfill into static knowledge so fresh installs and template setups
    # resolve them too. Substring order matters in two places:
    # 'gemini-3.5-flash-lite' before 'gemini-3.5-flash'. The gemini-3.5/3.6
    # unknown guard below sits AFTER these rules, so verified variants win
    # while unknown future variants (e.g. a hypothetical gemini-3.5-pro)
    # still refuse to inherit the generic 'gemini' family estimate.
    ('gemini-3.5-flash-lite', 1_000_000, 'vendor_official'),
    ('gemini-3.5-flash', 1_000_000, 'vendor_official'),
    ('gemini-3.6-flash', 1_000_000, 'vendor_official'),
    ('glm-5.1', 200_000, 'vendor_official'),
    ('glm-5.2', 1_000_000, 'vendor_official'),
    ('glm-5.3', 1_000_000, 'vendor_official'),
    ('glm-5v-turbo', 200_000, 'vendor_official'),
    ('hy3-preview', 256_000, 'vendor_official'),
    ('longcat-2.0', 1_000_000, 'vendor_official'),
    ('text-embedding-3', 8_191, 'vendor_official'),
    ('text-embedding-v4', 8_192, 'vendor_official'),
)
_ESTIMATE_RULES: tuple[tuple[str, int, str], ...] = (
    ('gpt-5.6-sol-wm', 1_050_000, 'family_estimate'),
    # LongCat-Flash iterations ride the 128K LongCat-Flash base (official
    # tech report: "context length is extended to 128k"); the 2601/2603
    # release notes state no window change. Flash-Chat is deliberately NOT
    # covered here — its Dec 2025 upgrade moved it to 256K.
    ('longcat-flash-thinking', 128_000, 'family_estimate'),
    ('longcat-flash-omni', 128_000, 'family_estimate'),
    ('gpt-4o', 128_000, 'family_estimate'),
    ('gpt-4', 128_000, 'family_estimate'),
    ('o1', 200_000, 'family_estimate'),
    ('o3', 200_000, 'family_estimate'),
    ('o4', 200_000, 'family_estimate'),
    ('gemini', 1_000_000, 'family_estimate'),
    ('qwen', 128_000, 'family_estimate'),
    ('deepseek', 128_000, 'family_estimate'),
    ('doubao', 128_000, 'family_estimate'),
    ('minimax', 1_000_000, 'family_estimate'),
)


def context_profile(model: str, provider_id: str = '') -> dict:
    """Return ``{window, source, exact}`` for static model knowledge.

    Explicit metadata registered through ``lib.model_registration`` wins over
    family rules. Learned provider-specific values are composed by
    :func:`resolved_context_profile`.
    """
    raw = model or ''
    name = raw.lower()
    if not name:
        return _UNKNOWN.as_dict()

    try:
        from lib.model_registration import registered_context_profile
        registered = registered_context_profile(raw, provider_id)
    except Exception as exc:
        logger.debug('[ModelContext] registration lookup failed: %s', exc)
        registered = None
    if registered is not None:
        return registered

    if is_official_gpt56_model(name):
        return ContextProfile(
            GPT56_CONTEXT_WINDOW, 'openai_official', True).as_dict()

    # Existing verified Claude policy: newer Opus/Sonnet and Fable 5 are 1M;
    # older Claude aliases retain the established 200K family estimate.
    for line, floor in (('opus', (4, 6)), ('sonnet', (4, 6)), ('fable', (5, 0))):
        version = claude_line_version(raw, line)
        if version is not None and version >= floor:
            return ContextProfile(1_000_000, 'repository_verified', True).as_dict()
    if 'claude' in name:
        return ContextProfile(200_000, 'family_estimate', False).as_dict()

    for needle, window, source in _EXACT_RULES:
        if needle in name:
            return ContextProfile(window, source, True).as_dict()
    if 'gemini-3.5' in name or 'gemini-3.6' in name:
        return _UNKNOWN.as_dict()
    for needle, window, source in _ESTIMATE_RULES:
        if needle in name:
            return ContextProfile(window, source, False).as_dict()

    # New catalogue ids without repository evidence must stay unknown rather
    # than guessing from marketing generation numbers.
    return _UNKNOWN.as_dict()


def resolved_context_profile(model: str, provider_id: str = '') -> dict:
    """Compose static knowledge with a provider+model learned override.

    Source-aware, mirroring ``lib.context_limits.resolve_learned_context_limit``:
    a *shrink* entry overrides the static window (its entire purpose), while
    an *expand* entry is a FLOOR-only corroboration — ``max(static, learned)``
    — because an expand observation is "at least this much worked", never a
    ceiling. Treating a stale expand as absolute pins the window below the
    vendor-documented one forever (live instance: sankuai::kimi-k3 expand
    383,727 masking the real 1M window, 2026-07-26). When static knowledge
    wins, the static profile is returned unchanged so provenance (verified /
    exact) survives to the UI.
    """
    profile = context_profile(model, provider_id)
    try:
        from lib.context_limits import (
            lookup_learned_context_limit,
            resolve_learned_context_limit,
        )
        learned = lookup_learned_context_limit(provider_id, model)
    except Exception as exc:
        logger.debug('[ModelContext] learned lookup failed: %s', exc)
        learned = None
    if learned is None:
        return profile
    if profile.get('window') is None:
        return {
            'window': int(learned),
            'source': 'learned:%s' % (provider_id or 'model'),
            'exact': False,
        }
    composed = resolve_learned_context_limit(
        provider_id, model, int(profile['window']))
    if composed == int(profile['window']):
        return profile
    return {
        'window': int(composed),
        'source': 'learned:%s' % (provider_id or 'model'),
        'exact': False,
    }


__all__ = ['ContextProfile', 'context_profile', 'resolved_context_profile']
