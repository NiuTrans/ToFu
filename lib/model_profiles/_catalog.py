"""Owner-scoped model-routing projection for model profiles.

Runtime callers enumerate enabled v2 Offerings through the repository. The
``providers=`` argument remains only as a pure compatibility/test fixture; no
ambient server-config provider list is consulted.
"""

from __future__ import annotations


import math
from lib.log import get_logger
from lib.model_profiles._profile import build_model_profile

logger = get_logger(__name__)


def _routing_model_entries(
    *, owner_user_id: int, tenant_id: str | None, repository=None,
) -> list[tuple[str, dict]]:
    """Project enabled v2 supply into profile-shaped model entries.

    Request names have no independent switch or probe prerequisite.
    """

    try:
        from lib.model_routing import ModelRoutingRepository, OwnerBoundary

        active_repository = repository or ModelRoutingRepository()
        authority = active_repository.get(
            OwnerBoundary.create(owner_user_id, tenant_id))
    except Exception as exc:
        logger.warning('[ModelProfile] owner routing load failed: %s', exc)
        return []
    if authority.revision <= 0:
        return []

    document = authority.document
    providers = {
        row['provider_id']: row for row in document['providers']}
    accesses = {
        row['provider_access_id']: row
        for row in document['provider_accesses']
        if row.get('enabled') is True
    }
    models = {
        (row['creator_id'], row['model_id']): row
        for row in document['models']
    }
    live_offerings = {
        row['offering_id']
        for row in document['deployments']
    }
    projected: list[tuple[str, dict]] = []
    from lib.model_catalog.aa import cached_routing_scores

    aa_scores = cached_routing_scores(list(models.values()))
    for offering in document['offerings']:
        access = accesses.get(offering.get('provider_access_id'))
        if (
            access is None
            or offering.get('enabled') is not True
            or offering.get('stale') is True
            or offering.get('offering_id') not in live_offerings
        ):
            continue
        provider_id = str(access['provider_id'])
        if provider_id not in providers:
            continue
        model_ref = offering.get('model') or {}
        official = models.get((
            model_ref.get('creator_id'), model_ref.get('model_id')))
        model_id = str(
            (official or {}).get('model_id')
            or offering.get('pending_model_id')
            or ''
        ).strip()
        if not model_id:
            continue
        entry = {
            'model_id': model_id,
            'capabilities': list(offering.get('capabilities') or []),
            'context_window': offering.get('context_window'),
            # Supply is not a credential grant. Consumers that dispatch must
            # intersect this identity with authorized execution slots.
            'provenance': {
                'source': 'model_routing_v2',
                'ownerUserId': owner_user_id,
                'tenantId': tenant_id,
                'revision': authority.revision,
                'providerAccessId': access['provider_access_id'],
                'offeringId': offering['offering_id'],
                'creatorId': model_ref.get('creator_id'),
                'identityState': offering.get('identity_state'),
                'contextSource': 'offering',
                'credentialsVerified': False,
            },
        }
        # The v2 Model/Offering schema does not admit capability_profile.
        # Do not smuggle one through this projection; declared catalog rules
        # remain the authority. The explicit providers= fixture already feeds
        # schema-independent capability profiles to build_model_profile.
        if official:
            score_key = f"{official['creator_id']}::{official['model_id']}"
            entry['aa_scores'] = aa_scores.get(score_key, {})
        pricing = offering.get('actual_pricing')
        if isinstance(pricing, dict):
            entry['pricing'] = dict(pricing)
        projected.append((provider_id, entry))
    return projected


