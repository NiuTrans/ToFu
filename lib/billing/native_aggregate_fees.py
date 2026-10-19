"""Project exact aggregate actor debt from the shared account and frozen quotes.

Entry project: join overlapping original producer rounds once, prove whole-cut
coverage, and describe only the requested root's invoice. This pure calculation
neither freezes debt nor authorizes settlement, account restoration or takeover.
Checkpoint quotes, wallet capsules and the cost-only aggregate schema own shapes.
"""
from lib.agent_core.account_frontier import validate_frontier
from lib.billing.native_actor_fees import decode as decode_actor
from lib.billing.native_settlement import FORMAT, _COMPONENT_TO_CREDIT, decode as decode_invoice


def project(frontier, actor_evidence, *, owner_user_id, root_task_id, wallet_user_id,
            reserved_micro, model):
    cut = validate_frontier(frontier); account = cut['snapshot']
    if (type(owner_user_id) is not int or account['owner_user_id'] != owner_user_id
            or account['root_task_id'] != root_task_id or not account['settled']
            or not account['usage_known'] or cut['model_requests']
            or cut.get('recovery', {}).get('pending_actors')):
        raise ValueError('Aggregate fees require an exact settled account cut')
    if account['root_tokens']:
        raise ValueError('Aggregate actor fees cannot cover root-owned consumption')
    actors, rounds, producers = {}, {}, {}
    for evidence in actor_evidence:
        if not isinstance(evidence, dict) or not {'root_task_id', 'actor_task_id'} <= evidence.keys():
            raise ValueError('Aggregate actor fee identity is missing')
        fees = decode_actor(evidence, owner_user_id=owner_user_id,
            root_task_id=evidence['root_task_id'], actor_task_id=evidence['actor_task_id'])
        actor = fees['actor_task_id']
        if actor in actors:
            raise ValueError('Aggregate actor fee evidence repeats an actor')
        actors[actor] = fees
        for row in fees['rounds']:
            # The payer is part of the immutable value, not of the lookup key:
            # changing a payer must conflict instead of minting a second debt.
            producer, payer = row['producer_task_id'], row['payer_task_id']
            identity = (producer, row['round'])
            if producer in producers and producers[producer] != payer:
                raise ValueError('Aggregate original producer has conflicting payers')
            producers[producer] = payer
            if identity in rounds and rounds[identity] != row:
                raise ValueError('Aggregate original quote changed across actor prefixes')
            rounds[identity] = row
    for actor, counters in cut['actors'].items():
        if actor == root_task_id:
            continue
        fees = actors.get(actor)
        if (fees is None or fees['root_task_id'] != root_task_id
                or counters['parent_task_id'] != root_task_id or fees['tokens'] != counters['tokens']):
            raise ValueError('Aggregate fees lack an exact active actor')
    tokens = sum(row['quote']['price']['prompt_tokens'] + (
        row['quote']['amounts']['outputTokens'] if row['quote']['amounts'] else 0) for row in rounds.values())
    if tokens != account['tokens']:
        raise ValueError('Aggregate original quotes do not cover whole-account consumption')
    owned = [row for row in rounds.values() if row['payer_task_id'] == root_task_id]
    components = {component: sum(row['quote']['credits'][credit] for row in owned)
                  for component, credit in _COMPONENT_TO_CREDIT.items()}
    # The existing wallet stores signed 64-bit credits, even when individual
    # native price/usage transport fields permit unsigned 64-bit integers.
    if any(value > 9223372036854775807 for value in components.values()):
        raise ValueError('Aggregate native fees exceed storage integer range')
    if wallet_user_id is None and type(reserved_micro) is int and reserved_micro == 0:
        from jsonschema import Draft202012Validator
        from tofu_db.generated_checkpoint_control import AGGREGATE_COST_SCHEMA
        cost = dict(format=AGGREGATE_COST_SCHEMA['$id'], task_id=root_task_id,
            owner_user_id=owner_user_id, model=model, componentsMicro=components, roundCount=len(owned))
        Draft202012Validator(AGGREGATE_COST_SCHEMA).validate(cost)
        return cost
    capsule = dict(format=FORMAT, task_id=root_task_id, owner_user_id=owner_user_id,
        wallet_user_id=wallet_user_id, model=model, reserved_micro=reserved_micro,
        componentsMicro=components, roundCount=len(owned))
    decode_invoice(capsule, task_id=root_task_id, owner_user_id=owner_user_id, wallet_user_id=wallet_user_id)
    return capsule
