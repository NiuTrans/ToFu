"""Native fan-out plan/merge preserves host scheduling, faults and joined exit."""
import ctypes as c
import inspect
import pytest
from lib.identity import PrincipalContext
from lib.orchestration.graph import FlowExecutionError
from lib.orchestration.parallel_runtime import OrchestrationParallelRuntime
from lib.orchestration.native_flow_contract import SPEC
from tests.orchestration.test_native_flow_engine import native_library as native_library
from tests.orchestration import test_orchestration_parallel_runtime as oracle

pytestmark=pytest.mark.unit


def _graph(library,branches):
    nodes={'fanout':dict(type='control',kind='parallel')}
    nodes.update({branch:dict(type='role') for branch in branches})
    nodes.update(join=dict(type='control',kind='barrier'),after=dict(type='role'))
    edges=[dict(**{'from':'fanout','to':branch}) for branch in branches]
    edges += [dict(**{'from':branch,'to':'join'}) for branch in branches]
    edges.append(dict(**{'from':'join','to':'after'}))
    return library.open_graph(principal=PrincipalContext.user(subject_id='parallel',owner_user_id=7),
        task_id='parallel',nodes=nodes,edges=edges)


CASES=[value for name,value in inspect.getmembers(oracle,inspect.isfunction) if name.startswith('test_')]

@pytest.mark.parametrize('case',CASES,ids=lambda case:case.__name__)
def test_native_parallel_obeys_retained_runtime_fault_and_capacity_contracts(case,native_library,monkeypatch,request):
    graphs=[]
    original=oracle._runtime
    def runtime(branches,walk,**options):
        result=original(branches,walk,**options)
        graph=_graph(native_library,branches)
        graphs.append(graph)
        result[0]._navigator=graph
        result[0]._branches=lambda _:pytest.fail('native plan entered Python branches')
        return result
    monkeypatch.setattr(oracle,'_runtime',runtime)
    monkeypatch.setattr(OrchestrationParallelRuntime,'_merge',lambda *_:pytest.fail('native merge entered Python'))
    try:
        arguments={name:request.getfixturevalue(name) for name in inspect.signature(case).parameters if name!='failure'}
        if 'failure' in inspect.signature(case).parameters:
            for failure in ('abort','event-delivery'):case(**arguments,failure=failure)
        else:case(**arguments)
    finally:
        for graph in graphs:graph.close()


@pytest.mark.parametrize('transport',['spans','json'])
@pytest.mark.parametrize('context,outputs',[('',[]),('', ['','', 'x','']),('seed',['','x','']),('中🙂',['\x00','🙂'*5000])])
def test_native_merge_exact_empty_unicode_and_large_results(native_library,monkeypatch,context,outputs,transport):
    if transport=='json':monkeypatch.setattr(native_library._library,'tofu_flow_parallel_merge_spans',None,raising=False)
    graph=_graph(native_library,[str(i) for i in range(len(outputs))])
    try:
        assert graph.parallel_merge('fanout',context,outputs)==OrchestrationParallelRuntime._merge(context,outputs)
        branches,barrier,next_node=graph.parallel_plan('fanout')
        assert branches==[str(i) for i in range(len(outputs))]
        assert (barrier,next_node)==(('join','after') if outputs else (None,None))
    finally:graph.close()


def test_native_merge_short_buffer_and_bad_receipts_publish_no_bytes(native_library):
    graph=_graph(native_library,['a','b'])
    try:
        function=native_library._library.tofu_flow_parallel_merge
        raw=b'{"context":"seed","outputs":["a","b"]}'
        output=c.create_string_buffer(b'unchanged',10);written=c.c_size_t(987)
        assert function(graph.handle,graph.index('fanout'),raw,len(raw),output,1,c.byref(written))==SPEC['statuses']['buffer_small']
        assert output.value==b'unchanged' and written.value==10
        for raw in (b'{"context":"seed","outputs":["a"]}',b'{"context":"seed","outputs":["a",null]}',b'{"context":"seed","outputs":["a","b"],"extra":1}'):
            written.value=987
            assert function(graph.handle,graph.index('fanout'),raw,len(raw),output,len(output),c.byref(written))==SPEC['statuses']['invalid_input']
            assert output.value==b'unchanged' and written.value==987
        with pytest.raises(FlowExecutionError,match='invalid_input'):graph.parallel_plan('a')
    finally:graph.close()
    with pytest.raises(FlowExecutionError,match='unknown_handle'):graph.parallel_merge('fanout','',['a','b'])


def test_optional_absence_is_explicit_but_selected_failure_never_falls_back(native_library,monkeypatch):
    graph=_graph(native_library,['a'])
    try:
        assert graph.supports_native_parallel()
        monkeypatch.setattr(native_library._library,'tofu_flow_parallel_merge_spans',None,raising=False)
        monkeypatch.setattr(native_library._library,'tofu_flow_parallel_merge',None)
        assert not graph.supports_native_parallel()
        with pytest.raises(FlowExecutionError,match='capability absent'):graph.parallel_merge('fanout','',['a'])
    finally:graph.close()


def test_native_parallel_query_small_output_and_merge_failure_are_atomic(native_library,monkeypatch):
    graph=_graph(native_library,['a','b'])
    try:
        output=(c.c_uint64*2)(123,456);written=c.c_size_t(987)
        status=native_library._library.tofu_flow_query(graph.handle,SPEC['queries']['parallel_plan'],
            graph.index('fanout'),SPEC['none_index'],SPEC['none_index'],None,0,output,2,c.byref(written))
        assert status==SPEC['statuses']['buffer_small']
        assert list(output)==[123,456] and written.value==987
        runtime,events,outcomes=oracle._runtime(['a','b'],lambda entry,context,**kwargs:entry)
        runtime._navigator=graph
        def reject(*args):raise FlowExecutionError('injected native merge refusal')
        monkeypatch.setattr(graph,'parallel_merge',reject)
        monkeypatch.setattr(OrchestrationParallelRuntime,'_merge',lambda *_:pytest.fail('native refusal entered Python'))
        with pytest.raises(FlowExecutionError,match='injected native merge refusal'):runtime.run('fanout','seed')
        assert not outcomes.failures
        assert events==[dict(type='parallel_start',node_id='fanout',branches=2)]
    finally:graph.close()
