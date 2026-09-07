"""Conservative role/tier selection over configured model profiles."""

from __future__ import annotations

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

    # Capability is a gate, not the optimization target. Once a model clears
    # the role tier, choose the least expensive proven candidate; quality only
    # breaks equal-price ties. This prevents a routine heavy-role task from
    # paying for frontier capacity when a heavy model already satisfies it.
    return (0 if price_known else 1, blended, quality, not is_parent,
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

    # Never switch a standard task away from an explicitly chosen parent that
    # is configured and proven. The role tiers are for light/heavy delegation,
    # not for silently replacing the user's main model.
    if tier == 'standard':
        parent = next((p for p in eligible
                       if p.get('modelId') == parent_model), None)
        if parent is not None:
            return parent_model

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
