"""Declared semantic results survive discovery and wire projection without authority."""
import copy
import json

import pytest

from lib.tools.contracts import ToolContractV2, adapt_legacy_tool_contract
from lib.tools.gateway import ptc_local_wire_tools, sanitize_wire_tools, search_executable_catalog
from lib.tools.tool_env import CustomToolError, dispose_tool_env, mint_tool_env

pytestmark = pytest.mark.unit


def schema():
    return {'type': 'object', 'properties': {'next': {'type': 'string'},
            'amount': {'type': 'integer'}}, 'required': ['next', 'amount'],
            'additionalProperties': False}


def tool(name='custom__read', declared=None):
    result = {'type': 'function', 'function': {'name': name,
              'description': 'Read JSON', 'parameters': {'type': 'object',
              'properties': {}, 'additionalProperties': False}}}
    if declared is not None:
        result['decoded_result_schema'] = declared
    return result


def test_contract_round_trip_and_projections_are_detached():
    declared = schema()
    contract = ToolContractV2('custom__read', {}, 'Read JSON',
                              decoded_result_schema=declared)
    declared['properties'].clear()
    provider = contract.provider_schema()
    document = contract.search_document()
    assert provider['decoded_result_schema'] == document['decoded_result_schema'] == schema()
    assert adapt_legacy_tool_contract(provider).search_document()['decoded_result_schema'] == schema()
    document['decoded_result_schema']['properties'].clear()
    provider['decoded_result_schema']['properties'].clear()
    assert contract.search_document()['decoded_result_schema'] == schema()
    assert 'decoded_result_schema' not in ToolContractV2('unknown', {}, 'Read').provider_schema()


@pytest.mark.parametrize('defect', ['list', 'nan', 'cycle', 'oversized'])
def test_invalid_producer_declaration_is_rejected(defect):
    declared = {'type': 'object'}
    if defect == 'list': declared = []
    elif defect == 'nan': declared['minimum'] = float('nan')
    elif defect == 'cycle': declared['recursive'] = declared
    else: declared['description'] = 'x' * 32769
    with pytest.raises(ValueError):
        ToolContractV2('custom__read', {}, 'Read', decoded_result_schema=declared)


def test_custom_environment_preserves_declaration_and_strips_execution():
    raw = tool(declared=schema())
    raw.update({'execution': {'mode': 'client'}, 'write': False})
    env = mint_tool_env(tools=[raw], owner='declared-result-test')
    try:
        assert env.schemas[0]['decoded_result_schema'] == schema()
        raw['decoded_result_schema']['properties'].clear()
        assert env.schemas[0]['decoded_result_schema'] == schema()
        assert 'execution' not in env.schemas[0] and 'write' not in env.schemas[0]
    finally:
        dispose_tool_env(env.handle_id)
    with pytest.raises(CustomToolError, match='decoded_result_schema'):
        mint_tool_env(tools=[tool(declared=[])], owner='declared-result-test')


def test_direct_wire_is_idempotent_and_has_no_private_provider_fields():
    catalog = [tool(declared=schema())]
    original = copy.deepcopy(catalog)
    wire = sanitize_wire_tools(catalog)
    assert catalog == original and wire is not catalog
    assert set(wire[0]) == {'type', 'function'}
    assert set(wire[0]['function']) == {'name', 'description', 'parameters'}
    text = wire[0]['function']['description']
    assert json.loads(text[text.index('{'):]) == schema()
    assert sanitize_wire_tools(wire) is wire
    assert wire[0]['function']['parameters'] == catalog[0]['function']['parameters']
    invalid = tool(declared=[])
    assert sanitize_wire_tools([invalid])[0] == tool()


def test_unknown_mcp_structured_output_is_not_a_decoded_contract():
    raw = tool()
    raw['output_schema'] = schema()  # A transport component is not the whole value.
    assert 'decoded_result_schema' not in adapt_legacy_tool_contract(raw).search_document()
    wire = ptc_local_wire_tools([raw], tier='program', eligible=[], exposure='code_api')
    assert 'Promise<unknown>' in wire[0]['function']['description']
    assert 'decoded_result_schema' not in wire[0]['function']['description']


