"""Exact native actor fees carried by the existing private completion receipt.

Entry capture/decode: export only an acknowledged immutable child-transfer grant,
then validate frozen quote arithmetic and original payer/producer identities.
No pricing lookup, wallet settlement, executor admission or persistence lives here.
The schema derives from the shared native checkpoint contract and quote shape.
"""
from copy import deepcopy
from functools import lru_cache
import math

from lib.agent_core.native_content_contract import REGISTERED_ACTOR_FEES_SCHEMA

TOKENS = ('inputTokens', 'outputTokens', 'totalInputTokens', 'cacheWriteTokens', 'cacheReadTokens', 'thinkingTokens')
MONEY = ('costUsd', 'costCny', 'inputCostUsd', 'outputCostUsd', 'cacheWriteCostUsd', 'cacheReadCostUsd',
         'inputCostCny', 'outputCostCny', 'cacheWriteCostCny', 'cacheReadCostCny', 'cacheSavingsUsd', 'cacheSavingsCny')
FORMAT = REGISTERED_ACTOR_FEES_SCHEMA['properties']['format']['const']


def validate_quote(quote):
    """Validate the original native quote, without recomputing a price."""
    from lib.agent_core.native_model_control import ModelPricing
    if not isinstance(quote, dict) or set(quote) != {'amounts', 'price', 'credits'}:
        raise ValueError('native fee projection shape differs')
    price = ModelPricing.resolved(quote['price']).values()
    amounts, credits = quote['amounts'], quote['credits']
    if (not isinstance(credits, dict) or set(credits) != {'input', 'output', 'cache_read', 'cache_write', 'base', 'margin', 'total'}
            or any(type(value) is not int or not 0 <= value <= 18446744073709551615 for value in credits.values())
            or credits['base'] != sum(credits[key] for key in ('input', 'output', 'cache_read', 'cache_write'))
            or credits['total'] != credits['base'] + credits['margin']):
        raise ValueError('native credit projection invalid')
    if amounts is not None:
        if (not isinstance(amounts, dict) or set(amounts) != set(TOKENS + MONEY)
                or any(type(amounts[key]) is not int or amounts[key] < 0 for key in TOKENS)
                or any(type(amounts[key]) not in (int, float) or not math.isfinite(amounts[key]) or amounts[key] < 0 for key in MONEY)
                or amounts['totalInputTokens'] != price['prompt_tokens']):
            raise ValueError('native amount projection invalid')
    elif any(credits.values()) or price['prompt_tokens'] != 0:
        raise ValueError('empty native amounts cannot carry a charge')
    return price, amounts, credits


def quote_from_api_round(row):
    """One lossless quote projection for native checkpoints and private receipts."""
    amounts = row['cost']
    return dict(amounts=None if amounts is None else {key: amounts[key] for key in TOKENS + MONEY},
                price=row['nativePrice'], credits=row['nativeCredits'])


@lru_cache(maxsize=1)
def _validator():
    from jsonschema import Draft202012Validator
    return Draft202012Validator(REGISTERED_ACTOR_FEES_SCHEMA)


def decode(evidence, *, owner_user_id, root_task_id, actor_task_id):
    from lib.billing.cost import native_task_cost
    try:
        _validator().validate(evidence)
        if (type(owner_user_id) is not int or type(evidence['owner_user_id']) is not int
                or evidence['owner_user_id'] != owner_user_id
                or evidence['root_task_id'] != root_task_id or evidence['actor_task_id'] != actor_task_id
                or root_task_id == actor_task_id or type(evidence['tokens']) is not int):
            raise ValueError('Native actor fee ownership differs')
        tokens, current_seen = 0, False
        for ordinal, row in enumerate(evidence['rounds'], 1):
            producer, payer = row['producer_task_id'], row['payer_task_id']
            if (type(row['round']) is not int or row['round'] != ordinal
                    or producer == root_task_id or payer in {producer, actor_task_id}):
                raise ValueError('Native actor fee origins differ')
            if producer == actor_task_id:
                current_seen = True
                if payer != root_task_id: raise ValueError('Fresh actor fee payer differs')
            elif current_seen or payer == root_task_id:
                raise ValueError('Historical actor fees must retain their preceding original payer')
            quote = row['quote']
            _, amounts, _ = validate_quote(quote)
            public = dict(round=row['round'], taskId=producer, nativePrice=quote['price'],
                          nativeCredits=quote['credits'], cost=None if amounts is None else
                          dict(amounts, pricingSource='native_resolved_price'))
            native_task_cost(dict(id=producer, apiRounds=[public], _nativeFrozenCostProjection=True),
                             model=row['logical_model'])
            tokens += quote['price']['prompt_tokens'] + (amounts['outputTokens'] if amounts else 0)
        if tokens != evidence['tokens']:
            raise ValueError('Native actor quotes do not cover the witnessed consumption')
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError('Native actor fee evidence is invalid') from exc
    return deepcopy(evidence)


