"""Public SDK admission and teardown for the explicit native repository owner."""
from pathlib import Path
import os
import subprocess
import threading

import pytest
from test_native_extraction_stream import stream as stream, observe_children, assert_joined
from test_native_application_scanner import observe_installed_images
from tests.tasks_pkg.test_public_agent_runtime import _access, _patch_route_slot

pytestmark = pytest.mark.unit
OWNER = 900001


def require_installed_grammars():
    import tofu_repo_kernel as package
    with package._load_lock:
        _, record = package._package_manifest()
        available = 'grammar_bundle' in record['stream']
    if os.environ.get('TOFU_REQUIRE_INSTALLED_GRAMMARS') == '1':
        assert available, 'the explicitly qualified package must carry grammars'
    elif not available:
        pytest.skip('an explicit installed grammar bundle is required')


@pytest.mark.parametrize('policy', ['explicit', 'launch', 'installed'])
def test_configured_sdk_is_lazy_before_first_tool(tmp_path, policy):
    code = '''
import sys
from tofu_agent import AgentRuntime, NativeRepositoryConfiguration, PrincipalContext
directory=None if sys.argv[2]=='installed' else sys.argv[1]
configuration=(NativeRepositoryConfiguration.from_launch_policy(grammar_directory=directory,launch_policy={'frame_maximum':128<<20})
               if sys.argv[2]!='explicit' else NativeRepositoryConfiguration(directory,(8<<20,8<<20,32<<20,16<<20)))
runtime=AgentRuntime(principal=PrincipalContext.user(subject_id='lazy-native-repo',owner_user_id=73),max_inflight=1,native_repository=configuration)
try:
    assert not {'tofu_repo_kernel','scipy','numpy'} & sys.modules.keys()
    assert runtime._repository_runtime.retained_image_bytes()==0
finally:runtime.close()
'''
    result=subprocess.run([os.environ['TOFU_BENCHMARK_PYTHON'],'-c',code,str(tmp_path),policy],
        cwd=Path(__file__).resolve().parents[2],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr


@pytest.mark.parametrize('policy', ['explicit', 'launch', 'installed'])
def test_public_sdk_owns_installed_scan_and_closes_image_after_tasks(stream, monkeypatch, policy):
    from tofu_agent import AgentRuntime, NativeRepositoryConfiguration
    from tofu_agent.capabilities import runtime_capabilities
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    from lib.agent_core.admission import notify_task
    from lib.tasks_pkg.executor import tool_registry
    import lib.tasks_pkg.spawn as spawn
    scanner, root = stream
    if policy=='installed':require_installed_grammars()
    (root/'module.js').write_text('function sdkOwnedSymbol() { return referenced; }')
    _patch_route_slot(monkeypatch)
    images=observe_installed_images(monkeypatch);children=observe_children(monkeypatch)
    tasks=[];entries=[]
    def execute(task, *, executor=None):
        tasks.append(task)
        session=task['_executionSession'];session.mark_routed();session.mark_reserved();session.mark_admitted()
        chat_task_runtime.mark_running(task['id'])
        entry={'query':'map project','toolName':'repo_map','toolCallId':'sdk-repository-call'}
        # The registered product handler resolves the trusted task runtime itself.
        value=tool_registry.lookup('repo_map')(task,{},'repo_map','sdk-repository-call',{},1,entry,{},str(root),True)
        entries.append(entry)
        assert not value[2] and 'sdkOwnedSymbol' in value[1]
        chat_task_runtime.finish(task['id'],result=value[1])
        notify_task(task['id'], terminal=True)
    monkeypatch.setattr(spawn,'spawn_task',execute)
    configuration=(NativeRepositoryConfiguration.from_launch_policy(grammar_directory=None if policy=='installed' else str(scanner.grammar_directory),
                       launch_policy={'frame_maximum':128<<20}) if policy!='explicit' else
                   NativeRepositoryConfiguration(str(scanner.grammar_directory),scanner.limits.arguments()))
    runtime=AgentRuntime.local(model_routing=_access(),owner_user_id=OWNER,max_inflight=1,native_repository=configuration)
    try:
        assert runtime_capabilities(runtime)['repository_extraction']['selection']=='native'
        for _ in range(2):
            execution=runtime.start([{'role':'user','content':'map the project'}])
            assert execution._terminal.is_set()
        assert len(images)==1 and runtime._repository_runtime.retained_image_bytes()>0
        assert all(task['_repository_scanner_closed'] and '_repository_scanner_binding' not in task for task in tasks)
        assert all(entry['status']=='done' for entry in entries)
    finally:
        runtime.close()
        for task in tasks:chat_task_runtime.discard(task['id'])
    assert images[0]._image.closed and runtime._repository_runtime.retained_image_bytes()==0
    assert_joined(children)


@pytest.mark.parametrize('policy', ['external', 'installed'])
def test_sdk_close_requires_active_work_to_join_before_image_disposal(stream, monkeypatch, policy):
    from tofu_agent import AgentRuntime, AgentTimeoutError, NativeRepositoryConfiguration
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    from lib.agent_core.admission import notify_task
    from lib.tasks_pkg.executor import tool_registry
    from test_native_extraction_owned import blocked_sources, observe_first_ast
    import lib.tasks_pkg.spawn as spawn
    scanner, root=stream
    blocked_sources(root)
    if policy=='installed':
        require_installed_grammars()
        (root/'0.rs').write_text('pub fn loaded_before_block() {}\n')
    entered=observe_first_ast(monkeypatch);images=observe_installed_images(monkeypatch)
    children=observe_children(monkeypatch);tasks=[];threads=[];errors=[]
    _patch_route_slot(monkeypatch)
    def execute(task, *, executor=None):
        tasks.append(task)
        def run():
            try:
                session=task['_executionSession'];session.mark_routed();session.mark_reserved();session.mark_admitted()
                chat_task_runtime.mark_running(task['id'])
                entry={'query':'blocked repository','toolName':'repo_map','toolCallId':'sdk-blocked'}
                result=tool_registry.lookup('repo_map')(task,{},'repo_map','sdk-blocked',{},1,entry,{},str(root),True)
                chat_task_runtime.finish(task['id'],result=result[1],error='cancelled' if result[2] else None)
                notify_task(task['id'], terminal=True)
            except BaseException as error:errors.append(error)
        thread=threading.Thread(target=run);threads.append(thread);thread.start()
    monkeypatch.setattr(spawn,'spawn_task',execute)
    configuration=NativeRepositoryConfiguration(None if policy=='installed' else str(scanner.grammar_directory),scanner.limits.arguments(),timeout=10)
    runtime=AgentRuntime.local(model_routing=_access(),owner_user_id=OWNER,max_inflight=1,native_repository=configuration)
    try:
        execution=runtime.start([{'role':'user','content':'map project'}])
        assert entered.wait(timeout=5)
        with pytest.raises(AgentTimeoutError):runtime.close(abort=False,timeout_s=0)
        assert len(images)==1 and not images[0]._image.closed
        execution.abort()
        for thread in threads:thread.join(timeout=5)
        assert not errors and all(not thread.is_alive() for thread in threads)
        runtime.close()
        assert images[0]._image.closed
    finally:
        runtime.close()
        for thread in threads:thread.join(timeout=5)
        for task in tasks:chat_task_runtime.discard(task['id'])
    assert_joined(children)
