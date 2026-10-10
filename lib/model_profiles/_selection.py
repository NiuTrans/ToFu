"""Conservative role/tier selection over configured model profiles."""

from __future__ import annotations

import math

from lib.log import get_logger
from lib.model_profiles._catalog import configured_model_profiles

logger = get_logger(__name__)

_TIER_TARGET = {'light': 100, 'standard': 200, 'heavy': 300}


def _candidate_key(profile: dict, *, tier: str, role: str,
                   parent_model: str) -> tuple:
    quality = int(profile.get('qualityScore') or 0)
    price = profile.get('price') or {}
    price_known = bool(price.get('known'))
    blended = price.get('blendedUsdPerMTok')
    blended = float(blended) if price_known and blended is not None else float('inf')
    is_parent = profile.get('modelId') == parent_model

    metrics = ('coding', 'intelligence') if role in {
        'coder', 'reviewer', 'critic', 'worker',
    } else ('agentic', 'intelligence') if role == 'browser' else ('intelligence',)
    evidence = profile.get('aaScores') or {}
    scores = [evidence.get(metric) for metric in metrics]
    measured = evidence.get('match') == 'exact' and all(
        isinstance(score, (int, float)) and not isinstance(score, bool)
        and math.isfinite(score) for score in scores)
    # Compare the original indices lexicographically, never mix incompatible
    # scales or invent an AA-to-tier threshold. Price breaks quality ties.
    measured_key = tuple(-float(score) for score in scores) if measured else (0,) * len(metrics)
    return (not measured, measured_key, -quality,
            0 if price_known else 1, blended, not is_parent,
            profile.get('providerId', ''), profile.get('modelId', ''))


def select_model_for_tier(tier: str, *, parent_model: str = '',
                          role: str = '', provider_id: str = '',
                          providers: list | None = None,
                          owner_user_id: int | None = None,
                          tenant_id: str | None = None,
                          repository=None) -> str:
    """Select a configured model; return parent/empty when evidence is weak.

    Provider pins are a hard isolation boundary: when ``provider_id`` is set,
    only that provider's entries are considered. Explicit per-agent model
    overrides are handled by the caller before this function is reached.
    """
    if tier not in _TIER_TARGET:
        return parent_model or ''
    # Delegation is not consent to change the selected model. Inheritance
    # also works for newly configured models without benchmark coverage;
    # execution still enforces owner/provider eligibility independently.
    if parent_model:
        logger.debug('[ModelProfile] tier=%s role=%s inheriting parent=%s',
                     tier, role or '-', parent_model)
        return parent_model
    profiles = configured_model_profiles(
        providers=providers,
        provider_id=provider_id,
        owner_user_id=owner_user_id,
        tenant_id=tenant_id,
        repository=repository,
    )
    target = _TIER_TARGET[tier]
    eligible = [
        p for p in profiles
        if p.get('autoSelectable')
        and int(p.get('qualityScore') or 0) >= target
        and (not role or role in set(p.get('roles') or ()))
    ]
    if not eligible:
        logger.debug('[ModelProfile] no proven candidate tier=%s role=%s '
                     'provider=%s; keeping parent=%s',
                     tier, role or '-', provider_id or '-', parent_model or '-')
        return parent_model or ''

    chosen = min(eligible, key=lambda p: _candidate_key(
        p, tier=tier, role=role, parent_model=parent_model))
    resolved = str(chosen.get('modelId') or parent_model or '')
    logger.info('[ModelProfile] tier=%s role=%s parent=%s provider=%s '
                '-> %s (quality=%s evidence=%s confidence=%.2f price=%s)',
                tier, role or '-', parent_model or '-', provider_id or '-',
                resolved, chosen.get('quality'), chosen.get('evidence'),
                float(chosen.get('confidence') or 0),
                (chosen.get('price') or {}).get('blendedUsdPerMTok'))
    return resolved


__all__ = ['select_model_for_tier']
