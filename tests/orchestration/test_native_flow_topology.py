"""Native topology matches the retained oracle on cyclic and disconnected graphs."""
import ctypes as c
import random
import pytest
from lib.identity import PrincipalContext
from lib.orchestration.graph import GraphNavigator,FlowExecutionError
from lib.orchestration.native_flow import _Step
from lib.orchestration.native_flow_contract import SPEC
from tests.orchestration.test_native_flow_engine import native_library as native_library

pytestmark=pytest.mark.unit


@pytest.mark.parametrize('seed',range(12))
def test_native_topology_matches_ordered_python_oracle(seed,native_library):
    rng=random.Random(seed)
    names=[f'n{i}' for i in range(16)]
    nodes={name:dict(id=name,type='role' if i%3 else 'control',
        kind='barrier' if i%5==0 else '') for i,name in enumerate(names)}
    edges=[dict(from_=a,to=b) for a in names for b in names if rng.random()<.09]
    edges=[{'from':row['from_'],'to':row['to']} for row in edges]
    reference=GraphNavigator.from_edges(nodes,edges)
    graph=native_library.open_graph(principal=PrincipalContext.user(subject_id='topology',
        owner_user_id=7,tenant_id='tenant'),task_id=f'topology-{seed}',nodes=nodes,edges=edges)
    try:
        for start in names:
            assert graph.single_next(start)==reference.single_next(start)
            assert graph.reachable(start)==reference.reachable(start)
            assert graph.loop_parts(start)==reference.loop_parts(start)
            body=reference.loop_parts(start)[0]
            assert graph.find_loop_planner(start,body)==reference.find_loop_planner(start,body)
            for target in names:
                assert graph.distance(start,target)==reference.distance(start,target)
                for avoid in ('',start,target,names[-1]):
                    assert graph.can_reach(start,target,avoid=avoid)==reference.can_reach(start,target,avoid=avoid)
        for _ in range(10):
            branches=rng.sample(names,3)
            actual=graph.find_common_barrier(branches)
            expected=reference.find_common_barrier(branches)
            if actual!=expected:
                # The legacy set traversal leaves equal-distance ties undefined.
                assert reference.distance(branches[0],actual)==reference.distance(branches[0],expected)
    finally:graph.close()
    assert not native_library._graphs


def test_cursor_and_query_faults_publish_nothing_and_join_exact_graph(native_library):
    principal=PrincipalContext.user(subject_id='faults',owner_user_id=7)
    nodes={'a':dict(id='a',type='role'),'b':dict(id='b',type='role')}
    edges=[{'from':'a','to':'b'}]
    left=native_library.open_graph(principal=principal,task_id='left',nodes=nodes,edges=edges)
    right=native_library.open_graph(principal=principal,task_id='right',nodes=nodes,edges=edges)
    try:
        with left.cursor('a',stop=None,budget=10) as cursor:
            assert cursor.step().node==0
            with pytest.raises(FlowExecutionError,match='invalid_state'):cursor.step()
            with pytest.raises(FlowExecutionError,match='invalid_input'):cursor.advance('a')
            with pytest.raises(FlowExecutionError,match='busy'):left.close()
            output=_Step(987,987,987)
            with native_library._call() as library:
                assert library.tofu_flow_cursor_next(right.handle,cursor.handle,c.byref(output))==SPEC['statuses']['unknown_handle']
            assert (output.node,output.next,output.action)==(987,987,987)
            cursor.advance('b');assert cursor.step().node==1
            cursor.advance(None);assert cursor.step().action==SPEC['actions']['halt']
        with native_library._call() as library:
            out=(c.c_uint64*1)(987);written=c.c_size_t(987)
            assert library.tofu_flow_query(left.handle,SPEC['queries']['reachable'],0,0,0,None,0,out,1,c.byref(written))==SPEC['statuses']['buffer_small']
            assert out[0]==written.value==987
        assert not left.closed
    finally:left.close();right.close()
    assert not native_library._graphs
