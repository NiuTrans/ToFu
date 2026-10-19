"""Actual native gateway shares tool parsing, approval, handler and settlement."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace

import pytest

from tests.memory.test_working_facts import facts as facts, artifact, storage, turn_service, query, owner, PRINCIPAL
from tests.tasks_pkg.test_native_gateway import task as gateway_task
from lib.agent_core.native_process import ApprovalRequest, ToolRequest
from lib.tasks_pkg.native_gateway import NativeGateway
from lib.tools.working_facts import WORKING_FACT_CONTRACTS
from lib.tools.registry import all_specs

pytestmark = pytest.mark.unit


def test_native_gateway_records_reads_and_refuses_overwriting_owner_decision(facts, monkeypatch):
    task = gateway_task() | {'convId': facts['conv_id'], '_principalContext': PRINCIPAL.to_payload()}
    definitions = [contract.provider_schema() for contract in WORKING_FACT_CONTRACTS]
    spec = next(spec for spec in all_specs() if spec.key == 'working_facts')
    task['_tool_env'] = SimpleNamespace(resolve=lambda name: spec.handler if name in spec.provides else None,
        write_names=spec.write_tools, idempotent_names=spec.cacheable_tools, confirmation_names=set())
    task['_executable_tool_catalog'] = definitions
    args = {'revision': 0, 'text': '# Report 1', 'kind': 'observation', 'sources': [
        {'kind': 'artifact', 'id': facts['id'], 'version': facts['content_sha256'], 'quote': '# Report 1'}]}
    async def run(executor):
        gateway = NativeGateway(principal=PRINCIPAL, task=task, executor=executor,
            configuration={'autoApply': True}, tool_list=definitions,
            permissions={'read_working_facts': 'read', 'record_working_fact': 'write'},
            project_path=None, project_enabled=False, model='fixture', round_source=lambda: 0,
            decision_policy=None, pending_calls=4, pending_bytes=65536)
        async def write(arguments, identity):
            request = ApprovalRequest(PRINCIPAL, task['id'], identity + '-approval', identity,
                'record_working_fact', 'epoch', json.dumps(arguments).encode(), b'null', asyncio.Event(), 2)
            assert await gateway.decide(request) == 'auto_write_mode'
            return await gateway.execute(ToolRequest(PRINCIPAL, task['id'], identity + '-execute',
                'record_working_fact', arguments, identity, model_round=0))
        try:
            missing = json.loads(await write(args | {'id': 'invented-new-id'}, 'invented'))['error']
            assert missing['code'] == 'working_fact_not_found'
            assert missing['retryable'] is False and 'omit id' in missing['next_action']
            assert query(facts)['revision'] == 0 and not query(facts)['facts']
            paraphrased = json.loads(await write(args | {'text': 'The file states Report 1'}, 'paraphrase'))['error']
            assert paraphrased['code'] == 'working_facts_invalid_request'
            assert paraphrased['retryable'] is False
            assert query(facts)['revision'] == 0 and not query(facts)['facts']
            saved = json.loads(await write(args, 'save'))
            fact = saved['facts'][0]
            assert fact['producerTaskId'] == task['id'] and fact['status'] == 'observed'
            result = await gateway.execute(ToolRequest(PRINCIPAL, task['id'], 'read',
                'read_working_facts', {'ids': [fact['id']]}, model_round=0))
            assert json.loads(result)['facts'][0]['id'] == fact['id']
            owner(facts, fact, 'correct', text='The owner correction remains authoritative')
            refreshed = await gateway.execute(ToolRequest(PRINCIPAL, task['id'], 'read-again',
                'read_working_facts', {'ids': [fact['id']]}, model_round=1))
            assert json.loads(refreshed)['facts'][0]['text'] == 'The owner correction remains authoritative'
            assert json.loads(refreshed)['facts'][0]['revision'] == fact['revision'] + 1
            refused = await write(args | {'revision': query(facts)['revision'], 'id': fact['id']}, 'overwrite')
            assert json.loads(refused)['error']['code'] == 'working_facts_forbidden'
            assert 'cannot overwrite' in refused
            assert query(facts)['facts'][0]['text'] == 'The owner correction remains authoritative'
            assert [row['status'] for row in task['toolRounds']] == ['error', 'error', 'done', 'done', 'done', 'error']
            from lib.memory.working_facts import service
            from lib.storage import StorageError
            for index, failure in enumerate([StorageError('database_unavailable', 'Injected unavailable store'),
                                             ValueError('Working facts commit was not acknowledged')]):
                def unavailable_write(*a, **k):
                    raise failure
                with monkeypatch.context() as patch:
                    patch.setattr(service, 'record_fact', unavailable_write)
                    uncertain = json.loads(await write(args, f'uncertain-{index}'))['error']
                assert uncertain['code'] == 'working_facts_unavailable'
                assert uncertain['retryable'] is False
                assert query(facts)['facts'][0]['text'] == 'The owner correction remains authoritative'
            def unavailable_read(*a, **k):
                raise StorageError('database_unavailable', 'Injected unavailable store')
            with monkeypatch.context() as patch:
                patch.setattr(service, 'query_facts', unavailable_read)
                failed = json.loads(await gateway.execute(ToolRequest(PRINCIPAL, task['id'], 'unavailable-read',
                    'read_working_facts', {}, model_round=2)))['error']
            assert failed['code'] == 'working_facts_unavailable' and failed['retryable'] is True
            assert [row['status'] for row in task['toolRounds'][-3:]] == ['error'] * 3

        finally:
            gateway.close()
    with ThreadPoolExecutor(max_workers=1) as executor:
        asyncio.run(run(executor))
