"""Behavior probes for offline research suggestions; no production admission."""
from copy import deepcopy

import pytest

from evaluations.evolution.decision_aids import (
    compile_graph, digest, evaluate, full_scan_impact, indexed_impact,
    prerequisite_fold, prerequisite_full_scan,
)

pytestmark = pytest.mark.unit
SCOPE = {'principal': 'controlled-owner', 'namespace': 'controlled-study'}
OLD, NEW = 'a'*64, 'b'*64


@pytest.fixture
def graph():
    def node(dependencies):
        return {'scope': SCOPE, 'dependencies': dependencies, 'outcome': 'unknown'}
    def edge(key, role='support'):
        return {'id': key, 'role': role}
    return {'scope': SCOPE,
            'sources': {key: {'scope': SCOPE, 'generation': OLD} for key in ['usage', 'era', 'unrelated']},
            'claims': {'whole-goal': node([edge('cost-complete'), edge('era-claim')]),
                       'era-claim': node([edge('era', 'context')]),
                       'cost-complete': node([edge('usage')])}}


def change(source='usage', **kwargs):
    return {'scope': SCOPE, 'source_id': source, 'previous': OLD, 'current': NEW, **kwargs}


@pytest.mark.parametrize('source,current,expected', [
    ('usage', NEW, ['cost-complete', 'whole-goal']),
    ('era', NEW, ['era-claim', 'whole-goal']),
    ('unrelated', NEW, []), ('usage', OLD, []),
])
def test_declared_impact_matches_full_scan_and_preserves_unknowns(graph, source, current, expected):
    original = deepcopy(graph); index = compile_graph(graph, scope=SCOPE)
    for invoke in [full_scan_impact, indexed_impact]:
        actual = invoke(index, change(source, current=current))
        assert actual['suggested_review'] == expected
        assert actual['execution_authorized'] is False
        assert actual['verified_unaffected'] == []
        assert actual['semantic_coverage'] == 'unverified'
        assert actual['historical_claims_sha256'] == digest(graph['claims'])
    assert graph == original


@pytest.mark.parametrize('scope', [{'principal': 'foreign', 'namespace': 'controlled-study'},
                                   {'principal': 'controlled-owner', 'namespace': 'foreign'}])
def test_foreign_events_refuse_before_suggestions(graph, scope):
    index = compile_graph(graph, scope=SCOPE)
    for invoke in [full_scan_impact, indexed_impact]:
        with pytest.raises(ValueError):
            invoke(index, change(scope=scope))


@pytest.mark.parametrize('failure', ['missing', 'cycle', 'duplicate', 'collision'])
def test_invalid_graph_refuses(graph, failure):
    if failure == 'missing':
        graph['claims']['era-claim']['dependencies'][0]['id'] = 'unrecorded'
    elif failure == 'cycle':
        graph['claims']['era-claim']['dependencies'][0]['id'] = 'whole-goal'
    elif failure == 'duplicate':
        graph['claims']['era-claim']['dependencies'] *= 2
    else:
        graph['sources']['era-claim'] = {'scope': SCOPE, 'generation': OLD}
    with pytest.raises(ValueError):
        compile_graph(graph, scope=SCOPE)


def test_missing_semantic_edge_is_an_observed_candidate_limitation(graph):
    graph['claims']['era-claim']['dependencies'] = []
    index = compile_graph(graph, scope=SCOPE)
    actual = indexed_impact(index, change('era'))
    # A named-node validator cannot find an unstated true dependency. Retain
    # this counterexample instead of equating graph consistency with coverage.
    semantic_expected = ['era-claim', 'whole-goal']
    assert actual['suggested_review'] != semantic_expected
    assert actual['verified_unaffected'] == []
    assert actual['semantic_coverage'] == 'unverified'


@pytest.fixture
def branch():
    return {'scope': SCOPE, 'requirements': {'invoice': OLD}, 'expires_at': 100,
            'cancelled': False, 'hypothesis_assessment': {'problem': 'unknown', 'net_value': 'unknown'}}


def observation(event_id='available', **kwargs):
    return {'scope': SCOPE, 'event_id': event_id, 'key': 'invoice', 'generation': OLD,
            'version': 1, 'status': 'available', 'complete': True, 'value': True, **kwargs}


@pytest.mark.parametrize('scenario', ['valid', 'revoked', 'duplicate', 'out_of_order', 'partial',
                                      'cancelled', 'expired', 'wrong_generation', 'missing'])
def test_prerequisite_trace_equivalence_without_authority(branch, scenario):
    event = observation(); events = [event]; now = 50
    if scenario == 'revoked':
        events += [observation('revocation', version=2, status='revoked')]
    elif scenario == 'duplicate':
        events += [deepcopy(event)]
    elif scenario == 'out_of_order':
        events = [observation('revocation', version=2, status='revoked'), event]
    elif scenario == 'partial':
        events = [observation(complete=False)]
    elif scenario == 'cancelled':
        branch['cancelled'] = True
    elif scenario == 'expired':
        now = 100
    elif scenario == 'wrong_generation':
        events = [observation(generation=NEW)]
    elif scenario == 'missing':
        events = []
    original_branch, original_events = deepcopy(branch), deepcopy(events)
    folded = prerequisite_fold(branch, events, scope=SCOPE, now=now)
    scanned = prerequisite_full_scan(branch, events, scope=SCOPE, now=now)
    assert folded['ready_for_review'] is (scenario in {'valid', 'duplicate'})
    assert {k:v for k,v in folded.items() if k != 'observations_inspected'} == {
        k:v for k,v in scanned.items() if k != 'observations_inspected'}
    assert folded['execution_authorized'] is False
    assert folded['hypothesis_assessment'] == branch['hypothesis_assessment']
    assert branch == original_branch and events == original_events


@pytest.mark.parametrize('events', [[observation(scope={'principal':'other','namespace':'controlled-study'})],
                                   [observation(), observation(status='revoked')],
                                   [observation(), observation('second-id', status='revoked')]])
def test_foreign_or_conflicting_observations_refuse(branch, events):
    for invoke in [prerequisite_fold, prerequisite_full_scan]:
        with pytest.raises(ValueError):
            invoke(branch, events, scope=SCOPE, now=50)


def test_nan_expiry_and_boolean_version_refuse(branch):
    branch['expires_at'] = float('nan')
    with pytest.raises(ValueError):
        prerequisite_fold(branch, [], scope=SCOPE, now=50)
    branch['expires_at'] = 100
    with pytest.raises(ValueError):
        prerequisite_fold(branch, [observation(version=True)], scope=SCOPE, now=50)


def test_large_change_batch_refuses(graph, branch):
    payload = {'scope':SCOPE, 'graph':graph, 'branch':branch,
               'events':[], 'now':50, 'changes':[change()]*33}
    with pytest.raises(ValueError):
        evaluate(payload)
