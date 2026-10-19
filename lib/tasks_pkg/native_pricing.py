"""Owner/route-bound production price resolution for native paid settlement.

Entry NativeRoutePricing: caller supplies principal, explicit resolved margin,
and optionally owner-aware lookup/rate/clock ports. Existing pricing resolution
owns issued flat offering rates and catalog tier/peak policy; Rust owns amount/
credit arithmetic. No price table, wallet, provider request, thread, cache,
storage or refresh is created.
"""
import math
import time

from lib.agent_core.native_model_control import ModelPricing, NativePricingError
from lib.identity import PrincipalContext


def _lookup(principal, model, provider, *, at, prompt_tokens, actual_pricing=None):
    principal.require_owner(context='native price lookup')
    from lib.pricing import declared_pricing, lookup_pricing
    if actual_pricing is not None:
        return declared_pricing(actual_pricing)
    return lookup_pricing(model, provider, at=at, prompt_tokens=prompt_tokens)


def _rates(principal):
    principal.require_owner(context='native price exchange rate')
    from lib.pricing import get_pricing_data, DEFAULT_USD_CNY_RATE
    return get_pricing_data().get('usdToCny') or DEFAULT_USD_CNY_RATE


def _numeric(value):
    if isinstance(value,bool): raise NativePricingError('native price numeric value invalid')
    return float(value)


class NativeRoutePricing:
    __slots__=('principal','margin','lookup','rates','clock')

    def __init__(self, *, principal: PrincipalContext, margin: float,
                 lookup=None, rates=None, clock=None):
        principal.require_owner(context='native price resolver')
        if type(margin) not in (int,float) or not math.isfinite(margin) or margin<0:
            raise ValueError('native pricing requires an explicit resolved margin')
        self.principal,self.margin=principal,margin
        self.lookup=_lookup if lookup is None else lookup
        self.rates=_rates if rates is None else rates
        self.clock=time.time if clock is None else clock
        if not all(callable(port) for port in (self.lookup,self.rates,self.clock)):
            raise TypeError('native price resolver ports must be callable')

    def validate_binding(self, principal, metadata):
        owner=self.principal.require_owner(context='native priced settlement')
        if (principal.require_owner(context='native priced request')!=owner
                or (principal.tenant_id or '')!=(self.principal.tenant_id or '')
                or type(metadata.get('owner_user_id')) is not int or metadata['owner_user_id']!=owner
                or (metadata.get('tenant_id') or '')!=(self.principal.tenant_id or '')
                or not isinstance(metadata.get('logical_model'),str) or not metadata['logical_model']
                or not isinstance(metadata.get('provider_id'),str) or not metadata['provider_id']):
            raise NativePricingError('native price route binding differs')
        if self.lookup is _lookup and metadata.get('actual_pricing') is not None:
            # The issued flat rate must be representable before any paid I/O.
            # Explicit lookup ports retain their existing owner-aware policy.
            from lib.pricing import declared_pricing
            try:
                declared_pricing(metadata['actual_pricing'])
            except (ValueError, OverflowError) as exc:
                raise NativePricingError('native route price is not representable') from exc

    def __call__(self, result, metadata):
        self.validate_binding(result.request.principal,metadata)
        if result.unsent: raise NativePricingError('native price requires sent usage')
        from lib.cost import observed_usage_totals, split_input_tokens
        usage=result.usage
        if not observed_usage_totals(usage)[1]:
            return None
        total=split_input_tokens(usage)[1]
        at=self.clock()
        if type(at) not in (int,float) or not math.isfinite(at):
            raise NativePricingError('native pricing clock invalid')
        options = {'actual_pricing': metadata.get('actual_pricing')} if self.lookup is _lookup else {}
        row=self.lookup(self.principal,metadata['logical_model'],metadata['provider_id'],at=at,prompt_tokens=total,**options)
        if (not isinstance(row,dict) or any(key not in row or row[key] is None for key in ('input','output'))
                or row.get('_pricingSource') in {'default_estimate','qwen_default_estimate'}):
            # The legacy display estimate is not evidence of a model price.
            raise NativePricingError('native model price unavailable')
        return ModelPricing.resolved(dict(prompt_tokens=total,
            input=_numeric(row['input']),output=_numeric(row['output']),
            cache_write_multiplier=_numeric(row.get('cacheWriteMul',1.25)),
            cache_read_multiplier=_numeric(row.get('cacheReadMul',.10)),
            usd_to_cny=_numeric(self.rates(self.principal)),currency=str(row.get('currency') or 'USD').upper(),
            margin=self.margin))
