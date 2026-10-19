"""Registered child completion receipts for the Flow journal handoff.

The existing task-result CAS owns one immutable completion per child. The Flow
journal owns admission and replay; a receipt cannot launch or retry an effect.
Dependencies: registered invocation ownership, selected storage, journal codec.
"""
from copy import deepcopy
import json
import uuid

from lib.orchestration.execution_journal import RecoveryRefused, decode_agent_result
from lib.tasks_pkg.invocation_registration import INVOCATION_COMPLETION_FIELD


FORMAT = 'tofu.flow.invocation-completion/v1'
FIELD = INVOCATION_COMPLETION_FIELD


def completion_binding(event):
    """Validate a closed witness without interpreting a locator as authority."""
    if 'invocation_completion' not in event:
        return None
    binding = event['invocation_completion']
    identity = binding.get('identity') if isinstance(binding, dict) else None
    if (not isinstance(binding, dict) or set(binding) not in (
            {'format', 'nonce', 'identity'}, {'format', 'nonce', 'identity', 'account_root_task_id'})
            or binding['format'] != FORMAT or not isinstance(binding['nonce'], str)
            or len(binding['nonce']) != 32 or any(c not in '0123456789abcdef' for c in binding['nonce'])
            or not isinstance(identity, dict) or set(identity) != {
                'user_id', 'conversation_id', 'parent_task_id', 'child_task_id', 'archive_scope'}
            or type(identity['user_id']) is not int or identity['user_id'] < 1
            or any(not isinstance(identity[key], str) or not identity[key] or len(identity[key]) > 512
                   for key in ('conversation_id', 'parent_task_id', 'child_task_id'))
            or event.get('kind') != 'agent'
            or identity['child_task_id'] != event.get('invocation_id')
            or identity['archive_scope'] != f"invocation:{identity['user_id']}:{identity['child_task_id']}"):
        raise RecoveryRefused('Invalid registered invocation completion binding')
    if 'account_root_task_id' in binding and (
            not isinstance(binding['account_root_task_id'], str)
            or not 0 < len(binding['account_root_task_id']) <= 512):
        raise RecoveryRefused('Invalid invocation account root binding')
    return deepcopy(binding)


def _record(client, identity):
    from lib.tasks_pkg.invocation_registration import owned_child_record
    record = owned_child_record(client, user_id=identity['user_id'],
        conversation_id=identity['conversation_id'], invocation_id=identity['child_task_id'])
    if record['value']['child_invocation'] != identity:
        raise RecoveryRefused('Invocation completion birth lineage changed')
    return record