def test_code_api_groups_only_equal_declared_returns_and_hashes_changes():
    catalog = [tool('custom__a', schema()), tool('custom__b', schema()),
               tool('custom__unknown'), tool('custom__rows', {'type': 'array',
               'items': {'type': 'integer'}})]
    original = copy.deepcopy(catalog)
    proof = {}
    wire = ptc_local_wire_tools(catalog, tier='program', eligible=[], exposure='code_api',
                               projection_evidence=proof)
    description = wire[0]['function']['description']
    assert proof['programmaticCodeApi']['schemaGroupCount'] == 3
    definitions = [json.loads(line[3:]) for line in description.splitlines() if line.startswith('// ')]
    shared = next(row for row in definitions if 'names' in row)
    assert shared['names'] == ['custom__a', 'custom__b'] and shared['decoded_result_schema'] == schema()
    assert 'Promise<Array<number | bigint>>' in description and 'Promise<unknown>' in description
    assert '"next": string' in description and catalog == original
    again = {}
    assert ptc_local_wire_tools(list(reversed(catalog)), tier='program', eligible=[],
                               exposure='code_api', projection_evidence=again) == wire
    assert again == proof
    catalog[0]['decoded_result_schema']['required'] = ['next']
    changed = {}
    ptc_local_wire_tools(catalog, tier='program', eligible=[], exposure='code_api', projection_evidence=changed)
    assert changed['programmaticCodeApi']['apiSha256'] != proof['programmaticCodeApi']['apiSha256']


def test_exact_discovery_preserves_complete_result_schema_and_detaches_it():
    declared = {'$defs': {'row': schema()}, 'type': 'array', 'items': {'$ref': '#/$defs/row'},
                'minItems': 1, 'maxItems': 10}
    catalog = [tool(declared=declared)]
    exact = search_executable_catalog(catalog, 'custom__read')['items'][0]
    assert exact['decoded_result_schema'] == declared
    exact['decoded_result_schema']['$defs'].clear()
    assert declared['$defs']
    document = adapt_legacy_tool_contract(catalog[0]).search_document()
    assert search_executable_catalog(catalog, 'custom__read',
        contract_documents_by_name={'custom__read': document})['items'][0]['decoded_result_schema'] == declared


def test_large_catalog_can_discover_results_without_truncating_constraints():
    catalog = [tool('custom__read' + str(n), schema()) for n in range(65)]
    proof = {}
    wire = ptc_local_wire_tools(catalog, tier='program', eligible=[], exposure='code_api',
                               projection_evidence=proof)
    assert proof['programmaticCodeApi']['reason'] == 'code_api_discovery'
    assert proof['programmaticCodeApi']['schemaTokens'] <= 1200
    assert 'decoded_result_schema' in wire[0]['function']['description']
    assert search_executable_catalog(catalog, 'custom__read64')['items'][0]['decoded_result_schema'] == schema()


def test_undiscoverable_return_schema_falls_back_without_partial_contract():
    declared = {'type': 'object', 'description': 'x' * 25000}
    catalog = [tool('custom__read' + str(n), declared) for n in range(65)]
    proof = {}
    wire = ptc_local_wire_tools(catalog, tier='program', eligible=[], exposure='code_api',
                               projection_evidence=proof)
    assert proof['programmaticExposure'] == 'additive'
    assert proof['programmaticCodeApi']['reason'] == 'code_api_discovery_schema_bound'
    assert len(wire) == 66
    exact = search_executable_catalog(catalog, 'custom__read64')
    assert exact['status'] == 'error' and 'items' not in exact


