"""Native loop decisions match complete Python outcomes, ledgers and event order."""
import ctypes as c
import itertools
import json
import pytest
from lib.agent_verdict import classify_verdict,parse_progress
from lib.identity import PrincipalContext
from lib.orchestration.native_flow import NativeFlowBinding
from tests.orchestration.test_native_flow_engine import native_library as native_library
from tests.orchestration.test_orchestration_loop_runtime import _runtime
pytestmark=pytest.mark.unit
SCENARIOS={
 'critic_continue':['[VERDICT: CONTINUE_WORKER]']*8,
 'critic_stop':['[VERDICT: CONTINUE_WORKER]','[VERDICT: STOP]']*4,
 'planner':['[PLAN_DEFECT: wrong build graph][VERDICT: CONTINUE_PLANNER]']*8,
 'vu_missing':['[VU: TASK_DONE]']*8,
 'vu_stop':['[VU: TASK_DONE][PROGRESS: resolved=1 remaining=0]']*8,
 'vu_vacuous':['[VU: TASK_DONE][PROGRESS: resolved=0 remaining=0]']*8,
 'vu_remaining':['[VU: TASK_DONE][PROGRESS: resolved=2 remaining=3]']*8,
 'vu_blocked':['Concrete unavailable dependency requires external action. '+('evidence '*15)+'[VERDICT: BLOCKED][PROGRESS: resolved=2 remaining=3]']*8,
 'vu_false_blocked':['[VERDICT: BLOCKED][PROGRESS: resolved=2 remaining=3]']*8,
 'vu_churn':[(' '.join(f'aspect{i}_{j}' for j in range(8)))+' [PROGRESS: resolved=1 remaining=2]' for i in range(8)],
}
CASES=list(itertools.product(SCENARIOS,(1,2,5),(0,1),(False,True),(0,1)))

def run_case(scenario,cap,changes,reported,tools,*,library=None,budget='bounded'):
    role='virtual_user' if scenario.startswith('vu_') else 'critic'
    def classify(text,*,verifier_role=''):
        result=classify_verdict(text,verifier_role=verifier_role,loose_fallback=True)
        return result['phase'],result['plan_defect']
    runtime,outcomes,feedback,events,iterations,replans=_runtime(SCENARIOS[scenario],
        max_iterations=cap,state_changing=changes,producer_reported=reported,
        verifier_role=role,verifier_tool_rounds=tools,classify=classify,progress_parser=parse_progress)
    runtime._budget_policy=budget
    if library is None:result=runtime.run('loop','request')
    else:
        nav=runtime._navigator
        edges=[{'from':source,'to':target} for source,targets in nav.fwd.items() for target in targets]
        binding=NativeFlowBinding(library,PrincipalContext.user(subject_id='policy',owner_user_id=7),
            'policy',nav.nodes,edges)
        runtime._navigator=binding
        with binding.run_scope():result=runtime.run('loop','request')
    return dict(result=result,outcomes=outcomes.loop_exits_snapshot(),events=events,iterations=iterations,
        replans=replans,feedback=feedback.pending_feedback(),directive=feedback.pending_directive(),
        history=feedback.history_snapshot(),progress=feedback.vu_progress_snapshot())

@pytest.mark.parametrize('scenario,cap,changes,reported,tools',CASES)
def test_full_loop_decisions_match_python(scenario,cap,changes,reported,tools,native_library,monkeypatch):
    if not hasattr(native_library._library,'tofu_flow_loop_open'):pytest.skip('native loop capability absent')
    monkeypatch.setenv('TOFU_FLOW_REPLAN','1');monkeypatch.setenv('TOFU_AUTOPILOT_PROGRESS_WINDOW','4')
    audits=[];monkeypatch.setattr('lib.agent_verdict.audit_log',lambda event,**kw:audits.append((event,kw)))
    expected=run_case(scenario,cap,changes,reported,tools);expected_audits=list(audits);audits.clear()
    actual=run_case(scenario,cap,changes,reported,tools,library=native_library)
    assert actual==expected
    assert audits==expected_audits
    assert not native_library._graphs


def test_checkpoint_progress_regression_cannot_be_recredited(native_library,monkeypatch):
    if not hasattr(native_library._library,'tofu_flow_loop_open'):pytest.skip('native loop capability absent')
    outputs=['[PROGRESS: resolved=100 remaining=2]', '[PROGRESS: resolved=1 remaining=2]',
             '[PROGRESS: resolved=100 remaining=2]','[PROGRESS: resolved=100 remaining=2]']
    monkeypatch.setitem(SCENARIOS,'vu_credit',outputs)
    assert run_case('vu_credit',1,0,False,0,budget='checkpoint_extend',library=native_library)==run_case(
        'vu_credit',1,0,False,0,budget='checkpoint_extend')


