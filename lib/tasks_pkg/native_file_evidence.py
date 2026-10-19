"""Pure binding checks shared by native file recovery and actor preflight.

Entry: file_recovery_batch. Checkpoint metadata selects exact owned producer
coordinates; domain inspection separately proves publication. No read grants a
lease, budget charge, effect replay or successor execution authority.
"""
from functools import lru_cache


@lru_cache(maxsize=1)
def _validator():
    from jsonschema import Draft202012Validator
    from lib.agent_core.native_content_contract import FILE_TOOL_RECOVERY_SCHEMA
    return Draft202012Validator(FILE_TOOL_RECOVERY_SCHEMA)


def pending_tool_calls(state):
    """Read the exact paid batch without granting any replay capability."""
    messages = state.get('messages')
    calls = messages[-1].get('tool_calls') if (isinstance(messages, list) and messages
        and isinstance(messages[-1], dict)) else None
    if (not isinstance(calls, list) or not calls or any(
            not isinstance(call, dict) or not isinstance(call.get('id'), str) or not call['id']
            or not isinstance(call.get('function'), dict)
            or not isinstance(call['function'].get('name'), str)
            or not call['function']['name'] for call in calls)
            or len({call['id'] for call in calls}) != len(calls)):
        raise ValueError('native tool recovery requires a complete uniquely identified batch')
    return calls


def file_recovery_calls(state):
    calls = pending_tool_calls(state)
    if any(call['function']['name'] != 'write_file' for call in calls):
        raise ValueError('native file recovery supports complete write_file batches only')
    return calls


def file_recovery_occurrences(state, references):
    """Use each producer tool counter; legacy coordinates must still match exactly."""
    occurrences = []
    for item, reference in zip(state['tool_recovery']['calls'], references):
        number = item.get('tool_round', state.get('next_round'))
        if type(number) is not int or not 1 <= number <= 18446744073709551615:
            raise ValueError('native file recovery tool round is invalid')
        occurrences.append((item['call_id'], reference['operationId'], number))
    return occurrences


def file_recovery_batch(state, *, owner, conversation_id, producer_task_id, turn_id=None):
    from lib.conversation_sync.validation import decode
    from lib.conversation_sync.turn_images import turn_image_owner_scope
    calls = file_recovery_calls(state)
    metadata = state.get('tool_recovery')
    _validator().validate(metadata)
    if (metadata['producer_task_id'] != producer_task_id
            or [row['call_id'] for row in metadata['calls']] != [call['id'] for call in calls]):
        raise ValueError('native file recovery producer or batch changed')
    references = []
    for item in metadata['calls']:
        reference = decode('ToolEffectInspectionReference' if turn_id is not None
            else 'InvocationToolEffectInspectionReference', item['inspection'])
        if (reference['conversationId'] != conversation_id
                or reference['ownerScope'] != turn_image_owner_scope(owner, conversation_id)
                or (reference['turnId'] != turn_id if turn_id is not None
                    else reference['invocationId'] != producer_task_id)):
            raise ValueError('native file recovery reference belongs to another owner or producer')
        references.append(reference)
    if len({row['operationId'] for row in references}) != len(references):
        raise ValueError('native file recovery repeats an operation identity')
    return calls, references


def verify_recovered_actor_checkpoint(budget, state, *, owner, task, counter_policy):
    """Read the exact acknowledged account, allowing its proved pending batch."""
    account = state['shared_model_account']
    if state.get('tools_pending') is True and state.get('tool_recovery'):
        producer = state['tool_recovery'].get('producer_task_id')
        calls, _ = file_recovery_batch(state, owner=owner,
            conversation_id=task.get('convId'), producer_task_id=producer)
        budget.verify_recovered_actor_pending_tools(account, owner=owner, task=task,
            source_task_id=budget.recovered_actor_source(task, owner=owner)['task_id'],
            count=len(calls), counter_policy=counter_policy)
    else:
        budget.verify_recovered_actor_account(account, owner=owner, task=task,
            counter_policy=counter_policy)
