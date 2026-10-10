"""Declared-return fairness, complete real cursor reads and production SDK replay."""
import asyncio
import hashlib
import json
import socket
from pathlib import Path

import pytest

from evaluations.programmatic_pilot.declared_workloads import declared_reads_complete
from evaluations.programmatic_pilot.runner import config, paired_report, preflight, run_task
from evaluations.programmatic_pilot.workloads import canonical, digest, oracle, read_world, tool_schemas, workload_pack

pytestmark = pytest.mark.unit


def test_pack_preserves_both_existing_frozen_identities_and_controls():
    assert digest([w.__dict__ for w in workload_pack()]) == '5bc305d67c463e731d95de64febfef9f518c0e5596e51659d1d7ac033ef4668b'
    assert digest([w.__dict__ for w in workload_pack('adaptive')]) == 'e58d662b60b7187e43f4e30c5f1df460dbe7f83b7438d7446c0e5cfa381f3cd9'
    tasks = workload_pack('declared')
    assert tasks[-2:] == workload_pack()[-2:]
    assert {w.family for w in tasks} == {'declared_pagination', 'declared_composition',
                                         'semantic_judgment', 'small_result'}
    for task in tasks:
        assert oracle(task, task.expected_json)
        assert all(word not in task.instruction for word in ['execute_program', 'tools.invoke', 'tools.result', 'tools.keep'])
    left, right = config('direct', 'medium'), config('programmatic', 'medium')
    left['tools']['programmaticCalling'] = right['tools']['programmaticCalling']
    assert left == right and right['tools']['programmaticExposure'] == 'additive'


def calls_for(workload):
    calls = []
    def call(name, args):
        output = read_world(workload, name, args)
        calls.append({'name': name, 'arguments': args, 'accepted': True, 'isError': False,
                      'outputSha256': hashlib.sha256(output.encode()).hexdigest()})
        return json.loads(output)
    manifest = call('custom__current_batch', {})
    for dataset in manifest['datasets']:
        assert dataset not in workload.instruction
        offset = 0
        while offset is not None:
            page = call('custom__read_batch_page', {'dataset': dataset, 'offset': offset})
            # Direct tools must receive complete small pages too.
            assert len(canonical(page).encode()) < 2048
            offset = page['nextOffset']
    return calls


@pytest.mark.parametrize('index', [0, 1])
@pytest.mark.parametrize('missing', ['none', 'manifest', 'middle', 'terminal', 'forged', 'rejected', 'error'])
def test_coverage_requires_payload_bound_manifest_all_rows_and_terminal_cursor(index, missing):
    task = workload_pack('declared')[index]
    calls = calls_for(task)
    if missing == 'manifest': calls = calls[1:]
    elif missing == 'middle': calls.pop(2)
    elif missing == 'terminal': calls.pop()
    elif missing == 'forged': calls[-1]['outputSha256'] = '0' * 64
    elif missing == 'rejected': calls[-1]['accepted'] = False
    elif missing == 'error': calls[-1]['isError'] = True
    assert declared_reads_complete(task, calls) == (missing == 'none')


def test_preflight_binds_per_task_schemas_and_reports_reject_mixed_packs():
    tasks = workload_pack('declared')
    manifest = preflight(Path(__file__).resolve().parents[2], thinking='medium', trials=1,
                         timeout_s=180, pack='declared')
    assert manifest['toolSchemaSha256'] == digest({t.task_id: tool_schemas(t) for t in tasks})
    assert all('decoded_result_schema' in tool for task in tasks[:2] for tool in tool_schemas(task))
    rows = [{'taskId': w.task_id, 'arm': arm, 'trial': 0, 'oraclePassed': True,
             'programCount': 0, 'wallMs': 100, 'costUsd': None}
            for w in tasks for arm in ('direct', 'programmatic')]
    assert paired_report(rows, trials=1, pack='declared')['allOraclesPass']
    with pytest.raises(ValueError): paired_report(rows, trials=1, pack='adaptive')


@pytest.mark.parametrize('transport', ['sdk', 'native_api'])
def test_cli_preflight_selects_pack_without_dispatch(capsys, transport):
    from evaluations.programmatic_pilot import runner, native_api
    assert (runner.main if transport == 'sdk' else native_api.main)(['--pack', 'declared']) == 0
    assert json.loads(capsys.readouterr().out)['taskPack'] == 'declared'


