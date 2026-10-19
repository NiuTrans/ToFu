"""Frozen Rust fee handoff through existing task-result and wallet authorities.

Entry project/freeze/decode/recover: terminal results or atomic native retirement own
one bounded capsule; the existing reserve sweeper consumes it with wallet/owner
binding. No model, pricing lookup, executor, new ledger or storage is created.
"""
from lib.billing.cost import CostBreakdown, native_task_cost
from lib.agent_core.native_content_contract import FEE_CHECKPOINT_SCHEMA

# Shared wire authority; the checkpoint embeds the same terminal capsule shape.
NATIVE_BILLING_SETTLEMENT_SCHEMA = FEE_CHECKPOINT_SCHEMA['properties']['wallet_settlement']
FORMAT = NATIVE_BILLING_SETTLEMENT_SCHEMA['properties']['format']['enum'][0]
_COMPONENT_TO_CREDIT = {'input': 'input', 'output': 'output', 'cache_read': 'cache_read',
    'cache_write': 'cache_write', 'baseMicro': 'base', 'marginMicro': 'margin', 'totalMicro': 'total'}
_COMPONENTS = tuple(_COMPONENT_TO_CREDIT)


def decode(capsule, *, task_id, owner_user_id, wallet_user_id) -> CostBreakdown:
    from jsonschema import Draft202012Validator
    if not Draft202012Validator(NATIVE_BILLING_SETTLEMENT_SCHEMA).is_valid(capsule):
        raise ValueError('native billing settlement capsule invalid')
    if (type(owner_user_id) is not int or owner_user_id < 1
            or capsule['task_id'] != task_id or capsule['owner_user_id'] != owner_user_id
            or capsule['wallet_user_id'] != wallet_user_id):
        raise ValueError('native billing settlement ownership differs')
    values = capsule['componentsMicro']
    if (any(type(capsule[key]) is not int for key in ('owner_user_id', 'reserved_micro', 'roundCount'))
            or any(type(value) is not int for value in values.values())):
        raise ValueError('native billing settlement requires exact integers')
    if (values['baseMicro'] != sum(values[key] for key in _COMPONENTS[:4])
            or values['totalMicro'] != values['baseMicro'] + values['marginMicro']):
        raise ValueError('native billing settlement arithmetic invalid')
    snapshot = dict(source='native_resolved_price', roundCount=capsule['roundCount'],
                    componentsMicro={key: values[key] for key in _COMPONENTS})
    return CostBreakdown(values['totalMicro'], values['baseMicro'], values['marginMicro'],
        capsule['model'], {key: values[key] for key in _COMPONENTS[:4]}, snapshot)


def freeze(task):
    if '_nativeBillingSettlement' in task:
        capsule = task['_nativeBillingSettlement']
        decode(capsule, task_id=task['id'], owner_user_id=task['_userId'], wallet_user_id=task['_billingUserId'])
        if capsule['reserved_micro'] != task.get('_billing_reservation_micro', 0):
            raise ValueError('native frozen reservation changed')
        from copy import deepcopy
        return deepcopy(capsule)
    capsule = project(task)
    from copy import deepcopy
    task['_nativeBillingSettlement'] = deepcopy(capsule)
    return capsule


def terminal_metadata(task):
    """Persist a failed outcome while unknown financial usage retains its hold.

    This is no settlement receipt. Cold recovery still requires the existing
    exact native invoice and refuses the native-required marker alone. Known
    usage, missing accounts and invalid quotes retain ordinary freeze validation.
    """
    budget = task.get('_executionBudget')
    failed = task.get('status') in {'error', 'aborted', 'interrupted'} or (
        task.get('status') == 'done' and task.get('finish_reason') == 'aborted')
    if failed and budget is not None and budget.consumption_snapshot()[2] is False:
        if '_nativeBillingSettlement' in task:
            raise ValueError('native frozen settlement changed to unknown financial usage')
        return dict(nativePricingRequired=True, usageComplete=False)
    return dict(nativePricingRequired=True, nativeBillingSettlement=freeze(task))