class RegisteredInvocationCompletion:
    """Process-local writer bound by the ordinary registered child adapter."""

    def __init__(self, child):
        self._child = child
        self._admission = None
        self._preparation = None
        self._account_snapshot = None

    def _candidate(self, event, *, nonce=None):
        child = self._child
        if child.get('id') != event.get('invocation_id'):
            raise RecoveryRefused('Invocation completion admission changed')
        from lib.agent_core.execution_budget import ExecutionBudget
        budget = child.get('_executionBudget')
        if not isinstance(budget, ExecutionBudget) or not budget.authorizes_model_owner(child, child.get('_userId')):
            raise RecoveryRefused('Registered Flow child has no owned account')
        binding = {'format': FORMAT, 'nonce': nonce or uuid.uuid4().hex,
            'account_root_task_id': budget.root_id, 'identity': {
            'user_id': child.get('_userId'), 'conversation_id': child.get('convId'),
            'parent_task_id': child.get('_parentTaskId'), 'child_task_id': child.get('id'),
            'archive_scope': child.get('_archiveScope')}}
        candidate = {**deepcopy(event), 'invocation_completion': binding}
        completion_binding(candidate)
        return candidate

    def prepare(self, event):
        """Reserve correlation before handoff; no execution or settlement authority."""
        if (self._preparation is not None or self._admission is not None
                or event.get('type') != 'execution_effect_prepared'):
            raise RecoveryRefused('Invocation completion preparation changed')
        self._preparation = self._candidate(event)
        return deepcopy(self._preparation['invocation_completion'])

    def bind(self, event):
        if self._admission is not None:
            raise RecoveryRefused('Invocation completion admission changed')
        prepared = self._preparation
        candidate = self._candidate(event, nonce=prepared['invocation_completion']['nonce'] if prepared else None)
        if prepared is not None and candidate != {**prepared, 'type': 'execution_effect_started'}:
            raise RecoveryRefused('Invocation completion admission differs from its preparation')
        self._admission = candidate
        self._preparation = None
        return deepcopy(candidate['invocation_completion'])

    def capture_account(self):
        if self._admission is None:
            raise RecoveryRefused('Execution account has no child admission')
        if self._account_snapshot is None:
            from lib.agent_core.execution_budget import ExecutionBudget
            budget = self._child.get('_executionBudget')
            if not isinstance(budget, ExecutionBudget):
                raise RecoveryRefused('Registered Flow child has no shared execution account')
            self._account_snapshot = budget.capture_replay_snapshot(
                self._child, owner=self._admission['invocation_completion']['identity']['user_id'])
            if self._account_snapshot['root_task_id'] != self._admission['invocation_completion']['account_root_task_id']:
                raise RecoveryRefused('Flow execution account changed after admission')
        return deepcopy(self._account_snapshot)

    def settle(self, receipt):
        """ACK the frozen full result before Flow publishes its completion event."""
        from lib.storage import get_storage_client
        from lib.tasks_pkg.persistence_wait import wait_for_persistence
        from lib.task_replay.task_result_checkpoint_contract import TASK_RESULT_CHECKPOINT_GUARD_CONTRACT
        admission, child = self._admission, self._child
        if admission is None or child.get('_invocationEffectLock') is None:
            raise RecoveryRefused('Invocation completion has no registered admission')
        expected = {key: admission[key] for key in (
            'kind', 'input_hash', 'invocation_id', 'invocation_completion')}
        if any(receipt.get(key) != value for key, value in expected.items()):
            raise RecoveryRefused('Invocation completion changed its admission')
        if receipt.get('execution_account') != self.capture_account():
            raise RecoveryRefused('Invocation completion changed its execution account')
        decode_agent_result(receipt['result'])
        candidate = json.loads(json.dumps({'admission': admission, 'receipt': receipt},
            ensure_ascii=False, allow_nan=False, separators=(',', ':')))
        from lib.billing.native_actor_fees import capture
        fees = capture(child)
        if fees is not None:
            candidate['native_fees'] = fees
        _verified_completion(candidate, admission)
        with child['_invocationEffectLock']:
            client = get_storage_client(write=True)
            record = wait_for_persistence(child,
                lambda: _record(client, admission['invocation_completion']['identity']),
                label='Flow child completion lookup', cancellable=False)
            value = deepcopy(record['value'])
            if FIELD in value:
                if value[FIELD] != candidate:
                    raise RecoveryRefused('Invocation completion is immutable')
                return
            value[FIELD] = candidate
            payload = dict(key=child['id'], value=value, expected_version=record['version'],
                guard_contract=TASK_RESULT_CHECKPOINT_GUARD_CONTRACT, require_parent=True)
            ack = wait_for_persistence(child, lambda: client.command(
                'task_results.checkpoint', payload, None),
                label='Flow child completion', cancellable=False)
            if (not isinstance(ack, dict) or ack.get('owned') is not True
                    or ack.get('guard_contract') != TASK_RESULT_CHECKPOINT_GUARD_CONTRACT
                    or type(ack.get('version')) is not int or ack['version'] < record['version']):
                raise RecoveryRefused('Invocation completion was not durably acknowledged')


