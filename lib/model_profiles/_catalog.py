"""Configured-provider projection for model profiles."""

from __future__ import annotations

from lib.log import get_logger
from lib.model_profiles._profile import build_model_profile

logger = get_logger(__name__)


def _configured_providers(providers: list | None) -> list:
    if providers is not None:
        return providers
    try:
        from lib import _load_server_config
        cfg = _load_server_config()
        return list((cfg or {}).get('providers') or [])
    except Exception as exc:
        logger.warning('[ModelProfile] configured provider load failed: %s', exc)
        return []


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
            blended = (float(inp) + float(out)) / 2.0
            if str(currency).upper() == 'CNY':
                from lib.pricing import DEFAULT_USD_CNY_RATE
                blended /= DEFAULT_USD_CNY_RATE
            return {
                'known': True,
                'blendedUsdPerMTok': round(blended, 9),
                'source': str(info.get('_pricingSource') or 'model_entry'),
            }
    except (TypeError, ValueError) as exc:
        logger.debug('[ModelProfile] invalid price for %s/%s: %s',
                     provider_id, model_id, exc)
    fallback = model_entry.get('cost')
    try:
        if fallback is not None:
            return {
                'known': True,
                'blendedUsdPerMTok': round(float(fallback) * 1000.0, 9),
                'source': 'catalog_blended_estimate',
            }
    except (TypeError, ValueError) as exc:
        logger.debug('[ModelProfile] invalid catalog cost for %s/%s: %s',
                     provider_id, model_id, exc)
    return {'known': False, 'blendedUsdPerMTok': None, 'source': 'unknown'}


def configured_model_profiles(*, providers: list | None = None,
                              provider_id: str = '') -> list[dict]:
    """Return enabled chat-model profiles without collapsing providers."""
    out = []
    for provider in _configured_providers(providers):
        if not isinstance(provider, dict) or provider.get('enabled', True) is False:
            continue
        pid = str(provider.get('id') or provider.get('brand') or '')
        if provider_id and pid != provider_id:
            continue
        for entry in provider.get('models') or ():
            if not isinstance(entry, dict) or entry.get('enabled', True) is False:
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
            profile.update({
                'capabilities': sorted(caps),
                'price': _price_snapshot(model_id, pid, entry),
                'enabled': True,
            })
            out.append(profile)
    return out


__all__ = ['configured_model_profiles']