@pytest.mark.parametrize('arm', ['direct', 'programmatic'])
def test_native_admission_preserves_same_decoded_return_contracts(arm):
    from evaluations.programmatic_pilot.native_api import HTTPRuntime
    class API:
        body = None
        def call(self, path, body=None):
            assert path == '/api/v4/agent/run'
            self.body = body
            return {'task_id': 'synthetic-admitted-task'}
    api = API()
    runtime = HTTPRuntime(api, {'creator_id': 'synthetic', 'model_id': 'synthetic'})
    schemas = tool_schemas(workload_pack('declared')[0])
    execution = runtime.start([], config=config(arm, 'medium'), custom_tools=schemas,
        custom_tools_mode='exclusive', trajectory='tofu-native', request_id='synthetic', timeout_s=30)
    explicit = api.body['config']['_explicitToolSchemas']
    for original, projected in zip(schemas, explicit):
        assert projected['decoded_result_schema'] == original['decoded_result_schema']
        assert projected['function'] == original['function']
        assert set(projected) == {'type', 'function', 'decoded_result_schema'}
    assert api.body['tools'] == schemas and execution.expected_catalog_count == 2
    explicit[0]['decoded_result_schema']['properties'].clear()
    assert schemas[0]['decoded_result_schema']['properties']


@pytest.mark.parametrize('index', [0, 1])
@pytest.mark.parametrize('arm', ['direct', 'programmatic'])
def test_public_sdk_uses_observed_manifest_and_cursors_in_both_arms(tmp_path, monkeypatch, index, arm):
    import lib.llm
    from lib.llm._sse_core import prepare_request
    from lib.llm.stream_result import ProviderStreamResult, ProviderStreamState
    from tests.support.standalone_model_routing import standalone_model_routing_envelope
    from tofu_agent import AgentRuntime, ModelRoutingConfig
    task = workload_pack('declared')[index]
    requests, transport_errors = [], []
    datasets, records, offset, position = [], [], 0, 0

    def stream(body, **kwargs):
        nonlocal datasets, offset, position
        plan = prepare_request(body, api_key='synthetic-test-key',
                               base_url='https://gateway.example/v1', api_protocol='openai')
        requests.append(plan.body['tools'])
        descriptions = '\n'.join(row['function']['description'] for row in requests[-1])
        assert 'nextOffset' in descriptions and 'records' in descriptions and 'datasets' in descriptions
        delivered = [m for m in body['messages'] if m.get('role') == 'tool']
        if arm == 'programmatic' and not delivered:
            name, args = 'execute_program', {'code':
                'const m=await tools.invoke("custom__current_batch",{}); let count=0,total=0; '
                'for(const dataset of m.datasets){let offset=0; do{'
                'const p=await tools.invoke("custom__read_batch_page",{dataset,offset}); '
                'for(const r of p.records){count++;total+=r.amount;} offset=p.nextOffset;'
                '}while(offset!==null);} return {count,total};'}
        elif arm == 'direct' and not delivered:
            name, args = 'custom__current_batch', {}
        elif arm == 'direct':
            output = json.loads(delivered[-1]['content'])
            if 'datasets' in output:
                datasets = output['datasets']
            else:
                records.extend(output['records'])
                offset = output['nextOffset']
                if offset is None: position, offset = position + 1, 0
            if position < len(datasets):
                name, args = 'custom__read_batch_page', {'dataset': datasets[position], 'offset': offset}
            else:
                assert oracle(task, canonical({'count': len(records), 'total': sum(r['amount'] for r in records)}))
                name, args = None, None
        else:
            assert oracle(task, delivered[-1]['content'])
            name, args = None, None
        if name is None:
            kwargs['on_content'](task.expected_json)
            message, finish = {'role': 'assistant', 'content': task.expected_json}, 'stop'
        else:
            call = {'id': 'call-' + str(len(requests)), 'type': 'function',
                    'function': {'name': name, 'arguments': json.dumps(args)}}
            kwargs['on_tool_call_ready'](call)
            message, finish = {'role': 'assistant', 'content': '', 'tool_calls': [call]}, 'tool_calls'
        return ProviderStreamResult(message=message, compatibility_finish_reason=finish,
            provider_finish_reason=finish, state=ProviderStreamState.PROVIDER_FINISHED,
            saw_done=True, saw_finish_reason=True, usage={'prompt_tokens': 10, 'completion_tokens': 5})

    def guarded(body, **kwargs):
        try: return stream(body, **kwargs)
        except Exception as error:
            transport_errors.append(repr(error))
            raise
    monkeypatch.setattr(lib.llm, 'stream_chat', guarded)
    with socket.create_server(('127.0.0.1', 0)) as endpoint:
        routing = ModelRoutingConfig.from_mapping(standalone_model_routing_envelope(
            base_url=f'http://127.0.0.1:{endpoint.getsockname()[1]}/v1'))
        with AgentRuntime.local(model_routing=routing, owner_user_id=7, max_inflight=1) as runtime:
            record = asyncio.run(run_task(runtime, task, arm, 0, tmp_path, 'medium', 30, None))
    assert not transport_errors
    assert record['oraclePassed'] and record['requiredReadsComplete']
    assert record['leafCallCount'] == len(calls_for(task))
    assert record['programCount'] == (1 if arm == 'programmatic' else 0)
    assert record['apiRoundCount'] == (2 if arm == 'programmatic' else record['leafCallCount'] + 1)