def project(task):
    """Describe current settled fees without freezing a running task's invoice."""
    rows = task.get('apiRounds') or []
    model = task.get('model') or (task.get('config') or {}).get('model') or (rows[0].get('model') if rows else '') or ''
    cost = native_task_cost(task, model=model)
    capsule = dict(format=FORMAT, task_id=task['id'], owner_user_id=task['_userId'],
        wallet_user_id=task['_billingUserId'], model=cost.matched_model,
        reserved_micro=task.get('_billing_reservation_micro', 0),
        componentsMicro=cost.snapshot['componentsMicro'], roundCount=cost.snapshot['roundCount'])
    decode(capsule, task_id=task['id'], owner_user_id=task['_userId'], wallet_user_id=task['_billingUserId'])
    return capsule


def _retired_invoice(client, *, owner, task_id):
    """Selected native retirement evidence; never acquire or recover a lease."""
    available = getattr(client, 'native_checkpoint_available', None)
    if not callable(available) or available(user_id=owner) is not True:
        return None
    import time
    from tofu_db.generated_checkpoint_control import FORMAT, INVOICE_HEADER_BYTES, RESPONSE_HEADROOM_BYTES
    response = client.native_checkpoint_control('invoice', dict(format=FORMAT,
        invoice=dict(owner_user_id=owner, task_id=task_id, header_bytes=INVOICE_HEADER_BYTES)),
        user_id=owner, deadline_unix_ms=time.time_ns() // 1000000 + 2000,
        response_bytes=INVOICE_HEADER_BYTES + RESPONSE_HEADROOM_BYTES)
    if response.get('accepted') is not True:
        raise ValueError('native billing retirement evidence unavailable')
    state = response.get('state')
    if state is None:
        return None
    if (not isinstance(state, dict) or set(state) != {'phase', 'wallet_settlement'}
            or state['phase'] != 'terminal' or not isinstance(state['wallet_settlement'], dict)):
        raise ValueError('native billing journal has no retired settlement')
    return state['wallet_settlement']


def recover(*, wallet_user_id, task_id, held_micro):
    """Return False only for legacy/unavailable rows; invalid native debt raises."""
    from lib.billing.users import get_user
    from lib.storage import get_storage_client
    user = get_user(wallet_user_id)
    if user is None:
        return False
    owner = user.owner_user_id
    client = get_storage_client()
    row = client.query('task_results.replay_get',
        {'key': task_id, 'user_id': owner, 'include_metadata': True}, deadline=2.0)
    if row and (not isinstance(row, dict) or row.get('task_id') != task_id or row.get('user_id') != owner):
        raise ValueError('native billing result ownership differs')
    meta = (row or {}).get('metadata') or {}
    capsule = meta.get('nativeBillingSettlement')
    from_journal = False
    if capsule is None:
        capsule = _retired_invoice(client, owner=owner, task_id=task_id)
        from_journal = capsule is not None
    if capsule is None:
        from lib.billing.native_admission import is_protected
        if meta.get('nativePricingRequired') is True or is_protected(
                client, wallet_user_id=wallet_user_id, task_id=task_id):
            raise ValueError('native billing has no committed terminal settlement')
        return False
    if not from_journal and row.get('status') not in {'done', 'error', 'aborted', 'interrupted'}:
        raise ValueError('native billing result is not owned terminal evidence')
    decode(capsule, task_id=task_id, owner_user_id=owner, wallet_user_id=wallet_user_id)
    if capsule['reserved_micro'] != held_micro:
        raise ValueError('native billing reservation differs from durable hold')
    from lib.billing.request_flow import settle_task
    task = dict(id=task_id, _userId=owner, _billing_reservation_micro=held_micro,
                _nativeBillingSettlement=capsule)
    result = settle_task(task, user_id=wallet_user_id, model=capsule['model'], raise_on_error=True)
    if result is None:
        raise ValueError('native billing recovery was not acknowledged')
    return True



