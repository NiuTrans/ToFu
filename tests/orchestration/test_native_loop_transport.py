"""Reusable loop frames retain ingress withdrawal, ownership and output isolation."""
import threading
import pytest
from lib.identity import PrincipalContext
from lib.orchestration.graph import FlowExecutionError
from tests.orchestration.test_native_flow_engine import native_library as native_library
from tests.orchestration.test_native_loop_policy import SCENARIOS,run_case
pytestmark=pytest.mark.unit


def test_blocked_character_boundary_uses_canonical_cleanup(native_library,monkeypatch):
    if not hasattr(native_library._library,'tofu_flow_loop_advance_text'):pytest.skip('native text capability absent')
    for length in (79,80,81):
        text='汉'*length+'\n\n\n\n[VERDICT: BLOCKED]\n[PROGRESS: resolved=1 remaining=2]\n[VU: TASK_DONE]'
        # A done sentinel changes the classifier's phase; keep the stripping
        # boundary relevant to the blocked gate, including nested blank husks.
        text=text.replace('[VU: TASK_DONE]','')
        monkeypatch.setitem(SCENARIOS,'vu_boundary',[text]*8)
        assert run_case('vu_boundary',3,1,True,1,library=native_library)==run_case('vu_boundary',3,1,True,1)


def test_frame_close_withdraws_before_waiting_and_releases_buffer(native_library,monkeypatch):
    from lib.orchestration.native_flow import NativeFlowGraph
    graph=native_library.open_graph(principal=PrincipalContext.user(subject_id='close',owner_user_id=7),
        task_id='close',nodes={'loop':dict(id='loop',type='control',kind='loop')},edges=[])
    entered=threading.Event();release=threading.Event();closed=threading.Event();errors=[]
    original=NativeFlowGraph._loop_frame
    def delayed(self,*args,**kwargs):
        if args[0].startswith('tofu_flow_loop_advance'):
            entered.set();assert release.wait(5)
        return original(self,*args,**kwargs)
    try:
        with graph.native_loop('loop',dict(max_iterations=2,configured_cap=2,budget_policy='bounded')) as loop:
            monkeypatch.setattr(NativeFlowGraph,'_loop_frame',delayed)
            def advance():
                try:assert loop.advance(dict(operation='start',iteration=0))['iteration']==1
                except BaseException as error:errors.append(error)
            def close():
                try:loop.close()
                except BaseException as error:errors.append(error)
                finally:closed.set()
            worker=threading.Thread(target=advance);worker.start();assert entered.wait(5)
            closer=threading.Thread(target=close);closer.start()
            try:
                # The close flag is published before it can join the held frame.
                assert not closed.wait(.05)
                assert loop._closing
                with pytest.raises(FlowExecutionError,match='unknown_handle'):
                    loop.advance(dict(operation='start',iteration=0))
                with pytest.raises(FlowExecutionError,match='busy'):graph.close()
            finally:
                release.set();worker.join(5);closer.join(5)
            assert not worker.is_alive() and not closer.is_alive() and not errors
            assert loop.closed and loop.output is None
            with pytest.raises(FlowExecutionError,match='unknown_handle'):loop.advance(dict(operation='start',iteration=0))
    finally:release.set();graph.close()


def test_growing_output_is_reused_without_stale_bytes(native_library,monkeypatch):
    if not hasattr(native_library._library,'tofu_flow_loop_advance_text'):pytest.skip('native text capability absent')
    from lib.orchestration.native_flow import NativeFlowLoop
    observations=[];original=NativeFlowLoop.advance
    def record(loop,payload):
        result=original(loop,payload)
        observations.append((id(loop.output),len(loop.output),result['action']))
        return result
    monkeypatch.setattr(NativeFlowLoop,'advance',record)
    monkeypatch.setitem(SCENARIOS,'critic_large_transport',[
        '[PLAN_DEFECT: '+('wrong topology '*1000)+'][VERDICT: CONTINUE_PLANNER]',
        '[VERDICT: STOP]'])
    actual=run_case('critic_large_transport',3,1,True,1,library=native_library)
    assert actual==run_case('critic_large_transport',3,1,True,1)
    grown=[row for row in observations if row[1]>4096]
    assert grown and len({row[0] for row in grown})==1
    assert observations[-1][2]=='halt'


def test_text_capability_and_legacy_integer_frames_do_not_mix(native_library):
    if not hasattr(native_library._library,'tofu_flow_loop_advance_text'):pytest.skip('native text capability absent')
    graph=native_library.open_graph(principal=PrincipalContext.user(subject_id='modes',owner_user_id=7),
        task_id='modes',nodes={'loop':dict(id='loop',type='control',kind='loop')},edges=[])
    try:
        with graph.native_loop('loop',dict(max_iterations=3,configured_cap=3,budget_policy='bounded')) as loop:
            assert loop.advance(dict(operation='start',iteration=0))['iteration']==1
            payload=dict(operation='body_done',iteration=1,producer_count=0,producer_reported=False,
                producer_names=[],verifier_output=('evidence '*20)+'[VERDICT: BLOCKED][PROGRESS: resolved=1 remaining=2]',
                verifier_role='virtual_user',previous_resolved=None,verifier_tool_rounds=1,
                justification_chars=0,history_tail=[],progress_tail=[],progress_window=0,
                replan_enabled=True,digit_limit=4300)
            with pytest.raises(FlowExecutionError,match='invalid_input'):loop.advance(payload)
            payload['justification_chars']=None
            with pytest.raises(FlowExecutionError,match='invalid_input'):
                graph._loop_frame('tofu_flow_loop_advance',(loop.handle,),payload)
            result=loop.advance(payload)
            assert result['iteration']==2
            assert any(e.get('event',{}).get('reason')=='challenged' for e in result['effects'])
    finally:graph.close()


def test_selected_text_evidence_never_calls_host_token_cleanup(native_library,monkeypatch):
    if not hasattr(native_library._library,'tofu_flow_loop_advance_text'):pytest.skip('native text capability absent')
    expected=run_case('vu_blocked',3,1,True,1)
    monkeypatch.setattr('lib.orchestration.loop_runtime._strip_machine_tokens',
        lambda *a,**k:pytest.fail('native blocked evidence entered host token cleanup'))
    assert run_case('vu_blocked',3,1,True,1,library=native_library)==expected
