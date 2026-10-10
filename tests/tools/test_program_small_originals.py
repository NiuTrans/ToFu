"""A small direct manifest feeds a new real VM, without a second source read."""
import asyncio
import json
import secrets
import socket

import pytest

pytestmark = pytest.mark.unit


def test_public_sdk_reuses_small_direct_manifest_without_inline_metadata(monkeypatch):
    import lib.llm
    from lib.llm._sse_core import prepare_request
    from lib.llm.stream_result import ProviderStreamResult, ProviderStreamState
    from tests.support.standalone_model_routing import standalone_model_routing_envelope
    from tofu_agent import AgentRuntime, ModelRoutingConfig

    route = {'datasetId': secrets.token_hex(16), 'pageSize': 16}
    answer = '{"count":2,"total":33}'
    requests, calls, errors = [], [], []

    def stream(body, **kwargs):
        plan = prepare_request(body, api_key='synthetic-test-key',
                               base_url='https://gateway.example/v1', api_protocol='openai')
        requests.append(plan.body['tools'])
        delivered = [m for m in body['messages'] if m.get('role') == 'tool']
        if len(requests) == 1:
            name, args = 'custom__route', {}
        elif len(requests) == 2:
            assert json.loads(delivered[-1]['content']) == route
            assert 'programInput' not in delivered[-1]['content']
            name, args = 'execute_program', {'code':
                'const m=tools.result("custom__route",{}); '
                'const p=await tools.invoke("custom__page",{dataset:m.datasetId}); '
                'return {count:p.records.length,total:p.records.reduce((s,r)=>s+r.amount,0)};'}
        else:
            assert len(requests) == 3 and json.loads(delivered[-1]['content']) == json.loads(answer)
            kwargs['on_content'](answer)
            return ProviderStreamResult(message={'role': 'assistant', 'content': answer},
                compatibility_finish_reason='stop', provider_finish_reason='stop',
                state=ProviderStreamState.PROVIDER_FINISHED, saw_done=True, saw_finish_reason=True,
                usage={'prompt_tokens': 10, 'completion_tokens': 5})
        call = {'id': 'call-' + str(len(requests)), 'type': 'function',
                'function': {'name': name, 'arguments': json.dumps(args)}}
        kwargs['on_tool_call_ready'](call)
        return ProviderStreamResult(message={'role': 'assistant', 'content': '', 'tool_calls': [call]},
            compatibility_finish_reason='tool_calls', provider_finish_reason='tool_calls',
            state=ProviderStreamState.PROVIDER_FINISHED, saw_done=True, saw_finish_reason=True,
            usage={'prompt_tokens': 10, 'completion_tokens': 5})

    def guarded(body, **kwargs):
        try: return stream(body, **kwargs)
        except Exception as error:
            errors.append(repr(error))
            raise
    monkeypatch.setattr(lib.llm, 'stream_chat', guarded)
    def tool(name, description, properties, required):
        return {'type': 'function', 'function': {'name': name, 'description': description,
            'parameters': {'type': 'object', 'properties': properties, 'required': required,
                           'additionalProperties': False}}, 'execution': {'mode': 'client'}, 'write': False}

    async def run(runtime):
        execution = runtime.start([{'role': 'user', 'content':
            'Return the number of records in the current batch and sum of their amount fields.'}],
            config={'disableModelFallback': True, 'tools': {'programmaticCalling': 'on',
                'programmaticExposure': 'additive', 'toolSearch': 'off'},
                'context': {'programmaticInputs': False}, 'orchestration': {'multiAgent': 'off'},
                'max_tokens': 4096}, custom_tools=[tool('custom__route', 'Read the batch manifest.', {}, []),
                tool('custom__page', 'Read a JSON page.', {'dataset': {'type': 'string'}}, ['dataset'])],
            custom_tools_mode='exclusive', timeout_s=30)
        async for event in execution.events_async(timeout_s=30):
            if event['type'] != 'custom_tool_call': continue
            calls.append(event)
            if event['toolName'] == 'custom__route':
                assert len(calls) == 1
                value = route
            else:
                assert event['toolName'] == 'custom__page' and event['arguments'] == {'dataset': route['datasetId']}
                value = {'records': [{'amount': 12}, {'amount': 21}], 'nextOffset': None}
            assert execution.resolve_custom_tool_call(event['callId'], json.dumps(value))
        assert (await execution.result_async(timeout_s=2)).content == answer
        assert len(calls) == 2
        runs = execution.evidence_snapshot()['programRuns']
        assert len(runs) == 1 and runs[0]['stats']['successful_tool_calls'] == 1
        assert runs[0]['stats']['result_reads'] == 1 and runs[0]['stats']['input_queries'] == 0

    with socket.create_server(('127.0.0.1', 0)) as endpoint:
        routing = ModelRoutingConfig.from_mapping(standalone_model_routing_envelope(
            base_url=f'http://127.0.0.1:{endpoint.getsockname()[1]}/v1'))
        with AgentRuntime.local(model_routing=routing, owner_user_id=7, max_inflight=1) as runtime:
            asyncio.run(run(runtime))
    assert not errors
