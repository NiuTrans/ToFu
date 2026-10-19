"""Native acknowledgement and next lookup preserve effect and refusal boundaries."""
import ctypes as c
import pytest
from lib.identity import PrincipalContext
from lib.orchestration.graph import FlowExecutionError
from lib.orchestration.native_flow import _Step
from lib.orchestration.native_flow_contract import SPEC
from tests.orchestration.test_native_flow_engine import native_library as native_library

pytestmark=pytest.mark.unit


@pytest.mark.parametrize('combined',[True,False])
def test_transition_follows_same_actions_on_combined_and_legacy_calls(native_library,monkeypatch,combined):
    if not combined:monkeypatch.setattr(native_library._library,'tofu_flow_cursor_transition',None,raising=False)
    graph=native_library.open_graph(principal=PrincipalContext.user(subject_id='transition',owner_user_id=7),
        task_id='transition',nodes={'a':dict(id='a',type='role'),'b':dict(id='b',type='role')},edges=[{'from':'a','to':'b'}])
    try:
        with graph.cursor('a',stop=None,budget=10) as cursor:
            assert cursor.step().node==0
            next_step=cursor.transition('b')
            assert next_step.node==1 and next_step.action==SPEC['actions']['role']
            assert cursor.transition(None).action==SPEC['actions']['halt']
    finally:graph.close()


def test_rejected_transition_leaves_original_pending_role_and_output_untouched(native_library):
    if not hasattr(native_library._library,'tofu_flow_cursor_transition'):pytest.skip('combined capability absent')
    graph=native_library.open_graph(principal=PrincipalContext.user(subject_id='transition',owner_user_id=7),
        task_id='bad-transition',nodes={'a':dict(id='a',type='role'),'b':dict(id='b',type='role')},edges=[{'from':'a','to':'b'}])
    try:
        with graph.cursor('a',stop=None,budget=10) as cursor:
            assert cursor.step().node==0
            output=_Step(987,987,987)
            with native_library._call() as library:
                assert library.tofu_flow_cursor_transition(graph.handle,cursor.handle,0,c.byref(output))==SPEC['statuses']['invalid_input']
                assert library.tofu_flow_cursor_transition(graph.handle,cursor.handle,1,None)==SPEC['statuses']['invalid_input']
            assert (output.node,output.next,output.action)==(987,987,987)
            assert cursor.transition('b').node==1
    finally:graph.close()


def test_next_budget_failure_cannot_reissue_the_already_returned_effect(native_library):
    graph=native_library.open_graph(principal=PrincipalContext.user(subject_id='transition',owner_user_id=7),
        task_id='budget-transition',nodes={'a':dict(id='a',type='role'),'b':dict(id='b',type='role')},edges=[{'from':'a','to':'b'}])
    try:
        with graph.cursor('a',stop=None,budget=1) as cursor:
            assert cursor.step().node==0
            with pytest.raises(FlowExecutionError,match='node budget'):cursor.transition('b')
            with pytest.raises(FlowExecutionError,match='node budget'):cursor.step()
            with pytest.raises(FlowExecutionError,match='invalid_state'):cursor.advance('b')
    finally:graph.close()
