"""The agent inspection tool is fresh, read-only, scoped and actually callable."""
import json
from types import SimpleNamespace

import pytest

from lib.tools.tool_effect_inspection import TOOL_EFFECT_INSPECTION
from lib.tasks_pkg.handlers.tool_effect_inspection import inspect_for_task, handle_tool_effect_inspection
from lib.tools.registry import all_specs, ToolContext
from lib.tools.contracts import ToolContractError

pytestmark = pytest.mark.unit


def test_registry_contract_is_read_only_uncached_and_recovery_callable():
    spec = next(s for s in all_specs() if s.key == 'tool_effect_inspection')
    assert spec.discovery_policy == 'eager'
    assert spec.provides == {'inspect_tool_effect'} and not spec.write_tools
    assert spec.cacheable_tools == frozenset()
    assert TOOL_EFFECT_INSPECTION.effect_recovery == 'read_only'
    assert TOOL_EFFECT_INSPECTION.permission == 'read'
    from lib.tools.registry._build import _build_tool_effect_inspection
    ctx = SimpleNamespace(durable_state_available=True, conv_id='c')
    assert _build_tool_effect_inspection(ctx)[0]['function']['name'] == 'inspect_tool_effect'
    ctx.durable_state_available = False
    assert _build_tool_effect_inspection(ctx) == []
    ctx.durable_state_available = True; ctx.conv_id = ''
    assert _build_tool_effect_inspection(ctx) == []


@pytest.mark.parametrize('arguments', [{}, {'operation_id': 'bad'}, {'operation_id': 'g' * 32}, {'operation_id': 'a' * 32 + '\n'},
    {'operation_id': 'a' * 32, 'conversation_id': 'foreign'},
    {'operation_id': 'a' * 32, 'turn_id': ''},
    {'operation_id': 'a' * 32, 'invocation_id': ''},
    {'operation_id': 'a' * 32, 'invocation_id': 'child', 'turn_id': 'turn'}])
def test_invalid_arguments_never_query_storage(monkeypatch, arguments):
    monkeypatch.setattr('lib.conversation_sync.service.ConversationSyncService.inspect_tool_effect',
                        lambda *a, **kw: pytest.fail('Invalid input reached storage'))
    with pytest.raises(ToolContractError):
        inspect_for_task({'_userId': 7, 'convId': 'c', '_turnId': 't'}, arguments)


def test_each_read_uses_explicit_task_owner_and_conversation(monkeypatch):
    queries = []
    def inspect(self, conv, turn, owner, *, operation_id):
        queries.append((conv, turn, owner, operation_id))
        return {'outcome': 'unknown' if len(queries) == 1 else 'file_published'}
    monkeypatch.setattr('lib.conversation_sync.service.ConversationSyncService.inspect_tool_effect', inspect)
    task = {'_userId': 7, 'convId': 'c', '_turnId': 'current'}
    args = {'operation_id': 'a' * 32, 'turn_id': 'producer'}
    assert inspect_for_task(task, args)['outcome'] == 'unknown'
    assert inspect_for_task(task, args)['outcome'] == 'file_published'
    assert queries == [('c', 'producer', 7, 'a' * 32)] * 2


def test_handler_settles_unknown_as_a_read_result_and_failure_as_error(monkeypatch):
    settled = []
    monkeypatch.setattr('lib.tasks_pkg.handlers.tool_effect_inspection._finalize_tool_round',
                        lambda *a, **kw: settled.append(kw['status']))
    monkeypatch.setattr('lib.tasks_pkg.handlers.tool_effect_inspection.inspect_for_task',
                        lambda *a: {'outcome': 'unknown', 'reason': 'missing_evidence'})
    result = handle_tool_effect_inspection({'id': 'task'}, {}, 'inspect_tool_effect', 'call', {}, 1, {}, {}, None, False)
    assert json.loads(result[1])['outcome'] == 'unknown' and settled == ['done']
    def fail(*a):
        raise ToolContractError('effect_unavailable', 'missing producer')
    monkeypatch.setattr('lib.tasks_pkg.handlers.tool_effect_inspection.inspect_for_task', fail)
    result = handle_tool_effect_inspection({'id': 'task'}, {}, 'inspect_tool_effect', 'call', {}, 2, {}, {}, None, False)
    assert json.loads(result[1])['error']['code'] == 'effect_unavailable'
    assert settled == ['done', 'error']


def test_shared_contract_pattern_uses_json_schema_search_semantics():
    from lib.tools.contracts import ToolContractV2
    contract = ToolContractV2(name='pattern_probe', model_description='Test', parameters={
        'type': 'object', 'properties': {'value': {'type': 'string', 'pattern': '[0-9]+'}}})
    assert contract.validate_arguments({'value': 'prefix 12 suffix'})['value'] == 'prefix 12 suffix'
    with pytest.raises(ToolContractError, match='pattern'):
        contract.validate_arguments({'value': 'no number'})
