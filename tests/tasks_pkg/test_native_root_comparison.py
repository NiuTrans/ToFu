"""Both measured root arms reach real provider and durable Turn ownership."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import pytest

from tests.storage.test_storage_application_contract import application_storage as application_storage
from tests.storage.test_storage_application_contract import application_storage_owner_id as application_storage_owner_id

pytestmark = pytest.mark.api


@pytest.mark.parametrize('scenario',['text','discovery_execute','mixed_direct_first','mixed_gateway_first'])
@pytest.mark.parametrize('inject_context',[False,True])
def test_root_comparability_reports_actual_provider_projection(application_storage, tmp_path, scenario, inject_context, monkeypatch):
    source = os.environ.get('TOFU_NATIVE_TERMINAL_TEST_ARCHIVE')
    if not source:
        pytest.skip('explicit verified native archive required')
    from scripts.build_native_runtime import verify
    from lib.agent_core.native_image import NativeArchiveImage
    from lib.agent_core.native_process import Budget
    from lib.tasks_pkg.native_host import NativeHost
    from lib.resource_control.runtime import launch_policy
    from benchmarks.native_agent_root import RootWorkload, comparison_receipt
    if inject_context:
        from lib.tasks_pkg.orchestrator import _root_agent_loop, _tool_request_metadata
        original_prepare=_root_agent_loop.prepare_round_context
        original_metadata=_tool_request_metadata.attach_tool_request_metadata
        marker='Compare the independent evidence after context injection.'
        def prepare(task,messages,**values):
            original_prepare(task,messages,**values)
            if not any(message.get('content')==marker for message in messages):
                messages.append({'role':'user','content':marker})
        def attach(task,body,messages,*args,**values):
            assert any(message.get('content')==marker for message in messages),'wire intent was frozen before injection'
            return original_metadata(task,body,messages,*args,**values)
        monkeypatch.setattr(_root_agent_loop,'prepare_round_context',prepare)
        monkeypatch.setattr(_tool_request_metadata,'attach_tool_request_metadata',attach)
    archive = Path(source)
    manifest = verify(archive)
    budget = Budget(1, 16 << 20, 1 << 20, 1 << 20, 5)
    image = NativeArchiveImage(archive=archive, sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        application_version=manifest['build']['application_version'], budget=budget)
    host = NativeHost(image)
    async def run():
        with ThreadPoolExecutor(max_workers=1) as executor:
            workload = RootWorkload(storage=application_storage, host=host, executor=executor,
                directory=tmp_path, budget=budget, resource_policy=launch_policy({}), scenario=scenario)
            try:
                rows = [await workload.run(arm) for arm in ('python', 'native')]
                assert rows[0]['content'] == rows[1]['content']
                for name in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
                    assert rows[0]['usage'][name] == rows[1]['usage'][name]
                assert rows[0]['requests'][0]['messages'] == rows[1]['requests'][0]['messages']
                assert {key:value for key,value in rows[0]['requests'][0].items() if key!='tools'} == {
                    key:value for key,value in rows[1]['requests'][0].items() if key!='tools'}
                from lib.llm_sanitize._fields import _API_MESSAGE_FIELDS
                for message in rows[1]['requests'][0]['messages']:
                    assert set(message) <= _API_MESSAGE_FIELDS
                python_tools = {tool['function']['name']:tool['function']
                                for tool in rows[0]['requests'][0]['tools']}
                native_tools = {tool['function']['name']:tool['function']
                                for tool in rows[1]['requests'][0]['tools']}
                for name in python_tools.keys() & native_tools.keys():
                    assert native_tools[name] == python_tools[name], name
                receipt = comparison_receipt(rows)
                assert receipt['equivalent_work']
                assert receipt['measurement_permitted'] == (rows[0]['requests'] == rows[1]['requests'])
                assert not receipt['performance_admitted']
                backend = os.environ['TOFU_PERSONAL_STORAGE_BACKEND']
                receipt.update(archive_sha256=image.sha256,
                    application_version=manifest['build']['application_version'],
                    backend=backend, transport=os.environ.get('TOFUDB_TRANSPORT', 'daemon'),
                    runner_pid=os.getpid(),scenario=scenario,context_injected=inject_context)
                destination = os.environ.get('TOFU_NATIVE_ROOT_COMPARISON_RECEIPTS')
                if destination:
                    path = Path(destination)
                    path.mkdir(parents=True, exist_ok=True)
                    (path / (backend + '-' + os.environ.get('TOFUDB_TRANSPORT', 'daemon') + '-' + scenario + ('-injected' if inject_context else '') + '.json')).write_text(
                        json.dumps(receipt, indent=2) + '\n')
                print('ROOT_COMPARABILITY ' + json.dumps(receipt, sort_keys=True))
            finally:
                workload.close()
                await host.close()
    asyncio.run(run())