def _price_snapshot(model_id: str, provider_id: str,
                    model_entry: dict) -> dict:
    info = None
    embedded = model_entry.get('pricing')
    if isinstance(embedded, dict):
        info = dict(embedded)
    if info is None:
        try:
            from lib.pricing import lookup_pricing
            try:
                info = lookup_pricing(
                    model_id, provider_id, prompt_tokens=0)
            except TypeError as exc:
                logger.debug('[ModelProfile] pricing lookup uses legacy '
                             'signature for %s/%s: %s',
                             provider_id, model_id, exc)
                info = lookup_pricing(model_id, provider_id)
        except Exception as exc:
            logger.debug('[ModelProfile] price lookup failed for %s/%s: %s',
                         provider_id, model_id, exc)
    info = dict(info or {})
    selected = info.get('selectedTier')
    if isinstance(selected, dict):
        inp = selected.get('input')
        out = selected.get('output')
        currency = selected.get('currency') or info.get('currency') or 'USD'
    else:
        inp = info.get('input')
        out = info.get('output')
        currency = info.get('currency') or 'USD'
    try:
        if inp is not None and out is not None:
            if (isinstance(inp, bool) or isinstance(out, bool)
                    or any(not math.isfinite(float(value)) or float(value) < 0
                           for value in (inp, out))
                    or str(currency).upper() not in {'USD', 'CNY'}
                    or info.get('unit', 'per_million_tokens') != 'per_million_tokens'
                    or info.get('_pricingSource') in {'default_estimate', 'qwen_default_estimate'}):
                raise ValueError('unverified pricing')
            blended = float(inp) / 2.0 + float(out) / 2.0
            if str(currency).upper() == 'CNY':
                from lib.pricing import DEFAULT_USD_CNY_RATE
                rate = DEFAULT_USD_CNY_RATE
                if isinstance(rate, bool) or not math.isfinite(rate) or rate <= 0:
                    raise ValueError('unverified exchange rate')
                blended /= rate
            if not math.isfinite(blended):
                raise ValueError('nonfinite price')
            return {
                'known': True,
                'blendedUsdPerMTok': round(blended, 9),
                'source': str(info.get('_pricingSource') or 'model_entry'),
            }
    except (TypeError, ValueError, OverflowError) as exc:
        logger.debug('[ModelProfile] invalid price for %s/%s: %s',
                     provider_id, model_id, exc)
    if info:
        # An incomplete/invalid rate card cannot be repaired by a blended guess.
        return {'known': False, 'blendedUsdPerMTok': None, 'source': 'unknown'}
    fallback = model_entry.get('cost')
    try:
        if fallback is not None:
            amount = float(fallback) * 1000.0
            if isinstance(fallback, bool) or not math.isfinite(amount) or amount < 0:
                raise ValueError('unverified blended estimate')
            return {
                'known': True,
                'blendedUsdPerMTok': round(amount, 9),
                'source': 'catalog_blended_estimate',
            }
    except (TypeError, ValueError, OverflowError) as exc:
        logger.debug('[ModelProfile] invalid catalog cost for %s/%s: %s',
                     provider_id, model_id, exc)
    return {'known': False, 'blendedUsdPerMTok': None, 'source': 'unknown'}


def configured_model_profiles(
    *,
    providers: list | None = None,
    provider_id: str = '',
    owner_user_id: int | None = None,
    tenant_id: str | None = None,
    repository=None,
) -> list[dict]:
    """Return enabled chat-model profiles without crossing owner boundaries."""

    entries: list[tuple[str, dict]] = []
    if providers is not None:
        for provider in providers:
            if not isinstance(provider, dict) or provider.get('enabled', True) is False:
                continue
            pid = str(provider.get('id') or provider.get('brand') or '')
            entries.extend(
                (pid, entry)
                for entry in (provider.get('models') or ())
                if isinstance(entry, dict)
                and entry.get('enabled', True) is not False
            )
    elif owner_user_id is not None:
        entries = _routing_model_entries(
            owner_user_id=owner_user_id,
            tenant_id=tenant_id,
            repository=repository,
        )

    out = []
    for pid, entry in entries:
        if provider_id and pid != provider_id:
            continue
        model_id = str(entry.get('model_id') or '').strip()
        if not model_id:
            continue
        caps = {str(x) for x in (entry.get('capabilities') or ()) if x}
        if 'text' not in caps:
            continue
        if caps & {'embedding', 'image_gen', 'transcription', 'tts'}:
            continue
        profile = build_model_profile(
            model_id, provider_id=pid, model_entry=entry)

        # Structured ordinal roles and exact AA proxy scores are separate
        # evidence. Each consumer owns its qualification/ranking policy; the
        # projection never promotes a role profile from a benchmark score.
        profile_source = (
            'explicit_capability_profile'
            if isinstance(entry.get('capability_profile'), dict)
            else 'maintained_declared_rule'
            if profile.get('evidence') in {'vendor_declaration', 'provider_catalog'}
            else 'unqualified'
        )
        window = entry.get('context_window')
        if isinstance(window, bool) or not isinstance(window, int) or window <= 0:
            window = None
        profile.update({
            'capabilities': sorted(caps),
            'profileSource': profile_source,
            'price': _price_snapshot(model_id, pid, entry),
            'enabled': True,
            'aaScores': dict(entry.get('aa_scores') or {}),
            'contextWindow': window,
            'provenance': dict(entry.get('provenance') or {
                'source': 'fixture', 'credentialsVerified': False,
            }),
            # Preserve raw rate-card provenance. The legacy blended snapshot
            # is not a token-weighted estimate and must not drive compaction.
            'declaredPricing': dict(entry.get('pricing') or {}),
        })
        out.append(profile)
    return out


__all__ = ['configured_model_profiles']