def _verified_completion(completion, admission):
    """Validate one private completion without adding pricing to its public result."""
    if (not isinstance(completion, dict) or set(completion) not in (
            {'admission', 'receipt'}, {'admission', 'receipt', 'native_fees'})
            or completion.get('admission') != admission):
        raise RecoveryRefused('A model/tool invocation was interrupted; its external effects are unknown (no matching completion)')
    receipt = completion['receipt']
    expected = {key: admission[key] for key in ('kind', 'input_hash', 'invocation_id', 'invocation_completion')}
    if (not isinstance(receipt, dict)
            or set(receipt) not in ({*expected, 'result'}, {*expected, 'result', 'execution_account'})
            or any(receipt.get(key) != value for key, value in expected.items())):
        raise RecoveryRefused('Invocation completion does not match its admission')
    from lib.orchestration.recovery_account import receipt_account
    account = receipt_account(receipt)
    decode_agent_result(receipt['result'])
    fees = None
    if 'native_fees' in completion:
        from lib.billing.native_actor_fees import decode
        binding = completion_binding(admission)
        if account is None or binding.get('account_root_task_id') != account['root_task_id']:
            raise RecoveryRefused('Native actor fees have no exact shared account')
        fees = decode(completion['native_fees'], owner_user_id=account['owner_user_id'],
                      root_task_id=account['root_task_id'], actor_task_id=admission['invocation_id'])
        if fees['tokens'] > account['tokens']:
            raise RecoveryRefused('Native actor fees exceed their shared-account observation')
    return receipt, fees


def read_registered_actor_fees(storage, admission, *, owner_user_id, conversation_id, parent_task_id,
                               expected_receipt=None):
    """Read exact private fees and registration version; absence is not zero debt.

    This is evidence for aggregate recovery. It cannot retire a source, admit a
    new executor or settle a wallet without the owning atomic lineage boundary.
    """
    from lib.identity import require_user_id
    owner = require_user_id(owner_user_id, context='registered actor fee evidence')
    binding = completion_binding(admission)
    if (binding is None or binding['identity']['user_id'] != owner
            or binding['identity']['conversation_id'] != conversation_id
            or binding['identity']['parent_task_id'] != parent_task_id):
        raise RecoveryRefused('Native actor fees do not belong to this launch')
    try:
        record = _record(storage, binding['identity'])
        if type(record.get('version')) is not int or record['version'] < 1:
            raise RecoveryRefused('Native actor fee registration has no persisted version')
        receipt, fees = _verified_completion(record['value'].get(FIELD), admission)
        if expected_receipt is not None and receipt != expected_receipt:
            raise RecoveryRefused('Native actor fee completion differs from the Goal receipt')
        return None if fees is None else dict(registration_version=record['version'], evidence=deepcopy(fees))
    except RecoveryRefused:
        raise
    except Exception as exc:
        raise RecoveryRefused('Native actor fee evidence could not be verified') from exc


def completion_resolver(owner_user_id, *, conversation_id=None, parent_task_id=None,
                        allow_missing=False, storage=None):
    """Read an owned completion, optionally distinguishing absence from corruption.

    allow_missing serves read-only pending inspection. Birth/owner failures and
    present-but-invalid completion evidence always refuse, including null values.
    """
    from lib.identity import require_user_id
    owner = require_user_id(owner_user_id, context='Flow invocation recovery')

    def resolve(admission):
        from lib.storage import get_storage_client
        binding = completion_binding(admission)
        if binding is None or binding['identity']['user_id'] != owner:
            raise RecoveryRefused('Invocation completion has no owned binding')
        if ((conversation_id is not None and binding['identity']['conversation_id'] != conversation_id)
                or (parent_task_id is not None and binding['identity']['parent_task_id'] != parent_task_id)):
            raise RecoveryRefused('Invocation completion does not belong to this launch')
        try:
            record = _record(storage if storage is not None else get_storage_client(), binding['identity'])
            if allow_missing and FIELD not in record['value']:
                return None
            receipt, _ = _verified_completion(record['value'].get(FIELD), admission)
            return deepcopy(receipt)
        except RecoveryRefused:
            raise
        except Exception as exc:
            raise RecoveryRefused('Invocation completion could not be verified') from exc
    return resolve
