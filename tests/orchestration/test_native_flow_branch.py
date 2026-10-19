"""Native branch routing matches the retained Python oracle without executing it."""
import ctypes as c
import inspect
import random
import pytest
from lib.identity import PrincipalContext
from lib.orchestration.branch_runtime import OrchestrationBranchRuntime
from lib.orchestration.native_flow import _Span
from lib.orchestration.native_flow_contract import SPEC
from tests.orchestration.test_native_flow_engine import native_library as native_library
from tests.orchestration import test_orchestration_branch_runtime as oracle

pytestmark=pytest.mark.unit


def _graph(library,branches=('a','b')):
    return library.open_graph(principal=PrincipalContext.user(subject_id='branch',owner_user_id=7),
        task_id='branch',nodes={'branch':dict(type='control',kind='branch'),'a':dict(type='role'),'b':dict(type='role')},
        edges=[{'from':'branch','to':value} for value in branches])


CASES=[f for name,f in inspect.getmembers(oracle,inspect.isfunction) if name.startswith('test_') and name!='test_engine_delegates_branch_policy_to_the_focused_runtime']
@pytest.mark.parametrize('case',CASES,ids=lambda f:f.__name__)
def test_native_runtime_reuses_classifier_prompt_events_and_failure_owner(case,native_library,monkeypatch):
    graphs=[];original=oracle._runtime
    class Port:
        def __init__(self,runtime,graph):self.runtime,self.graph=runtime,graph
        def supports_native_branch(self):return True
        def branch_plan(self,node,enabled):return self.graph.branch_plan(node,enabled)
        def branch_choose(self,node,verdict,labels):return self.graph.branch_choose(node,verdict,labels)
    original_run=OrchestrationBranchRuntime.run
    def initialize(**options):
        result=original(**options);graph=_graph(native_library,options['successors']);graphs.append(graph)
        result[0]._successors=lambda _:pytest.fail('native branch entered Python successor selection')
        result[0]._native_test_graph=graph
        return result
    def run(self,*args):
        # Some retained cases replace the label projection after construction.
        # Delegate that detached projection, while keeping native decisions.
        navigator=self._navigator
        port=Port(self,self._native_test_graph);port.node_label=navigator.node_label
        self._navigator=port
        return original_run(self,*args)
    monkeypatch.setattr(oracle,'_runtime',initialize)
    monkeypatch.setattr(OrchestrationBranchRuntime,'run',run)
    monkeypatch.setattr(OrchestrationBranchRuntime,'_classifier_choice',lambda *_:pytest.fail('native branch entered Python matching'))
    try:case()
    finally:
        for graph in graphs:graph.close()


@pytest.mark.parametrize('verdict,labels',[
    ('I choose Security Review',{'a':'Review','b':'Security Review'}),
    ('Straße',{'a':'Writer','b':'STRASSE'}),('İ',{'a':'other','b':'i\u0307'}),
    ('A',{'a':'A','b':'a'}),('a--- ',{'a':'x','b':'--'}),
    ('Data',{'a':'A','b':'Data'}),('ﬁle',{'a':'file','b':'not selected'}),
    ('Alpha Beta',{'a':'Alpha','b':'Beta'}),('x中x',{'a':'other','b':'中'}),
    ('\u001cBeta\u001f',{'a':'other','b':'\u001cBeta\u001f'}),
])
def test_native_label_choice_matches_real_python_casefold_boundary_and_containment(native_library,verdict,labels):
    graph=_graph(native_library)
    try:
        choice=OrchestrationBranchRuntime._classifier_choice(verdict,labels)
        assert graph.branch_choose('branch',verdict,labels)==(choice or 'a',choice is not None)
    finally:graph.close()


def test_full_casefold_mapping_and_random_overlaps_match_python(native_library):
    graph=_graph(native_library);randomizer=random.Random(7342)
    try:
        labels={'a':'unmentioned fallback','b':'x'}
        for code in range(0x110000):
            char=chr(code)
            if char.casefold()==char:continue
            labels['b']=char.casefold()
            expected=OrchestrationBranchRuntime._classifier_choice(char,labels)
            assert graph.branch_choose('branch',char,labels)==(expected or 'a',expected is not None),hex(code)
        alphabet='aA_ -/中ßİﬁ🙂\u0307\u001c'
        for _ in range(1000):
            verdict=''.join(randomizer.choice(alphabet) for _ in range(randomizer.randrange(25)))
            labels={name:''.join(randomizer.choice(alphabet) for _ in range(randomizer.randrange(1,7))) for name in ('a','b')}
            expected=OrchestrationBranchRuntime._classifier_choice(verdict,labels)
            assert graph.branch_choose('branch',verdict,labels)==(expected or 'a',expected is not None),(verdict,labels)
    finally:graph.close()