def capture(child):
    from lib.billing.cost import NativeChildFeeGrant
    grant = child.get('_nativeChildFeeGrant')
    if child.get('_nativePricingRequired') is not True and grant is None:
        return None  # Legacy/unpriced is absent evidence, never a zero-fee proof.
    if child.get('_nativePricingRequired') is not True or not isinstance(grant, NativeChildFeeGrant):
        raise ValueError('Native actor fees require acknowledged original transfer evidence')
    rows = grant.source_rows(child)
    evidence = dict(format=FORMAT, owner_user_id=grant.owner, root_task_id=grant.parent_id,
                    actor_task_id=grant.child_id, tokens=grant.tokens, rounds=[
        dict(round=row['round'], producer_task_id=row['taskId'],
             payer_task_id=row.get('billableTaskId', grant.parent_id), logical_model=row['model'],
             provider_id=row['provider_id'], quote=quote_from_api_round(row)) for row in rows])
    return decode(evidence, owner_user_id=grant.owner, root_task_id=grant.parent_id, actor_task_id=grant.child_id)


@lru_cache(maxsize=1)
def _checkpoint_validators():
    from jsonschema import Draft202012Validator
    from lib.agent_core.native_content_contract import FEE_CHECKPOINT_SCHEMA, SHARED_ACCOUNT_SCHEMA
    return tuple(Draft202012Validator(schema) for schema in (FEE_CHECKPOINT_SCHEMA, SHARED_ACCOUNT_SCHEMA))


def from_checkpoint(state, *, owner_user_id, root_task_id, actor_task_id, tenant_id):
    """Read original actor quotes without creating a cost policy or live account.

    This is the same private fee vocabulary as completed actors. The caller must
    separately prove journal ownership, writer death and the whole account cut.
    """
    from lib.cost import observed_usage_totals
    try:
        projection, account = state['host_fee_projection'], state['shared_model_account']
        fee_validator, account_validator = _checkpoint_validators()
        fee_validator.validate(projection); account_validator.validate(account)
        if (type(owner_user_id) is not int or type(state['owner_user_id']) is not int
                or state['owner_user_id'] != owner_user_id or state['task_id'] != actor_task_id
                or projection['owner_user_id'] != owner_user_id or projection['task_id'] != actor_task_id
                or projection['tenant_id'] != tenant_id or projection['wallet_user_id']
                or projection['reserved_micro'] or projection.get('child_rounds')
                or 'wallet_settlement' in projection or 'root_tokens' in account
                or account['owner_user_id'] != owner_user_id or account['root_task_id'] != root_task_id
                or account.get('actor_task_id') != actor_task_id or account['complete'] is not True
                or type(state['next_round']) is not int
                or state['next_round'] != len(projection['rounds'])
                or not isinstance(state['usage'], list) or len(state['usage']) != state['next_round']):
            raise ValueError('Native actor checkpoint fee binding differs')
        rounds = []
        for saved, usage in zip(projection['rounds'], state['usage']):
            if 'public_round' in saved or 'billable_task_id' not in saved:
                raise ValueError('Native actor checkpoint lacks original fee origins')
            price, amounts, _ = validate_quote(saved['cost'])
            actual, complete = observed_usage_totals(usage)
            if complete is not True or actual != price['prompt_tokens'] + (amounts['outputTokens'] if amounts else 0):
                raise ValueError('Native actor quote does not cover checkpoint usage')
            route = saved.get('route', projection)
            rounds.append(dict(round=saved['round'], producer_task_id=saved.get('task_id', actor_task_id),
                payer_task_id=saved['billable_task_id'], logical_model=route['logical_model'],
                provider_id=route['provider_id'], quote=saved['cost']))
        return decode(dict(format=FORMAT, owner_user_id=owner_user_id, root_task_id=root_task_id,
            actor_task_id=actor_task_id, tokens=account['tokens'], rounds=rounds),
            owner_user_id=owner_user_id, root_task_id=root_task_id, actor_task_id=actor_task_id)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError('Native actor checkpoint fee evidence is invalid') from exc