def test_state_refusals_and_short_buffers_do_not_commit(native_library):
    from lib.orchestration.native_flow_contract import SPEC
    from lib.orchestration.graph import FlowExecutionError
    if not hasattr(native_library._library,'tofu_flow_loop_open'):pytest.skip('native loop capability absent')
    nodes={'loop':dict(id='loop',type='control',kind='loop')}
    graph=native_library.open_graph(principal=PrincipalContext.user(subject_id='fault',owner_user_id=7),task_id='fault',nodes=nodes,edges=[])
    try:
        with graph.native_loop('loop',dict(max_iterations=2,configured_cap=2,budget_policy='bounded')) as loop:
            with pytest.raises(FlowExecutionError,match='busy'):graph.close()
            raw=json.dumps(dict(operation='start',iteration=0)).encode();buffer=c.create_string_buffer(raw)
            output=c.create_string_buffer(b'unchanged',16);written=c.c_size_t(123)
            with native_library._call() as library:
                assert library.tofu_flow_loop_advance(graph.handle,loop.handle,buffer,len(raw),output,1,c.byref(written))==SPEC['statuses']['buffer_small']
                assert output.value==b'unchanged' and written.value>1
                corrupt=c.create_string_buffer(b'\xff');written.value=123
                assert library.tofu_flow_loop_advance(graph.handle,loop.handle,corrupt,1,output,16,c.byref(written))==SPEC['statuses']['invalid_input']
                assert output.value==b'unchanged' and written.value==123
            assert loop.advance(dict(operation='start',iteration=0))['iteration']==1
            with pytest.raises(FlowExecutionError,match='invalid_state'):loop.advance(dict(operation='start',iteration=0))
        with pytest.raises(FlowExecutionError,match='unknown_handle'):loop.advance(dict(operation='start',iteration=0))
    finally:graph.close()


def test_large_progress_receipts_and_checkpoint_windows_match(native_library,monkeypatch):
    if not hasattr(native_library._library,'tofu_flow_loop_open'):pytest.skip('native loop capability absent')
    high='9'*4000
    outputs=['[PROGRESS: resolved='+high+' remaining=1]']*3+['[VU: TASK_DONE][PROGRESS: resolved='+high+' remaining=0]']
    monkeypatch.setitem(SCENARIOS,'vu_large',outputs)
    assert run_case('vu_large',4,1,True,1,library=native_library)==run_case('vu_large',4,1,True,1)
    monkeypatch.setitem(SCENARIOS,'critic_extend',['[VERDICT: CONTINUE_WORKER]']*4+['[VERDICT: STOP]'])
    assert run_case('critic_extend',2,1,True,0,library=native_library,budget='checkpoint_extend')==run_case(
        'critic_extend',2,1,True,0,budget='checkpoint_extend')


def test_accepted_state_does_not_replay_when_cancelled_after_body(native_library):
    from lib.orchestration.loop_runtime import OrchestrationLoopAborted
    if not hasattr(native_library._library,'tofu_flow_loop_open'):pytest.skip('native loop capability absent')
    def run(library):
        runtime,outcomes,feedback,events,iterations,replans=_runtime(['[VERDICT: CONTINUE_WORKER]'],classify=lambda *a,**k:('worker',None))
        body=runtime._walk;cancelled=[]
        def walk(*a,**k):
            result=body(*a,**k);cancelled.append(True);return result
        runtime._walk=walk;runtime._abort_check=lambda:bool(cancelled)
        nav=runtime._navigator;edges=[{'from':a,'to':b} for a,targets in nav.fwd.items() for b in targets]
        if library is not None:runtime._navigator=NativeFlowBinding(library,PrincipalContext.user(subject_id='abort',owner_user_id=7),'abort',nav.nodes,edges)
        if library is None:
            with pytest.raises(OrchestrationLoopAborted):runtime.run('loop','request')
        else:
            with runtime._navigator.run_scope():
                with pytest.raises(OrchestrationLoopAborted):runtime.run('loop','request')
        return events,iterations,outcomes.loop_exits_snapshot()
    assert run(native_library)==run(None)
    assert not native_library._graphs


def test_loop_scope_and_concurrent_start_are_fenced(native_library):
    from concurrent.futures import ThreadPoolExecutor
    from lib.orchestration.graph import FlowExecutionError
    from lib.orchestration.native_flow_contract import SPEC
    if not hasattr(native_library._library,'tofu_flow_loop_open'):pytest.skip('native loop capability absent')
    principal=PrincipalContext.user(subject_id='scoped',owner_user_id=7)
    nodes={'loop':dict(id='loop',type='control',kind='loop')}
    graph=native_library.open_graph(principal=principal,task_id='one',nodes=nodes,edges=[])
    other=native_library.open_graph(principal=principal,task_id='two',nodes=nodes,edges=[])
    try:
        with graph.native_loop('loop',dict(max_iterations=2,configured_cap=2,budget_policy='bounded')) as loop:
            def start(_):
                try:return loop.advance(dict(operation='start',iteration=0))['action']
                except FlowExecutionError as error:return str(error)
            with ThreadPoolExecutor(max_workers=8) as workers:results=list(workers.map(start,range(8)))
            assert results.count('body')==1
            assert all('invalid_state' in item for item in results if item!='body')
            with native_library._call() as library:
                assert library.tofu_flow_loop_close(other.handle,loop.handle)==SPEC['statuses']['unknown_handle']
    finally:other.close();graph.close()


def test_feedback_similarity_uses_host_unicode_case_rules(native_library,monkeypatch):
    if not hasattr(native_library._library,'tofu_flow_loop_open'):pytest.skip('native loop capability absent')
    # Cyrillic TJE gained case mapping after Python 3.12's Unicode 15 tables.
    upper=' '.join('\u1c89'+str(i) for i in range(12))
    lower=' '.join('\u1c8a'+str(i) for i in range(12))
    monkeypatch.setitem(SCENARIOS,'critic_unicode',[upper+' [VERDICT: CONTINUE_WORKER]',lower+' [VERDICT: CONTINUE_WORKER]','[VERDICT: STOP]'])
    assert run_case('critic_unicode',3,1,True,0,library=native_library)==run_case('critic_unicode',3,1,True,0)