def test_duplicate_edges_preserve_classifier_need_and_unique_label_order(native_library):
    graph=_graph(native_library,['a','a','b']*4)
    try:
        assert graph.branch_plan('branch',True)==(['a','a','b']*4,True)
        assert graph.branch_choose('branch','B',{'a':'A','b':'B'})==('b',True)
        assert graph.branch_choose('branch','',{})==('a',False)
    finally:graph.close()


def test_branch_capacity_and_invalid_inputs_publish_no_choice(native_library):
    graph=_graph(native_library)
    output=(c.c_uint64*2)(123,456);written=c.c_size_t(987)
    try:
        f=native_library._library.tofu_flow_branch_choose
        spans=(_Span*3)((b'B',1),(b'A',1),(b'B',1))
        assert f(graph.handle,graph.index('branch'),spans,3,output,1,c.byref(written))==SPEC['statuses']['buffer_small']
        assert list(output)==[123,456] and written.value==2
        spans[2].data=b'\xff';written.value=987
        assert f(graph.handle,graph.index('branch'),spans,3,output,2,c.byref(written))==SPEC['statuses']['invalid_input']
        assert list(output)==[123,456] and written.value==987
        f=native_library._library.tofu_flow_branch_plan
        assert f(graph.handle,graph.index('branch'),2,output,2,c.byref(written))==SPEC['statuses']['invalid_input']
        assert list(output)==[123,456] and written.value==987
    finally:graph.close()


def test_selected_native_failure_never_reclassifies_or_emits_a_choice(native_library,monkeypatch):
    from lib.orchestration.graph import FlowExecutionError
    graph=_graph(native_library)
    try:
        runtime,events,calls=oracle._runtime(successors=['a','b'],classifier='Beta path',params={'classifier':'router'})
        graph.node_label=oracle._Navigator().node_label
        runtime._navigator=graph
        monkeypatch.setattr(native_library._library,'tofu_flow_branch_choose',lambda *args:SPEC['statuses']['invalid_state'])
        monkeypatch.setattr(OrchestrationBranchRuntime,'_classifier_choice',lambda *_:pytest.fail('selected error entered Python'))
        with pytest.raises(FlowExecutionError,match='invalid_state'):runtime.run('branch','seed')
        assert len(calls)==1 and not events
    finally:graph.close()


def test_classifier_delivery_failure_never_selects_or_emits(native_library,monkeypatch):
    from lib.orchestration.graph import FlowEventDeliveryError
    graph=_graph(native_library)
    try:
        runtime,events,_=oracle._runtime(successors=['a','b'],params={'classifier':'router'})
        graph.node_label=oracle._Navigator().node_label
        runtime._navigator=graph
        def fail(*args):raise FlowEventDeliveryError('classifier receipt not durable')
        runtime._run_classifier=fail
        monkeypatch.setattr(graph,'branch_choose',lambda *args:pytest.fail('failed classifier reached selection'))
        with pytest.raises(FlowEventDeliveryError,match='not durable'):runtime.run('branch','seed')
        assert not events
    finally:graph.close()


def test_plan_short_buffer_and_stale_handle_publish_no_nodes(native_library):
    graph=_graph(native_library)
    f=native_library._library.tofu_flow_branch_plan
    output=(c.c_uint64*3)(123,456,789);written=c.c_size_t(987)
    try:
        assert f(graph.handle,graph.index('branch'),1,output,2,c.byref(written))==SPEC['statuses']['buffer_small']
        assert list(output)==[123,456,789] and written.value==3
        assert f(graph.handle,graph.index('branch'),1,output,3,c.byref(written))==0
        assert list(output)==[1,graph.index('a'),graph.index('b')]
    finally:graph.close()
    written.value=987;before=list(output)
    assert f(graph.handle,graph.index('branch'),1,output,3,c.byref(written))==SPEC['statuses']['unknown_handle']
    assert list(output)==before and written.value==987


def test_partial_branch_capability_is_refused_at_library_admission(native_library,monkeypatch):
    from lib.orchestration.graph import FlowExecutionError
    monkeypatch.setattr(native_library._library,'tofu_flow_branch_choose',None)
    with pytest.raises(FlowExecutionError,match='branch capability is incomplete'):native_library._bind()
