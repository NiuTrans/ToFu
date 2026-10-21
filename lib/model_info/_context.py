"""Structured, static context-window knowledge for model ids.

This registry describes knowledge only. Runtime learned limits are composed by
callers and never written back here. Unknown models deliberately remain
``window=None`` instead of inheriting an operational safety fallback.

Matching runs in three ordered layers, most specific first:

1. LINE rules (:data:`_LINE_RULES`) parse a tiered line's tier token and
   generation, so every spelling of one served model resolves identically and
   a newer generation of a known line needs no new entry.
2. Needle rules (:data:`_EXACT_RULES` / :data:`_ESTIMATE_RULES`) are spelled-in
   ids, probed against the SEPARATOR-FOLDED spelling so '.' / '-' / '_'
   version punctuation cannot hide a known model.
3. ``None`` — unknown, never an operational safety fallback.

An id must never be matched by a raw substring of one of its spellings: the
2026-09-21 regression resolved the catalogue identity ``deepseek-v4.1-flash``
(1M window, enforced as 1M by its route) to the 128K DeepSeek family estimate,
because the spelled needles ``deepseek-flash`` and ``deepseek-v4-flash`` are
both absent from it.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from lib.log import get_logger
from lib.model_info._family import (
    claude_line_version,
    deepseek_line_version,
    is_deepseek,
)
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
    # DeepSeek generations are NOT spelled here — the flash/pro lines resolve
    # by tier + generation in _LINE_RULES.
    ('kimi-k3', 1_000_000, 'repository_verified'),
    # ChatGPT/Codex subscription ids: the oauth_codex provider's model rows
    # are rebuilt from the remote catalogue every refresh, so vendor-documented
    # windows must live here (same reason the gpt-5.6 ids ride the official
    # contract above). Order matters: 'gpt-5.4-mini' before 'gpt-5.4'.
    ('gpt-5.5', 1_050_000, 'vendor_official'),
    ('gpt-5.4-mini', 400_000, 'vendor_official'),
    ('gpt-5.4', 1_000_000, 'vendor_official'),
    ('gpt-5.3-codex-spark', 128_000, 'vendor_official'),
    # Sankuai/YourProvider gateway catalogue ids (2026-08-14 research, owner-
    # reviewed): vendor-documented windows promoted from the runtime-data
    # backfill into static knowledge so fresh installs and template setups
    # resolve them too. Substring order matters in two places:
    # 'gemini-3.5-flash-lite' before 'gemini-3.5-flash'. The
    # gemini-3.5–3.8 unknown guard below sits AFTER these rules, so
    # verified variants win while unknown future variants (e.g. a
    # hypothetical gemini-3.5-pro) still refuse to inherit the generic
    # 'gemini' family estimate.
    # Gemini 4 Argon (GA flagship, updated 2026-10): 1,048,576 unified
    # input+output context; 65,536 default max output (ai.google.dev model page).
    ('gemini-4-argon', 1_048_576, 'vendor_official'),
    ('gemini-3.5-flash-lite', 1_000_000, 'vendor_official'),
    ('gemini-3.5-flash', 1_000_000, 'vendor_official'),
    ('gemini-3.6-flash', 1_000_000, 'vendor_official'),
    ('gemini-3.7-flash', 1_000_000, 'vendor_official'),
    ('gemini-3.8-flash', 1_000_000, 'vendor_official'),
    ('glm-5.1', 200_000, 'vendor_official'),
    ('glm-5.2', 1_000_000, 'vendor_official'),
    # Opus 5.5 (GA 2026-09-22): 1M ctx / 128K max output (official model page).
    ('claude-opus-5.5', 1_000_000, 'vendor_official'),
    ('claude-opus-5-5', 1_000_000, 'vendor_official'),
    # Sonnet 5.5 (2026-09-28) / Haiku 5.5 (2026-10-07) resolve via the claude
    # line rules below (sonnet >=4.6, haiku >=5.5) — 1M ctx, 128K max out.
    # Mistral Large 4 (Public Preview 2026-10-06): 1M ctx (docs model page).
    ('mistral-large-4-0', 1_000_000, 'vendor_official'),
    # xAI Grok 4.7 (GA 2026-09-21): 500k context, text+image→text, no text output
    # limit (docs.x.ai release notes / grok-4-7 overview). 4.6 shares the 500k window.
    ('grok-4.7', 500_000, 'vendor_official'),
    ('grok-4.6', 500_000, 'vendor_official'),
    ('glm-5.3', 1_000_000, 'vendor_official'),
    ('glm-5v-turbo', 200_000, 'vendor_official'),
    ('hy3-preview', 256_000, 'vendor_official'),
    ('hy4-preview', 1_000_000, 'vendor_official'),
    ('longcat-2.0', 1_000_000, 'vendor_official'),
    # Qwen3.8-Omni-Flash (2026-09-18): 1M ctx / 131K max output (qwencloud model page).
    ('qwen3.8-omni-flash', 1_000_000, 'vendor_official'),
    # Xiaomi MiMo V2.6 (2026-09-22): 1M ctx / 128K max output (mimo.mi.com models page).
    ('mimo-v2.6', 1_000_000, 'vendor_official'),
    # Meta Muse Spark (dev.meta.ai/docs/models, 2026-09): 1,048,576
    # context; both SKUs share it. Substring covers -contributor.
    ('muse-spark-1.3', 1_048_576, 'vendor_official'),
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


# Version punctuation is not identity: gateways respell the same version as
# '3.5' / '3-5' / '3_5', so the probes below compare canonical spellings (the
# same treatment ``lib.model_info._folds.family_key`` gives id tokens).
_VERSION_SEPARATOR = re.compile(r'(?<=\d)[-_.](?=\d)')
_TOKEN_SPLIT = re.compile(r'[-_.:]+')


def _canon_id(model: str) -> str:
    """Separator-folded, lower-cased spelling of *model_id*."""
    return _VERSION_SEPARATOR.sub('.', (model or '').lower())


_CANON_EXACT = tuple((_canon_id(n), w, s) for n, w, s in _EXACT_RULES)
_CANON_ESTIMATE = tuple((_canon_id(n), w, s) for n, w, s in _ESTIMATE_RULES)


# Versioned LINES, as ``(family predicate, generation parser, lines)`` where a
# line is ``(tier token, generation floor, window, source)``. A line's window
# belongs to the LINE, not to one spelled id: 'deepseek-v4.1-flash' (catalogue
# identity), 'deepseek-v4-1-flash', 'deepseek-v4-flash' (legacy wire spelling)
# and the versionless canonical 'deepseek-flash' are one served model, and the
# V4 generation opened a 1M window for all of them. Extend a family by adding
# a parser to :mod:`lib.model_info._family` and its lines here — never by
# spelling one more id into the needle tables above.
_LINE_RULES: tuple[tuple, ...] = (
    (is_deepseek, deepseek_line_version, (
        # V4.1 Flash (2026-09-10): 1M ctx / 384K max output. The canonical id
        # 'deepseek-flash' is vendor-documented; the legacy v4-flash wire
        # spellings serve the same model, so the line shares one provenance.
        ('flash', (4, 0), 1_000_000, 'vendor_official'),
        ('pro', (4, 0), 1_000_000, 'repository_verified'),
    )),
)


def _line_profile(folded: str) -> ContextProfile | None:
    """Resolve a tiered line by parsing the id instead of spelling it."""
    for family, parser, lines in _LINE_RULES:
        if not family(folded):
            continue
        tokens = _TOKEN_SPLIT.split(folded)
        generation = parser(folded)
        for tier, floor, window, source in lines:
            if tier not in tokens:
                continue
            if generation is None:
                # A versionless id names the line's CURRENT generation (the
                # canonical 'deepseek-flash' IS V4.1 Flash). Require the tier
                # immediately after the family token so an unversioned
                # VARIANT (a hypothetical 'deepseek-r1-flash') cannot inherit
                # the line's window — it keeps the family estimate.
                if tokens[1:2] == [tier]:
                    return ContextProfile(window, source, True)
                continue
            if generation >= floor:
                return ContextProfile(window, source, True)
    return None


def context_profile(model: str, provider_id: str = '') -> dict:
    """Return ``{window, source, exact}`` for static model knowledge.

    Explicit metadata registered through ``lib.model_registration`` wins over
    the rules below; line rules win over needle rules, which win over family
    estimates (see the module docstring for the matching contract). Learned
    provider-specific values are composed by :func:`resolved_context_profile`.
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

    # Exact official IDs, including Astra, use the maintained model contract.
    # A provider's smaller default belongs to its Offering, not global knowledge;
    # the 272K price tier must never masquerade as the physical model window.
    if is_official_gpt56_model(name):
        return ContextProfile(
            GPT56_CONTEXT_WINDOW, 'openai_official', True).as_dict()

    # Existing verified Claude policy: newer Opus/Sonnet and Fable 5 are 1M;
    # older Claude aliases retain the established 200K family estimate. Haiku
    # joins the 1M line at 5.5 (GA 2026-10-07, official model page: 1M ctx).
    for line, floor in (('opus', (4, 6)), ('sonnet', (4, 6)), ('fable', (5, 0)),
                        ('haiku', (5, 5))):
        version = claude_line_version(raw, line)
        if version is not None and version >= floor:
            return ContextProfile(1_000_000, 'repository_verified', True).as_dict()
    if 'claude' in name:
        return ContextProfile(200_000, 'family_estimate', False).as_dict()

    folded = _canon_id(name)

    line = _line_profile(folded)
    if line is not None:
        return line.as_dict()
    for needle, window, source in _CANON_EXACT:
        if needle in folded:
            return ContextProfile(window, source, True).as_dict()
    if ('gemini-3.5' in folded or 'gemini-3.6' in folded
            or 'gemini-3.7' in folded or 'gemini-3.8' in folded):
        return _UNKNOWN.as_dict()
    for needle, window, source in _CANON_ESTIMATE:
        if needle in folded:
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
    vendor-documented one forever (live instance: example-corp::kimi-k3 expand
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