@pytest.mark.parametrize('mode', ['off', 'additive', 'code_api'])
def test_public_sdk_serial_dependency_uses_actual_result_with_declared_fields(monkeypatch, mode):
    """Protocol replay, not a claim of voluntary model adoption or billed gains."""
    import asyncio
    import secrets
    import socket
    import lib.llm
    from lib.llm._sse_core import prepare_request
    from lib.llm.stream_result import ProviderStreamResult, ProviderStreamState
    from tests.support.standalone_model_routing import standalone_model_routing_envelope
    from tofu_agent import AgentRuntime, ModelRoutingConfig

    nonce = secrets.token_hex(16)
    requests, leaves, transport_errors = [], [], []
    answer = '{"count":2,"total":33}'
    code = ('const route=await tools.invoke("custom__route",{}); '
            'const rows=await tools.invoke("custom__rows",{dataset:route.datasetId}); '
            'return {count:rows.length,total:rows.reduce((s,r)=>s+r.amount,0)};')
    catalog = [tool('custom__route', {'type': 'object', 'properties': {
                   'datasetId': {'type': 'string'}}, 'required': ['datasetId'],
                   'additionalProperties': False}),
               tool('custom__rows', {'type': 'array', 'items': {'type': 'object',
                   'properties': {'amount': {'type': 'integer'}}, 'required': ['amount'],
                   'additionalProperties': False}})]
    catalog[1]['function']['parameters'] = {'type': 'object', 'properties': {
        'dataset': {'type': 'string'}}, 'required': ['dataset'], 'additionalProperties': False}
    for item in catalog:
        item['execution'] = {'mode': 'client'}

    def stream(body, **kwargs):
        plan = prepare_request(body, api_key='synthetic-test-key',
                               base_url='https://gateway.example/v1', api_protocol='openai')
        requests.append(copy.deepcopy(plan.body['tools']))
        assert all('decoded_result_schema' not in item for item in requests[-1])
        descriptions = '\n'.join(item['function']['description'] for item in requests[-1])
        assert 'datasetId' in descriptions and 'amount' in descriptions
        delivered = [m for m in body['messages'] if m.get('role') == 'tool']
        if len(requests) == 1:
            assert nonce not in json.dumps({'messages': body['messages'], 'tools': requests[-1]})
            name, args = ('custom__route', {}) if mode == 'off' else ('execute_program', {'code': code})
        elif mode == 'off' and len(requests) == 2:
            route = json.loads(delivered[-1]['content'])
            name, args = 'custom__rows', {'dataset': route['datasetId']}
        else:
            assert len(requests) == (3 if mode == 'off' else 2)
            value = json.loads(delivered[-1]['content'])
            if mode == 'off': value = {'count': len(value), 'total': sum(row['amount'] for row in value)}
            assert value == json.loads(answer)
            kwargs['on_content'](answer)
            return ProviderStreamResult(message={'role': 'assistant', 'content': answer},
                compatibility_finish_reason='stop', provider_finish_reason='stop',
                state=ProviderStreamState.PROVIDER_FINISHED, saw_done=True,
                saw_finish_reason=True, usage={'prompt_tokens': 10, 'completion_tokens': 5})
        call = {'id': 'call-' + str(len(requests)), 'type': 'function',
                'function': {'name': name, 'arguments': json.dumps(args)}}
        kwargs['on_tool_call_ready'](call)
        return ProviderStreamResult(message={'role': 'assistant', 'content': '', 'tool_calls': [call]},
            compatibility_finish_reason='tool_calls', provider_finish_reason='tool_calls',
            state=ProviderStreamState.PROVIDER_FINISHED, saw_done=True,
            saw_finish_reason=True, usage={'prompt_tokens': 10, 'completion_tokens': 5})

    def guarded_stream(body, **kwargs):
        try:
            return stream(body, **kwargs)
        except Exception as error:
            transport_errors.append(repr(error))
            raise

    monkeypatch.setattr(lib.llm, 'stream_chat', guarded_stream)

    async def run(runtime):
        execution = runtime.start([{'role': 'user', 'content':
            'Return count as the number of records and total as the sum of their amount fields.'}],
            config={'disableModelFallback': True, 'tools': {'programmaticCalling':
                'off' if mode == 'off' else 'on', 'programmaticExposure':
                'code_api' if mode == 'code_api' else 'additive', 'toolSearch': 'off'},
                'orchestration': {'multiAgent': 'off'}, 'max_tokens': 4096},
            custom_tools=catalog, custom_tools_mode='exclusive', timeout_s=30)
        async for event in execution.events_async(timeout_s=30):
            if event['type'] != 'custom_tool_call': continue
            leaves.append(event)
            if event['toolName'] == 'custom__route':
                value = {'datasetId': nonce}
            else:
                assert event['toolName'] == 'custom__rows' and event['arguments'] == {'dataset': nonce}
                value = [{'amount': 12}, {'amount': 21}]
            assert execution.resolve_custom_tool_call(event['callId'], json.dumps(value))
        assert (await execution.result_async(timeout_s=2)).content == answer
        assert len(leaves) == 2
        runs = execution.evidence_snapshot()['programRuns']
        if mode == 'off':
            assert not runs
        else:
            assert len(runs) == 1 and runs[0]['stats']['successful_tool_calls'] == 2
            assert runs[0]['stats']['result_reads'] == 0

    with socket.create_server(('127.0.0.1', 0)) as endpoint:
        access = ModelRoutingConfig.from_mapping(standalone_model_routing_envelope(
            base_url=f'http://127.0.0.1:{endpoint.getsockname()[1]}/v1'))
        with AgentRuntime.local(model_routing=access, owner_user_id=7, max_inflight=1) as runtime:
            try:
                asyncio.run(run(runtime))
            except Exception as error:
                raise AssertionError({'transport_errors': transport_errors, 'requests': len(requests),
                    'leaves': len(leaves)}) from error
