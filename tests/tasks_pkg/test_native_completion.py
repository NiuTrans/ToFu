"""Completion work follows acknowledged native termination without replay."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch

import pytest
from lib.agent_core.abort_signal import AbortSignal
from lib.agent_core.native_process import TerminalRequest
from lib.tasks_pkg.native_execution import run_native_task
from lib.tasks_pkg.orchestrator._post_commit import finish_committed_task
from tests.agent_core.test_native_task_terminal import principal

pytestmark=pytest.mark.unit


@pytest.mark.parametrize('committed',[False,True])
def test_native_translation_once_and_post_commit_only_after_acknowledgement(committed):
    async def run():
        task={'id':'task','_userId':7,'status':'running','messages':[],'abort_event':AbortSignal()}
        client=SimpleNamespace(principal=principal(),budget=SimpleNamespace(frame_bytes=4096))
        configuration={'owner_user_id':7,'task_id':'task','terminal_persistence':{
            'operation_id':'commit','attempts':2,'ack_ms':500,'frame_bytes':4096}}
        rounds=SimpleNamespace(prepare=lambda request:[],reserve=AsyncMock(return_value=True))
        completion=Mock();order=[]
        async def native(*args,**kwargs):
            request=SimpleNamespace(principal=principal(),task_id='task',round=3)
            await kwargs['round_policy'].reserve(request)
            candidate=TerminalRequest(principal(),'task','commit',1,
                json.dumps({'type':'done','status':'done','result':{
                    'messages':[{'role':'assistant','content':'final prose'}],'usage':[]}}).encode(),
                b'{"invariants_satisfied":true}')
            assert await kwargs['terminal_policy'].persist(candidate)=='retry'
            if committed:
                from dataclasses import replace
                assert await kwargs['terminal_policy'].persist(replace(candidate,attempt=2))=='accepted'
            order.append('joined')
            return 'finished'
        def after(task):order.append('completion');completion(task)
        with ThreadPoolExecutor(max_workers=1) as executor,\
                patch('lib.tasks_pkg.native_execution.run_native_route',side_effect=native),\
                patch('lib.tasks_pkg.native_terminal._persist',side_effect=[False,True] if committed else [False]),\
                patch('lib.tasks_pkg.manager.append_event'),\
                patch('lib.translate.submit_round_segment') as translation:
            await run_native_task(client,object(),configuration,task=task,executor=executor,
                callbacks={},working_directory=Path('/tmp'),round_policy=rounds,after_terminal=after)
            translation.assert_called_once_with(task,3,'final prose')
            assert completion.call_count==int(committed)
            assert order==(['joined','completion'] if committed else ['joined'])
    asyncio.run(run())


def test_shared_post_commit_captures_hooks_then_releases_existing_heavy_state():
    messages=[{'role':'assistant','content':'x'*100000}]
    task={'id':'task','_userId':7,'status':'done','messages':messages,'toolRounds':[{'status':'done'}]}
    calls=[]
    def commit(task,*args,**kwargs):assert task['messages'] is messages;calls.append('commit')
    def profile(task,observed,cfg):
        assert task['messages'] is None
        assert observed is messages
        calls.append('profile')
    with patch('lib.tasks_pkg.event_invariants.maybe_check_task_log'),\
            patch('lib.tasks_pkg.orchestrator._post_commit.notify_terminal_conversation_change'),\
            patch('lib.swarm.delivery.restore_unconsumed_results'),\
            patch('lib.swarm.turn_handoff.resume_swarm_after_turn'):
        finish_committed_task(task,messages=messages,cfg={},project_enabled=False,project_path=None,
            commit_round=commit,consolidate_profile=profile)
    assert task['messages'] is None and calls==['commit','profile']


def test_cancelled_post_commit_wait_joins_callback_and_preserves_primary_failure():
    async def run():
        task={'id':'task','_userId':7,'status':'error','abort_event':AbortSignal()}
        client=SimpleNamespace(principal=principal(),budget=SimpleNamespace(frame_bytes=4096))
        configuration={'owner_user_id':7,'task_id':'task','terminal_persistence':{
            'operation_id':'commit','attempts':2,'ack_ms':500,'frame_bytes':4096}}
        entered,release,finished=threading.Event(),threading.Event(),threading.Event()
        failure=OSError('original native failure')
        async def native(*args,**kwargs):raise failure
        def complete(task):entered.set();assert release.wait(3);finished.set()
        with ThreadPoolExecutor(max_workers=1) as executor,\
                patch('lib.tasks_pkg.native_execution.NativeTaskTerminalPolicy',return_value=SimpleNamespace(accepted=True)),\
                patch('lib.tasks_pkg.native_execution.run_native_route',side_effect=native):
            task['status']='running'
            work=asyncio.create_task(run_native_task(client,object(),configuration,task=task,executor=executor,
                callbacks={},working_directory=Path('/tmp'),after_terminal=complete))
            assert await asyncio.to_thread(entered.wait,2)
            work.cancel();await asyncio.sleep(0)
            assert not work.done()
            release.set()
            with pytest.raises(OSError) as caught:await work
            assert caught.value is failure and finished.is_set()
    asyncio.run(run())


def test_joined_native_run_releases_terminal_projection_owner_without_gc(monkeypatch):
    """A completed invocation must not retain its caller's context until full GC."""
    import gc
    import weakref

    class Projection:
        def __call__(self, task, candidate):
            return None

    async def native(*args, **kwargs):
        return 'joined'

    monkeypatch.setattr('lib.tasks_pkg.native_execution.run_native_route', native)

    async def run():
        task = {'id': 'task', '_userId': 7, 'status': 'running', 'abort_event': AbortSignal()}
        client = SimpleNamespace(principal=principal(), budget=SimpleNamespace(frame_bytes=4096))
        configuration = {'owner_user_id': 7, 'task_id': 'task', 'terminal_persistence': {
            'operation_id': 'commit', 'attempts': 2, 'ack_ms': 500, 'frame_bytes': 4096}}
        projection = Projection()
        owner = weakref.ref(projection)
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert await run_native_task(client, object(), configuration, task=task,
                executor=executor, callbacks={}, working_directory=Path('/tmp'),
                project_terminal=projection) == 'joined'
        del projection
        assert owner() is None

    enabled = gc.isenabled()
    gc.disable()
    try:
        asyncio.run(run())
    finally:
        if enabled:
            gc.enable()
