"""Public SDK delivers progress between real custom-tool client handshakes.

Only provider transport is scripted. This proves streaming, not model adoption.
"""
import asyncio
import json
import socket

import pytest

pytestmark = pytest.mark.unit


def test_public_sdk_delivers_settled_child_before_next_client_request(monkeypatch):
    import lib.llm
    from lib.llm.stream_result import ProviderStreamResult, ProviderStreamState
    from tests.support.standalone_model_routing import standalone_model_routing_envelope
    from tofu_agent import AgentRuntime, ModelRoutingConfig

    requests = []

    def stream(body, **kwargs):
        requests.append(body)
        if len(requests) == 1:
            call = {'id': 'sdk-progress', 'type': 'function', 'function': {
                'name': 'execute_program', 'arguments': json.dumps({'code':
                    'const a=await tools.invoke("custom__read",{});'
                    'const b=await tools.invoke("custom__read",{}); return {sum:a.value+b.value};'})}}
            kwargs['on_tool_call_ready'](call)
            message, finish = {'role': 'assistant', 'content': '', 'tool_calls': [call]}, 'tool_calls'
        else:
            assert len(requests) == 2
            kwargs['on_content']('{"sum":12}')
            message, finish = {'role': 'assistant', 'content': '{"sum":12}'}, 'stop'
        return ProviderStreamResult(message=message, compatibility_finish_reason=finish,
            provider_finish_reason=finish, state=ProviderStreamState.PROVIDER_FINISHED,
            saw_done=True, saw_finish_reason=True, usage={'prompt_tokens': 10, 'completion_tokens': 5})

    monkeypatch.setattr(lib.llm, 'stream_chat', stream)

    async def observe(runtime):
        execution = runtime.start([{'role': 'user', 'content': 'Read twice and sum values.'}],
            config={'disableModelFallback': True, 'tools': {'programmaticCalling': 'on',
                    'programmaticExposure': 'code_api', 'toolSearch': 'off'},
                    'orchestration': {'multiAgent': 'off'}, 'max_tokens': 4096},
            custom_tools=[{'type': 'function', 'function': {'name': 'custom__read',
                'description': 'Read JSON data.', 'parameters': {'type': 'object',
                'properties': {}, 'additionalProperties': False}},
                'execution': {'mode': 'client'}, 'write': False}],
            custom_tools_mode='exclusive', timeout_s=30)
        leaves, progress, types = 0, [], []
        async for event in execution.events_async(timeout_s=30):
            types.append(event['type'])
            if event['type'] == 'program_progress':
                assert leaves == len(event['childCallIds'])
                assert 'code' not in event and 'result' not in event
                progress.append(event)
            if event['type'] == 'custom_tool_call':
                if leaves == 1:
                    assert len(progress) == 1
                leaves += 1
                assert execution.resolve_custom_tool_call(event['callId'], '{"value":6}')
        assert leaves == len(progress) == 2
        assert types.index('program_progress') < types.index('program_output')
        assert (await execution.result_async(timeout_s=2)).content == '{"sum":12}'

    with socket.create_server(('127.0.0.1', 0)) as endpoint:
        access = ModelRoutingConfig.from_mapping(standalone_model_routing_envelope(
            base_url=f'http://127.0.0.1:{endpoint.getsockname()[1]}/v1'))
        with AgentRuntime.local(model_routing=access, owner_user_id=7, max_inflight=1) as runtime:
            asyncio.run(observe(runtime))
