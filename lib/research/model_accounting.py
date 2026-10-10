"""Research logical model-call admission and acknowledged usage checkpoints.

ModelCalls binds to the observation journal's owner/task/basis and selected
record authority. Intents precede dispatch; cumulative usage follows shared
meter accounting and precedes tools. Reads never authorize replay or reset a
budget. Provider-internal attempts and terminal task state remain separate.
"""
from copy import deepcopy
import uuid

from lib.storage.errors import StorageError
from .action_observations import (ActionObservations, _validate, _record_value,
                                  _task_prefix, _validate_page_parameters)

INTENT_NAMESPACE = 'research.model_call_intents.v1'
USAGE_NAMESPACE = 'research.model_call_usage.v1'


class ModelCalls(ActionObservations):
    def load_model_intent(self, ordinal):
        if type(ordinal) is not int or ordinal < 1:
            raise ValueError('Research model dispatch ordinal must be positive')
        value = self._get(INTENT_NAMESPACE, self.prefix + str(ordinal), 'modelIntent')
        if value is not None and value['ordinal'] != ordinal:
            raise StorageError('database_integrity', 'Research model dispatch identity is invalid')
        return value

    def load_model_usage(self, ordinal):
        intent = self.load_model_intent(ordinal)
        value = self._get(USAGE_NAMESPACE, self.prefix + str(ordinal), 'modelUsage')
        if value is None:
            return None
        if (intent is None or value['ordinal'] != ordinal
                or value['admission_id'] != intent['admission_id']
                or value['usage_snapshot']['agent_dispatches'] != ordinal
                or value['usage_snapshot']['calls'] != ordinal):
            raise StorageError('database_integrity', 'Research model usage identity is invalid')
        return deepcopy(value)

    def admit_model_call(self, *, ordinal, round_index, requested_model, request_digest, max_output, usage_before, request_snapshot_version=None, response_snapshot_version=None, loop_checkpoint_version=None):
        value = {'schema_version': 1, 'binding': deepcopy(self.binding), 'ordinal': ordinal,
            'round_index': round_index, 'requested_model': requested_model,
            'request_digest': request_digest, 'max_output': max_output,
            'admission_id': uuid.uuid4().hex, 'usage_before': deepcopy(usage_before)}
        if request_snapshot_version is not None:
            value['request_snapshot_version'] = request_snapshot_version
        if response_snapshot_version is not None:
            value['response_snapshot_version'] = response_snapshot_version
        if loop_checkpoint_version is not None:
            value['loop_checkpoint_version'] = loop_checkpoint_version
        _validate(value, 'modelIntent')
        if self.load_basis() is None:
            raise ValueError('Research model dispatch requires an acknowledged frozen basis')
        if self.load_model_intent(ordinal) is not None:
            raise StorageError('database_conflict', 'Research model slot was already admitted; replay is refused')
        if usage_before['agent_dispatches'] != ordinal or usage_before['calls'] != ordinal - 1:
            raise ValueError('Research model admission counters do not match this dispatch')
        previous = self.load_model_usage(ordinal - 1) if ordinal > 1 else None
        if ordinal > 1 and previous is None:
            raise StorageError('database_conflict', 'Previous Research model usage is unresolved')
        if previous is not None:
            prior = previous['usage_snapshot']
            admission_changes = {'agent_dispatches', 'forced_final', 'forced_final_reason'}
            if (set(usage_before) != set(prior) or any(usage_before[key] != prior[key]
                    for key in prior if key not in admission_changes)):
                raise ValueError('Research model admission cannot reset accounted usage or budgets')
        if ordinal > 1 and self.load_model_intent(ordinal - 1).get('loop_checkpoint_version') == 1:
            from .loop_checkpoint import LoopCheckpoints
            checkpoints = LoopCheckpoints.open(self.client, user_id=self.binding['user_id'], task_id=self.binding['task_id'])
            if checkpoints is None or checkpoints.load_checkpoint(ordinal - 1) is None:
                raise StorageError('database_conflict', 'Previous Research completed-round checkpoint is unresolved')
        if request_snapshot_version == 1:
            from .request_context import NAMESPACE as REQUEST_NAMESPACE
            header = self._get(REQUEST_NAMESPACE, self.prefix + request_digest, 'requestContext')
            if header is None:
                raise StorageError('database_integrity', 'Research model request was not acknowledged')
            definition = header.get('action_definition')
            if ordinal > 1:
                first = self.load_model_intent(1)
                original = self._get(REQUEST_NAMESPACE, self.prefix + first['request_digest'], 'requestContext')
                if original is None or original.get('action_definition') != definition:
                    raise StorageError('database_conflict', 'Research model admission changed its original action definition')
            if definition is not None:
                budget = definition['logical_budget']
                if (budget['token_budget'] != usage_before['agent_token_budget']
                        or budget['dispatch_budget'] != usage_before['agent_dispatch_budget']
                        or (definition['model_override'] and definition['model_override'] != requested_model)):
                    raise StorageError('database_integrity', 'Research model admission differs from its original action policy')
        self._put(INTENT_NAMESPACE, self.prefix + str(ordinal), value, 'modelIntent')
        return deepcopy(value)

    def record_model_usage(self, intent, snapshot, *, usage_mapping_received, response_digest=None):
        ordinal = intent['ordinal']
        if self.load_model_intent(ordinal) != intent:
            raise StorageError('database_integrity', 'Research model usage does not match its admission')
        value = {'schema_version': 1, 'binding': deepcopy(self.binding), 'ordinal': ordinal,
            'admission_id': intent['admission_id'], 'usage_mapping_received': usage_mapping_received,
            'usage_snapshot': deepcopy(snapshot)}
        if response_digest is not None:
            from .model_response import NAMESPACE as RESPONSE_NAMESPACE
            response = self._get(RESPONSE_NAMESPACE, self.prefix + str(ordinal), 'modelResponse')
            if (response is None or response['ordinal'] != ordinal
                    or response['admission_id'] != intent['admission_id']
                    or response['response_digest'] != response_digest):
                raise StorageError('database_integrity', 'Research usage response is missing or mismatched')
            value['response_digest'] = response_digest
        if intent.get('response_snapshot_version') == 1 and response_digest is None:
            raise ValueError('Research model usage requires its acknowledged response')
        _validate(value, 'modelUsage')
        if snapshot['agent_dispatches'] != ordinal or snapshot['calls'] != ordinal:
            raise ValueError('Research usage snapshot has not accounted this dispatch')
        before = intent['usage_before']
        cumulative = ('agent_tokens', 'total_tokens', 'unmetered_calls', 'priced_calls',
                      'prompt_tokens', 'completion_tokens', 'cache_read_tokens',
                      'cache_write_tokens', 'reasoning_tokens', 'cost_usd', 'cost_cny')
        if any(key in before and (key not in snapshot or snapshot[key] < before[key])
               for key in cumulative):
            raise ValueError('Research cumulative usage cannot decrease')
        if any(snapshot[key] != before[key] for key in ('version', 'agent_token_budget', 'agent_dispatch_budget')):
            raise ValueError('Research model usage cannot replace its admitted budget')
        if any(before.get(key) is True and snapshot.get(key) is not True
               for key in ('cost_estimated', 'budget_ignored')):
            raise ValueError('Research usage cannot erase accounting uncertainty or budget violations')
        if not set(before.get('models', [])).issubset(snapshot.get('models', [])):
            raise ValueError('Research usage cannot erase previously accounted models')
        self._put(USAGE_NAMESPACE, self.prefix + str(ordinal), value, 'modelUsage')

    def restore_usage_meter(self, ordinal, *, fallback_model=''):
        """Restore only a complete acknowledged latest call's cumulative meter.

        An admitted successor makes this checkpoint stale, including unresolved
        paid calls. This read does not claim task/tool continuation ownership.
        """
        from lib.paper.agent_usage import PaperAgentUsageMeter
        if self.load_basis() is None:
            raise StorageError('database_integrity', 'Research model call frozen basis is missing')
        usage = self.load_model_usage(ordinal)
        if usage is None:
            raise StorageError('database_conflict', 'Research model usage is unresolved')
        if self.load_model_intent(ordinal + 1) is not None:
            raise StorageError('database_conflict', 'Research usage checkpoint has an admitted successor')
        return PaperAgentUsageMeter.from_snapshot(usage['usage_snapshot'], fallback_model=fallback_model)

    def latest_model_ordinal(self):
        """Fold admission headers only; numeric order differs from record-key order.

        Memory is one existing 16-row page plus scalar counters. Missing sequence
        entries invalidate the frontier instead of hiding a later paid admission.
        No usage/context chunks are read and no persistent index is introduced.
        """
        cursor = ''; count = 0; latest = 0
        while True:
            rows = self.client.query('record.list', {'namespace': INTENT_NAMESPACE,
                'prefix': self.prefix, 'after_key': cursor, 'limit': 16})
            if not isinstance(rows, list) or len(rows) > 16:
                raise StorageError('database_integrity', 'Research admission page is invalid')
            for row in rows:
                intent = _record_value(row, 'modelIntent'); key = row.get('key')
                if (key != self.prefix + str(intent['ordinal']) or key <= cursor
                        or intent['binding'] != self.binding):
                    raise StorageError('database_integrity', 'Research admission page boundary is invalid')
                cursor = key; count += 1; latest = max(latest, intent['ordinal'])
            if len(rows) < 16:
                break
        if count != latest:
            raise StorageError('database_integrity', 'Research admission sequence is incomplete')
        return latest

    def model_calls_page(self, *, limit=8, cursor=''):
        _validate_page_parameters(self.prefix, limit, cursor)
        rows = self.client.query('record.list', {'namespace': INTENT_NAMESPACE,
            'prefix': self.prefix, 'after_key': cursor, 'limit': limit + 1})
        if not isinstance(rows, list) or len(rows) > limit + 1:
            raise StorageError('database_integrity', 'Research model call page is invalid')
        if rows and self.load_basis() is None:
            raise StorageError('database_integrity', 'Research model call frozen basis is missing')
        calls = []; previous = cursor
        for row in rows:
            intent = _record_value(row, 'modelIntent'); key = row.get('key')
            if (key != self.prefix + str(intent['ordinal']) or key <= previous
                    or intent['binding'] != self.binding):
                raise StorageError('database_integrity', 'Research model call page boundary is invalid')
            previous = key
            usage = self.load_model_usage(intent['ordinal'])
            calls.append({'intent': deepcopy(intent),
                'state': 'usage_recorded' if usage is not None else 'unresolved_usage',
                'usage': usage, 'replay_allowed': False})
        return {'calls': calls[:limit], 'next_cursor': rows[limit - 1]['key'] if len(rows) > limit else ''}


def load_model_calls_page(task_id, *, user_id, limit=8, cursor=''):
    from lib.storage import get_storage_client
    _, prefix = _task_prefix(user_id, task_id)
    _validate_page_parameters(prefix, limit, cursor)
    repository = ModelCalls.open(get_storage_client(), user_id=user_id, task_id=task_id)
    if repository is None:
        return {'found': False, 'binding': None, 'calls': [], 'next_cursor': ''}
    return {'found': True, 'binding': deepcopy(repository.binding),
            **repository.model_calls_page(limit=limit, cursor=cursor)}