def restore_accepted_invoice_prefix(task, *, lineage, model):
    """Read one accepted source invoice, then restore only its historical spend."""
    from lib.conversation_sync.dispatch_contract import AcceptedResumeLineage
    from lib.agent_core.execution_budget import ExecutionBudget
    from lib.storage import get_storage_client
    if (not isinstance(lineage, AcceptedResumeLineage)
            or lineage.task_id != task.get('id') or lineage.owner_user_id != task.get('_userId')
            or lineage.conversation_id != task.get('convId')):
        raise ValueError('native invoice source requires accepted owned lineage')
    prefix = task.get('_checkpointApiRounds')
    if not isinstance(prefix, list) or not prefix or task.get('apiRounds') != prefix:
        raise ValueError('native invoice source requires an exact pristine prefix')
    native_task_cost(dict(task, _executionBudget=None, _nativePricingRequired=False,
        _nativeFrozenCostProjection=True), model=model)
    source = get_storage_client().query('task_results.replay_get',
        {'key': lineage.source_task_id, 'user_id': lineage.owner_user_id, 'include_metadata': True}, deadline=2.0)
    if (not isinstance(source, dict) or source.get('task_id') != lineage.source_task_id
            or source.get('user_id') != lineage.owner_user_id
            or source.get('conv_id') != lineage.conversation_id
            or source.get('status') not in {'done', 'error', 'aborted', 'interrupted'}):
        raise ValueError('native invoice source is not an owned terminal result')
    metadata = source.get('metadata') or {}
    if not isinstance(metadata, dict):
        raise ValueError('native invoice source metadata invalid')
    if metadata.get('nativePricingRequired') is not True or metadata.get('usageComplete') is not True:
        raise ValueError('native invoice source has unknown financial usage')
    stored_rows = metadata.get('apiRounds')
    if not isinstance(stored_rows, list):
        raise ValueError('native invoice source round evidence missing')
    # Borrow retained quotes; copy only a legacy row that lacks root provenance.
    original = [dict(row, taskId=lineage.source_task_id)
        if isinstance(row, dict) and not row.get('taskId') and not row.get('billableTaskId')
        else row for row in stored_rows]
    if original != prefix:
        raise ValueError('native invoice prefix differs from the durable source')
    capsule = metadata.get('nativeBillingSettlement')
    cost = decode(capsule, task_id=lineage.source_task_id, owner_user_id=lineage.owner_user_id,
        wallet_user_id=task.get('_billingUserId'))
    fresh = [row for row in original if row.get('taskId') == lineage.source_task_id
        or row.get('billableTaskId') == lineage.source_task_id]
    expected_components = {component: sum(row['nativeCredits'][credit] for row in fresh)
        for component, credit in _COMPONENT_TO_CREDIT.items()}
    if (capsule['roundCount'] != len(fresh)
            or cost.micro != expected_components['totalMicro']
            or capsule['componentsMicro'] != expected_components):
        raise ValueError('native source settlement differs from its executor quotes')
    root_origins = {row['taskId'] for row in original if not row.get('billableTaskId')}
    identities = set(); total_tokens = 0; root_tokens = 0
    for row in original:
        quoted = row['nativePrice']['prompt_tokens'] + (row['cost']['outputTokens'] if row['cost'] else 0)
        total_tokens += quoted
        if not row.get('billableTaskId'):
            root_tokens += quoted
            continue
        identity = (row['taskId'], row.get('sourceRound'))
        if (row['billableTaskId'] not in root_origins or row['taskId'] in root_origins
                or type(row.get('sourceRound')) is not int or row['sourceRound'] < 1
                or identity in identities):
            raise ValueError('native invoice child provenance invalid')
        identities.add(identity)
    account = task.get('_executionBudget')
    if not isinstance(account, ExecutionBudget):
        raise ValueError('native invoice prefix requires its owned account')
    account.restore_completed_invoice_consumption(task, root_tokens=root_tokens, total_tokens=total_tokens)
