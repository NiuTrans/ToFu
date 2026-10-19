"""Conserved physical occurrence receipts shared by dispatch and its source.

ExecutionBudget alone creates the events. This reducer never starts an account,
creates a lease, estimates usage or refunds uncertain provider consumption.
"""
from copy import deepcopy
from lib.llm.llm_errors import AbortedError


def record_model_event(attempts, event, *, max_tokens, max_requests, live_intents=(), prior_accounts=False):
    key = (event['root_task_id'], event['attempt_ordinal'])
    old = next((row for row in attempts if (row['root_task_id'], row['attempt_ordinal']) == key), None)
    if event['phase'] == 'dispatch_intent':
        if old is not None:
            if old != event:
                raise AbortedError('Physical model occurrence is terminal or belongs to another account')
            return
        same = [row for row in attempts if row['root_task_id'] == event['root_task_id']]
        if (not prior_accounts and len(same) != len(attempts)
                or len(attempts) >= max_requests
                or sum(row['reported_tokens'] for row in attempts) + event['reserved_tokens'] > max_tokens
                or any(row['account_nonce'] != event['account_nonce'] or not row['usage_complete'] and not (
                    row['phase'] == 'dispatch_intent' and row['attempt_ordinal'] in live_intents) for row in same)):
            raise AbortedError('Model allowance exhausted or prior consumption is unconfirmed')
        attempts.append(deepcopy(event))
    elif old is not None:
        if any(old.get(key) != event.get(key) for key in (
                'account_nonce', 'root_task_id', 'attempt_ordinal', 'attempt_task_id', 'reserved_tokens')):
            raise AbortedError('Model settlement belongs to another immutable occurrence')
        if event['phase'] == 'voided' and old['phase'] == 'dispatch_intent':
            raise AbortedError('Cannot void a durable uncertain physical intent')
        if old['phase'] in ('settled', 'voided') and old != event:
            raise AbortedError('Physical model occurrence is already terminal')
        old.clear(); old.update(deepcopy(event))
    elif event['phase'] != 'voided':
        raise AbortedError('Cannot settle an unrecorded model occurrence')


def model_consumption(attempts):
    observed = sum(row['reported_tokens'] for row in attempts)
    held = sum(row['reported_tokens'] if row['phase'] in ('settled', 'voided') and row['usage_complete']
               else max(row['reserved_tokens'], row['reported_tokens']) for row in attempts)
    return observed, held, all(row['phase'] in ('settled', 'voided') and row['usage_complete'] for row in attempts)
