"""Contracts for the orchestration engine's pure graph-navigation boundary."""

from pathlib import Path

import pytest

from lib.orchestration_engine import FlowExecutionError as EngineFlowError
from lib.orchestration_graph import FlowExecutionError, GraphNavigator

pytestmark = pytest.mark.unit


def _navigator(nodes: list[dict], edges: list[tuple[str, str]]) -> GraphNavigator:
    node_map = {node['id']: node for node in nodes}
    return GraphNavigator.from_edges(
        node_map,
        [{'from': source, 'to': target} for source, target in edges],
    )


def test_start_label_and_single_successor_queries():
    navigator = _navigator(
        [
            {'id': 'start', 'kind': 'start'},
            {'id': 'named', 'name': 'Named node'},
            {'id': 'role', 'role': 'writer'},
            {'id': 'kind', 'kind': 'stop'},
            {'id': 'bare'},
        ],
        [('start', 'named'), ('start', 'role')],
    )

    assert navigator.find_start() == 'start'
    assert navigator.single_next('start') == 'named'
    assert navigator.single_next('bare') is None
    assert navigator.node_label('named') == 'Named node'
    assert navigator.node_label('role') == 'writer'
    assert navigator.node_label('kind') == 'stop'
    assert navigator.node_label('bare') == 'bare'


def test_adjacency_builder_preserves_order_and_ignores_unknown_endpoints():
    nodes = {'a': {'id': 'a'}, 'b': {'id': 'b'}, 'c': {'id': 'c'}}
    navigator = GraphNavigator.from_edges(
        nodes,
        [
            {'from': 'a', 'to': 'c'},
            {'from': 'a', 'to': 'b'},
            {'from': 'missing', 'to': 'b'},
            {'from': 'a', 'to': 'missing'},
        ],
    )

    assert navigator.fwd == {'a': ['c', 'b'], 'b': [], 'c': []}
    assert navigator.rev == {'a': [], 'b': ['a'], 'c': ['a']}


def test_start_falls_back_to_source_and_uses_public_execution_error():
    source_graph = _navigator(
        [{'id': 'source'}, {'id': 'target'}],
        [('source', 'target')],
    )
    cyclic_graph = _navigator(
        [{'id': 'left'}, {'id': 'right'}],
        [('left', 'right'), ('right', 'left')],
    )

    assert source_graph.find_start() == 'source'
    with pytest.raises(FlowExecutionError, match='no start node'):
        cyclic_graph.find_start()
    assert EngineFlowError is FlowExecutionError


def test_loop_body_exit_and_external_planner_are_topology_derived():
    navigator = _navigator(
        [
            {'id': 'start', 'kind': 'start'},
            {'id': 'planner', 'type': 'role', 'role': 'planner'},
            {'id': 'loop', 'kind': 'loop'},
            {'id': 'worker', 'type': 'role', 'role': 'worker'},
            {'id': 'critic', 'type': 'role', 'role': 'critic'},
            {'id': 'exit', 'kind': 'stop'},
        ],
        [
            ('start', 'planner'),
            ('planner', 'loop'),
            ('loop', 'worker'),
            ('worker', 'critic'),
            ('critic', 'loop'),
            ('loop', 'exit'),
        ],
    )

    assert navigator.loop_parts('loop') == ('worker', 'exit')
    assert navigator.find_loop_planner('loop', 'worker') == 'planner'


def test_common_barrier_prefers_nearest_barrier_and_has_safe_fallbacks():
    navigator = _navigator(
        [
            {'id': 'left'},
            {'id': 'right'},
            {'id': 'right_hop'},
            {'id': 'near', 'kind': 'barrier'},
            {'id': 'far', 'kind': 'barrier'},
            {'id': 'stop', 'kind': 'stop'},
            {'id': 'isolated'},
        ],
        [
            ('left', 'near'),
            ('right', 'right_hop'),
            ('right_hop', 'near'),
            ('near', 'far'),
            ('far', 'stop'),
        ],
    )

    assert navigator.find_common_barrier(['left', 'right']) == 'near'
    assert navigator.find_common_barrier(['near', 'far']) == 'far'
    assert navigator.find_common_barrier(['left', 'isolated']) is None
    assert navigator.find_common_barrier([]) is None


def test_reachability_avoidance_and_distance_contracts():
    navigator = _navigator(
        [{'id': node_id} for node_id in ('a', 'b', 'c', 'd')],
        [('a', 'b'), ('b', 'c'), ('c', 'a')],
    )

    assert navigator.reachable('a') == {'a', 'b', 'c'}
    assert navigator.can_reach('a', 'c') is True
    assert navigator.can_reach('a', 'c', avoid='b') is False
    assert navigator.can_reach('a', 'a', avoid='a') is True
    assert navigator.distance('a', 'c') == 2
    assert navigator.distance('a', 'd') == 1 << 30


def test_engine_delegates_topology_without_reintroducing_implementation():
    root = Path(__file__).resolve().parents[1]
    engine = (root / 'lib' / 'orchestration_engine.py').read_text()
    graph = (root / 'lib' / 'orchestration_graph.py').read_text()

    assert 'from lib.orchestration_graph import' in engine
    assert 'class GraphNavigator' not in engine
    assert 'self._nav = GraphNavigator.from_edges(' in engine
    assert 'class GraphNavigator' in graph
    assert engine.count('\n') < 1700
