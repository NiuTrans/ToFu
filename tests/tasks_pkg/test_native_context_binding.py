"""Owned context snapshots preserve visibility, custom projections and completion."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from lib.agent_core.native_process import RoundRequest
from lib.tasks_pkg.native_context import NativeContextPolicy
from lib.tasks_pkg.native_tool_projection import NativeToolProjection
from tests.tasks_pkg.test_native_context import principal, task, metadata
from tests.tasks_pkg.test_native_tool_projection import tool

pytestmark = pytest.mark.unit


def route():
    return dict(metadata(), protocol='openai', base_url='https://example.invalid', inference_profile={})


def request(owner, number=0):
    return RoundRequest(principal(), owner['id'], 'binding', number, 1,
                        [{'role': 'user', 'content': 'Read'}])


def test_builtin_visibility_keeps_frozen_names_and_rejects_foreign_tools():
    async def run():
        owner = task()
        source = SimpleNamespace(task=owner, tool_list=[tool('read')], messages=[])
        catalog = {'read': {'arguments_schema': {'type': 'object'}},
                   'hidden': {'arguments_schema': {'type': 'object'}}}
        with ThreadPoolExecutor(max_workers=1) as executor:
            projection = NativeToolProjection(source, executor)
            policy = NativeContextPolicy(principal=principal(), task=owner, executor=executor)
            policy.bind_wire_projection(projection, source.tool_list)
            policy.bind_route(route(), {'max_tokens': 64}, catalog)
            catalog.clear()
            catalog['foreign'] = {}
            with patch('lib.tasks_pkg.orchestrator._root_agent_loop.prepare_round_context'), \
                 patch('lib.tasks_pkg.orchestrator._root_agent_loop.admit_round_context'), \
                 patch('lib.tasks_pkg.orchestrator._tool_request_metadata.attach_tool_request_metadata'), \
                 patch('lib.llm.tool_wire.project_tool_wire'):
                first = await policy.prepare(request(owner))
                assert first.wire_tools == [tool('read')]
                source.tool_list = [tool('hidden')]
                second = await policy.prepare(request(owner, 1))
                assert second.wire_tools == [tool('hidden')]
                source.tool_list = [tool('foreign')]
                with pytest.raises(ValueError, match='declared function tools'):
                    await policy.prepare(request(owner, 2))
            assert policy.completion_binding() == ('fixture', [tool('hidden')])
            policy.close()
            with pytest.raises(ValueError): policy.completion_binding()
    asyncio.run(run())


@pytest.mark.parametrize('subclass', [False, True])
def test_custom_projection_retains_full_detached_schemas_each_round(subclass):
    async def run():
        owner = task()
        catalog = {'read': {'arguments_schema': {'properties': {'value': {'type': 'string'}}}}}
        observed = []
        def project(metadata, template, received, messages, number):
            assert received['read']['arguments_schema']['properties']['value']['type'] == 'string'
            received['read']['arguments_schema']['properties']['value']['type'] = 'changed'
            observed.append(number)
            return [tool('read')]
        class CustomProjection(NativeToolProjection):
            def project(self, *args): return project(*args)
        with ThreadPoolExecutor(max_workers=1) as executor:
            projection = CustomProjection(SimpleNamespace(task=owner), executor) if subclass else project
            policy = NativeContextPolicy(principal=principal(), task=owner, executor=executor)
            policy.bind_wire_projection(projection, [tool('read')])
            policy.bind_route(route(), {'max_tokens': 64}, catalog)
            catalog['read']['arguments_schema']['properties']['value']['type'] = 'outside'
            with patch('lib.tasks_pkg.orchestrator._root_agent_loop.prepare_round_context'), \
                 patch('lib.tasks_pkg.orchestrator._root_agent_loop.admit_round_context'):
                for number in (0, 5):
                    result = await policy.prepare(request(owner, number))
                    result.wire_tools[0]['function']['name'] = 'outside'
                    exported = policy.completion_binding()[1]
                    assert exported == [tool('read')]
                    exported.clear()
                    assert policy.completion_binding() == ('fixture', [tool('read')])
            assert observed == [0, 5]
            policy.close()
    asyncio.run(run())


@pytest.mark.parametrize('defect', ['nan', 'cycle', 'not_object'])
def test_name_only_binding_still_validates_full_catalog(defect):
    owner = task()
    schema = {'type': 'object'}
    if defect == 'nan': schema['maximum'] = float('nan')
    elif defect == 'cycle': schema['recursive'] = schema
    catalog = {'read': [] if defect == 'not_object' else {'arguments_schema': schema}}
    with ThreadPoolExecutor(max_workers=1) as executor:
        projection = NativeToolProjection(SimpleNamespace(task=owner), executor)
        policy = NativeContextPolicy(principal=principal(), task=owner, executor=executor)
        policy.bind_wire_projection(projection, [])
        with pytest.raises(ValueError): policy.bind_route(route(), {'max_tokens': 64}, catalog)
        policy.close()
