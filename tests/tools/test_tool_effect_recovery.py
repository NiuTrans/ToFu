"""Tool effects remain uncertain after lost replies, without inventing replay authority."""
import json
from types import SimpleNamespace

import pytest

from lib.tools.contracts import ToolContractV2, compile_execution_contract_documents
from lib.tools.effect_recovery import effect_recovery_policy
from lib.tasks_pkg.executor._execute import _execute_tool_one

pytestmark = pytest.mark.unit


def contract(**fields):
    return ToolContractV2(name='perform', parameters={'type': 'object', 'properties': {}},
                          model_description='Perform the requested operation.', **fields)


@pytest.mark.parametrize('fields,expected', [
    ({}, 'read_only'), ({'permission': 'write', 'idempotency': 'idempotent'}, 'idempotent'),
    ({'permission': 'write', 'idempotency': 'non_idempotent'}, 'reconcile'),
    ({'permission': 'external'}, 'reconcile'), ({'effect_recovery': 'reconcile'}, 'reconcile'),
])
def test_only_authoritative_contract_grants_safe_recovery(fields, expected):
    value = contract(**fields)
    assert effect_recovery_policy({'perform': value.search_document()}, 'perform') == expected
    assert 'effectRecovery' not in value.provider_schema()['function']
    assert effect_recovery_policy(compile_execution_contract_documents([value.provider_schema()]), 'perform') == 'reconcile'


@pytest.mark.parametrize('document', [None, {}, {'name': 'other', 'effectRecovery': 'read_only'},
    {'name': 'perform', 'effectRecovery': 'read_only', 'permission': 'write', 'idempotency': 'read_only'},
    {'name': 'perform', 'effectRecovery': 'idempotent', 'idempotency': 'non_idempotent'},
    {'name': 'perform', 'permission': 'read', 'idempotency': 'read_only'}])
def test_missing_or_contradictory_declarations_require_reconciliation(document):
    assert effect_recovery_policy({'perform': document}, 'perform') == 'reconcile'


@pytest.mark.parametrize('fields', [
    {'permission': 'write', 'effect_recovery': 'read_only'},
    {'effect_recovery': 'idempotent'}, {'effect_recovery': 'retry_everything'},
    {'effect_recovery': ''}, {'effect_recovery': False}])
def test_contract_rejects_unsafe_recovery_declaration(fields):
    with pytest.raises(ValueError):
        contract(**fields)


def invoke(task, row, handler):
    task.setdefault('_userId', 1)
    task['_tool_env'] = SimpleNamespace(resolve=lambda name: handler)
    return _execute_tool_one(task, {}, 'perform', 'call', {}, 1, row, {}, None, False)


def test_lost_reply_preserves_one_operation_and_nonretryable_result(monkeypatch):
    monkeypatch.setattr('lib.tasks_pkg.executor._execute._finalize_tool_round', lambda *a, **kw: None)
    task, row, effects = {'id': 'effect-test'}, {}, []
    def handler(*args):
        effects.append(row['executionEffect']['operationId'])
        raise TimeoutError('reply lost after remote commit')
    result = invoke(task, row, handler)
    error = json.loads(result[1])['error']
    assert effects == [row['executionEffect']['operationId']]
    assert row['executionEffect']['state'] == 'unknown'
    assert row['executionEffect']['scope'] == 'process'
    assert error['code'] == 'tool_effect_unknown' and error['retryable'] is False
    assert effects[0] in error['message']


def test_read_failure_does_not_claim_unknown_write(monkeypatch):
    monkeypatch.setattr('lib.tasks_pkg.executor._execute._finalize_tool_round', lambda *a, **kw: None)
    task = {'id': 'read-effect', '_toolContractDocumentsByName': {'perform': contract().search_document()}}
    def handler(*args):
        raise TimeoutError('read unavailable')
    row = {}
    result = invoke(task, row, handler)
    assert row['executionEffect']['recovery'] == 'read_only'
    assert 'tool_effect_unknown' not in result[1]


def test_returned_background_receipt_is_not_completion_and_calls_have_distinct_identity():
    task = {'id': 'background-effect'}
    first, second = {}, {}
    def handler(*args):
        return 'call', '{"status":"running","handle":"process-1"}', False
    assert invoke(task, first, handler)[1] == '{"status":"running","handle":"process-1"}'
    invoke(task, second, handler)
    assert first['executionEffect']['state'] == 'returned'
    assert first['executionEffect']['operationId'] != second['executionEffect']['operationId']


def test_base_exception_preserves_uncertainty_and_propagates():
    task, row = {'id': 'cancelled-effect'}, {}
    class Interrupted(BaseException):
        pass
    def handler(*args):
        raise Interrupted()
    with pytest.raises(Interrupted):
        invoke(task, row, handler)
    assert row['executionEffect']['state'] == 'unknown'
    assert row['_runtimeState'] == 'error'


def test_prepared_effects_bind_distinct_occurrences_and_require_commit_before_entry():
    from lib.tasks_pkg.executor._effects import PreparedToolEffects, begin_tool_effect, ToolEffectDurabilityError
    task = {'id': 'prepared', '_userId': 1}
    first, second = {}, {}
    batch = PreparedToolEffects(task, [('perform', 'same-provider-id', 1, first),
                                      ('perform', 'same-provider-id', 2, second)])
    identities = batch.identities()
    assert len({row[3] for row in identities}) == 2
    assert [row[2] for row in identities] == [1, 2]
    batch.attach()
    with pytest.raises(ToolEffectDurabilityError):
        begin_tool_effect(task, 'perform', 'same-provider-id', 1, first)
    assert not first
    batch.arm()
    with pytest.raises(ToolEffectDurabilityError):
        begin_tool_effect(task, 'perform', 'same-provider-id', 1, dict(first))
    assert begin_tool_effect(task, 'perform', 'same-provider-id', 1, first)['operationId'] == identities[0][3]
    with pytest.raises(ToolEffectDurabilityError):
        begin_tool_effect(task, 'perform', 'same-provider-id', 1, first)
    task['_userId'] = 2
    with pytest.raises(ToolEffectDurabilityError):
        begin_tool_effect(task, 'perform', 'same-provider-id', 2, second)
    task['_userId'] = 1
    assert begin_tool_effect(task, 'perform', 'same-provider-id', 2, second)['operationId'] == identities[1][3]
    batch.close()
    assert not batch.rows and '_preparedToolEffects' not in task
