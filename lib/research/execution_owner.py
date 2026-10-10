"""One durable Research agent executor per owner/task/frozen basis.

Initial claims precede paid dispatch and are never stolen by elapsed time or a
registry miss. recover_checkpoint transfers a proven-dead claim at a fully
accounted completed-round frontier using a versioned head and immutable history.
Inspection proves local process state but grants no continuation.
"""
from copy import deepcopy
import uuid

from lib.process_owner import process_owner, owner_state
from lib.storage.errors import StorageError
from lib.log import get_logger
from .action_observations import ActionObservations, _validate, _digest

NAMESPACE = 'research.execution_owners.v1'
HEAD_NAMESPACE = 'research.execution_owner_heads.v1'
HISTORY_NAMESPACE = 'research.execution_owner_history.v1'


class ExecutionClaim:
    def __init__(self, repository, value):
        self.repository = repository
        self.value = deepcopy(value)

    def assert_current(self):
        stored, _version = self.repository._load_current()
        if (stored != self.value or (self.value['process'] is not None
                                    and process_owner() != self.value['process'])):
            raise StorageError('database_conflict', 'Research executor no longer owns its claim')


class ActionExecutionOwners(ActionObservations):
    def claim_initial(self):
        if self.load_basis() is None:
            raise StorageError('database_integrity', 'Research executor requires its frozen basis')
        value = {'schema_version': 1, 'binding': deepcopy(self.binding),
                 'nonce': uuid.uuid4().hex, 'process': process_owner(), 'replay_allowed': False}
        self._put(NAMESPACE, self.prefix, value, 'executionOwner')
        try:
            from .action_pending import retain_pending
            retain_pending(self.client, self.binding)
        except Exception as exc:
            get_logger(__name__).warning('Research pending lookup retention failed: %s', exc)
        return ExecutionClaim(self, value)

    def inspect_owner(self):
        value, _version = self._load_current()
        if value is None:
            return {'found': False, 'process_state': 'unknown', 'replay_allowed': False}
        return {'found': True, 'process_state': owner_state(value['process']),
                'replay_allowed': False}

    def _load_current(self):
        """Versioned recovery head; all original admission journals stay immutable."""
        initial = self._get(NAMESPACE, self.prefix, 'executionOwner')
        row = self.client.query('record.get', {'namespace': HEAD_NAMESPACE, 'key': self.prefix})
        if row is None:
            return initial, 0
        if initial is None or not isinstance(row, dict) or type(row.get('version')) is not int or row['version'] < 1:
            raise StorageError('database_integrity', 'Research recovery head is invalid')
        value = row.get('value')
        try:
            _validate(value, 'executionOwner')
        except ValueError as exc:
            raise StorageError('database_integrity', 'Research recovery owner is invalid') from exc
        recovery = value.get('recovery')
        if (value['binding'] != self.binding or recovery is None
                or recovery['generation'] != row['version']
                or recovery['previous_nonce'] == value['nonce'] or value['process'] is None):
            raise StorageError('database_integrity', 'Research recovery identity is invalid')
        prior = self._get(HISTORY_NAMESPACE, self.prefix + recovery['previous_nonce'], 'executionOwner')
        if (prior is None or prior['nonce'] != recovery['previous_nonce']
                or prior.get('recovery', {}).get('generation', 0) + 1 != recovery['generation']
                or (recovery['generation'] == 1 and prior != initial)):
            raise StorageError('database_integrity', 'Research recovery predecessor is invalid')
        return value, row['version']

    def recover_checkpoint(self, ordinal, *, tool_epoch_hash, expected_limits):
        """Transfer a proven-dead executor at a complete unpaid frontier.

        Returns the claim and exact restored material/meter/definition. This port
        neither launches a worker nor dispatches a model or replays a stored tool.
        The caller must restore these values before entering the shared chassis.
        """
        from .loop_checkpoint import LoopCheckpoints, NAMESPACE as CHECKPOINT_NAMESPACE
        from .request_context import RequestContexts
        from .model_response import ModelResponses
        from .model_accounting import ModelCalls
        from .action_terminal import NAMESPACE as TERMINAL_NAMESPACE
        from lib.agent_loop import validate_agent_loop_checkpoint
        if not isinstance(expected_limits, dict) or not expected_limits:
            raise ValueError('Research recovery requires explicit caller chassis limits')
        if type(ordinal) is not int or ordinal < 1:
            raise ValueError('Research recovery requires a positive checkpoint ordinal')
        old, version = self._load_current()
        if old is None or owner_state(old['process']) != 'dead':
            raise StorageError('database_conflict', 'Research recovery requires proven old process death')
        current_process = process_owner()
        if current_process is None:
            raise StorageError('database_conflict', 'Research recovery requires current process identity')
        if self.client.query('record.get', {'namespace': TERMINAL_NAMESPACE, 'key': self.prefix}) is not None:
            raise StorageError('database_conflict', 'Terminal Research action cannot recover')
        identity = {'user_id': self.binding['user_id'], 'task_id': self.binding['task_id']}
        checkpoints = LoopCheckpoints.open(self.client, **identity)
        calls = ModelCalls.open(self.client, **identity)
        intent = calls.load_model_intent(ordinal)
        if intent is None:
            raise StorageError('database_integrity', 'Research recovery admission is missing')
        material, meter = checkpoints.restore_checkpoint(ordinal, fallback_model=intent['requested_model'])
        validate_agent_loop_checkpoint(material['chassis'], expected_limits=expected_limits)
        if ModelResponses.open(self.client, **identity).load_response(ordinal) is None:
            raise StorageError('database_integrity', 'Research recovery requires its acknowledged response')
        usage = meter.snapshot()
        if usage['unmetered_calls'] or usage.get('budget_ignored'):
            raise StorageError('database_conflict', 'Research recovery requires complete retained usage')
        request = RequestContexts.open(self.client, **identity).load_request(ordinal)
        definition = request.get('action_definition') if request is not None else None
        if (definition is None or not tool_epoch_hash
                or definition['tool_epoch_hash'] != tool_epoch_hash):
            raise StorageError('database_conflict', 'Research recovery requires the original tool epoch')
        budget = definition['logical_budget']
        if (budget['token_budget'] != usage['agent_token_budget']
                or budget['dispatch_budget'] != usage['agent_dispatch_budget']):
            raise StorageError('database_integrity', 'Research recovery budget differs from its original policy')
        manifest = checkpoints._get(CHECKPOINT_NAMESPACE, self.prefix + str(ordinal), 'loopCheckpoint')
        # Repeat current ownership after reading potentially large checkpoint data.
        if self._load_current() != (old, version) or owner_state(old['process']) != 'dead':
            raise StorageError('database_conflict', 'Research recovery ownership changed during inspection')
        self._put(HISTORY_NAMESPACE, self.prefix + old['nonce'], old, 'executionOwner')
        value = {'schema_version': 1, 'binding': deepcopy(self.binding),
            'nonce': uuid.uuid4().hex, 'process': current_process, 'replay_allowed': False,
            'recovery': {'generation': version + 1, 'previous_nonce': old['nonce'],
                'checkpoint_ordinal': ordinal, 'material_digest': manifest['material_digest']}}
        _validate(value, 'executionOwner')
        def exact():
            return self._load_current() == (value, version + 1)
        try:
            ack = self.client.command('record.put', {'namespace': HEAD_NAMESPACE, 'key': self.prefix,
                'value': value, 'expected_version': version}, 'research-owner-recovery:' + _digest(value))
            if not isinstance(ack, dict) or ack.get('version') != version + 1:
                raise StorageError('database_internal', 'Research recovery claim was not acknowledged')
        except StorageError:
            if not exact():
                raise
        if not exact():
            raise StorageError('database_integrity', 'Research recovery claim was not exactly acknowledged')
        if self.client.query('record.get', {'namespace': TERMINAL_NAMESPACE, 'key': self.prefix}) is not None:
            raise StorageError('database_conflict', 'Research action settled during recovery')
        claim = ExecutionClaim(self, value)
        claim.assert_current()
        return claim, material, meter, deepcopy(definition)
