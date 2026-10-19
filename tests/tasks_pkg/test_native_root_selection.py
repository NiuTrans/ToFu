"""Root invocation selects one execution/terminal owner after shared preparation."""
from types import SimpleNamespace
import asyncio
from unittest.mock import Mock
import pytest

from lib.tasks_pkg.native_invocation import NativePreparedInvocation
from tests.tasks_pkg.test_native_prepared import prepared

pytestmark=pytest.mark.unit


def install(monkeypatch):
    import lib.tasks_pkg.orchestrator._run as root
    # Selection-only fixtures mock teardown; do not leak request correlation
    # into later HTTP/lifecycle tests running on the same thread.
    monkeypatch.setattr(root,'set_req_id',lambda *args:None)
    request=prepared();task=request.task;task['messages']=request.messages
    for name in ['snapshot_turn_input','bind_provider_and_affinity','setup_project_context',
                 'maybe_run_memory_prefetch','apply_resume_state','_emit_startup_phase']:
        monkeypatch.setattr(root,name,lambda *args,**kwargs:None)
    monkeypatch.setattr(root,'check_autopilot_kick',lambda task:False)
    monkeypatch.setattr(root,'log_task_open',lambda *args:0)
    monkeypatch.setattr(root,'make_vu_phase',lambda *args:lambda *a,**k:None)
    monkeypatch.setattr(root,'run_turn_prelude',lambda task,cfg,tid:cfg)
    monkeypatch.setattr(root,'resolve_and_seed_model_config',lambda *args:{
        'model':'fixture','thinking_enabled':False,'thinking_depth':None,'preset':'default',
        'max_tokens':512,'temperature':None,'search_mode':'none','search_enabled':False,
        'fetch_enabled':False,'project_path':'','project_enabled':False,
        'code_exec_enabled':False,'memory_enabled':False})
    monkeypatch.setattr(root,'handle_abort_during_prep',lambda *a,**k:False)
    monkeypatch.setattr(root,'prepare_resume_state',lambda *a:None)
    monkeypatch.setattr(root,'prepare_continue_tool_history',lambda **k:SimpleNamespace(injected_calls=0))
    monkeypatch.setattr(root,'inject_continue_tool_history',lambda **k:0)
    monkeypatch.setattr(root,'start_prefetches',lambda *a,**k:None)
    monkeypatch.setattr(root,'assemble_round_tools',lambda *a:([],False))
    monkeypatch.setattr(root,'inject_context_and_emit_chips',lambda **k:None)
    monkeypatch.setattr('lib.swarm.integration._rehydrate.resume_swarm_for_continue',lambda *a:None)
    ports={name:Mock() for name in ['run_root_agent_loop','finalize_after_loop','handle_task_fatal',
                                  'retain_failure_messages','stop_prefetches','finalize_task_lane']}
    for name,value in ports.items():monkeypatch.setattr(root,name,value)
    return root,task,ports


@pytest.mark.parametrize('status',['done','error','aborted'])
def test_native_terminal_skips_python_loop_and_finalization_but_runs_teardown(monkeypatch,status):
    root,task,ports=install(monkeypatch)
    port=object.__new__(NativePreparedInvocation);task['_nativePreparedInvocation']=port
    seen=[]
    def native(self,request):seen.append(request);task['status']=status
    monkeypatch.setattr(NativePreparedInvocation,'run',native)
    root._execute_task_invocation(task,policy=root.ROOT_INVOCATION_POLICY)
    assert len(seen)==1 and seen[0].task is task and seen[0].cfg is task['config']
    ports['run_root_agent_loop'].assert_not_called();ports['finalize_after_loop'].assert_not_called()
    ports['handle_task_fatal'].assert_not_called();ports['finalize_task_lane'].assert_called_once()
    ports['stop_prefetches'].assert_called_once()


@pytest.mark.parametrize('kind',[OSError,KeyboardInterrupt,asyncio.CancelledError])
def test_native_terminal_failure_preserves_cause_and_never_retries_or_reprojects(monkeypatch,kind):
    root,task,ports=install(monkeypatch);task['_nativePreparedInvocation']=object.__new__(NativePreparedInvocation)
    failure=kind('lost terminal ack')
    def native(self,request):task['status']='error';raise failure
    monkeypatch.setattr(NativePreparedInvocation,'run',native)
    with pytest.raises(kind) as caught:root._execute_task_invocation(task,policy=root.ROOT_INVOCATION_POLICY)
    assert caught.value is failure
    for name in ['run_root_agent_loop','finalize_after_loop','handle_task_fatal','retain_failure_messages']:
        ports[name].assert_not_called()
    ports['finalize_task_lane'].assert_called_once()


def test_default_python_selection_keeps_existing_loop_and_finalizer(monkeypatch):
    root,task,ports=install(monkeypatch)
    ports['run_root_agent_loop'].return_value=SimpleNamespace(last_round_num=0)
    root._execute_task_invocation(task,policy=root.ROOT_INVOCATION_POLICY)
    ports['run_root_agent_loop'].assert_called_once()
    ports['finalize_after_loop'].assert_called_once()
    ports['finalize_task_lane'].assert_called_once()


def test_native_preterminal_failure_uses_existing_fatal_owner_without_retry(monkeypatch):
    root,task,ports=install(monkeypatch);task['_nativePreparedInvocation']=object.__new__(NativePreparedInvocation)
    failure=OSError('route unavailable')
    def native(self,request):raise failure
    monkeypatch.setattr(NativePreparedInvocation,'run',native)
    root._execute_task_invocation(task,policy=root.ROOT_INVOCATION_POLICY)
    ports['handle_task_fatal'].assert_called_once_with(task,failure,allow_retry=False)
    ports['run_root_agent_loop'].assert_not_called();ports['finalize_after_loop'].assert_not_called()
    ports['finalize_task_lane'].assert_called_once()


def test_fatal_owner_explicitly_disables_turn_reexecution(monkeypatch):
    from tests.tasks_pkg.test_failed_invocation_evidence import _spine
    from lib.tasks_pkg.orchestrator import _post_loop as post
    failure=RuntimeError('native preterminal failure')
    _,_,task,_,_,observed=_spine(monkeypatch,failure)
    retry=Mock(return_value=True)
    monkeypatch.setattr(post,'_maybe_auto_retry_turn',retry)
    assert post.handle_task_fatal(task,failure,allow_retry=False) is False
    retry.assert_not_called()
    assert task['status']=='error' and len(observed)==1
