"""Curriculum (R2): probes enter the ordinary intake, bounded and deduplicated."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

USER = 12


@pytest.fixture
def store(monkeypatch):
    from tests.support.evolution_store import install_store
    return install_store(monkeypatch)


def _settle(store, goal_id, episode_id, outcome, *, domain='code',
            command=None, gap_summary='', failed_stage=None):
    from lib import evolution
    from lib.evolution import records
    criteria = ([{'criterion_id': 'c', 'kind': 'test_command',
                  'params': {'command': command}}] if command else [])
    evolution.admit_goal({
        'schema_version': 1, 'goal_id': goal_id,
        'statement': f'goal in {domain}', 'domain': domain,
        'exploratory': not criteria, 'criteria': criteria,
        'budget_envelope': {'max_tokens': 100, 'max_wall_clock_seconds': 10,
                            'max_tool_calls': 1},
        'status': 'draft', 'created_at': 1}, user_id=USER)
    records.put_record(records.NAMESPACE_EPISODES, f'{USER}:{episode_id}', {
        'schema_version': 1, 'episode_id': episode_id, 'goal_id': goal_id,
        'plan_id': 'p', 'selected_path_id': 'a', 'exploratory': not criteria,
        'task_id': f't-{episode_id}', 'status': outcome,
        'created_at': 1, 'updated_at': 2, 'settled_at': 2,
        'actuals': {'tokens': 10, 'wall_clock_seconds': 5, 'tool_calls': 1},
        'stage_receipts': ([{'stage': failed_stage, 'status': 'error'}]
                           if failed_stage else []),
        'verdict': (None if outcome == 'aborted' else
                    {'outcome': outcome, 'assessed_by': 'checker',
                     'criterion_results': []}),
        'learning': {'resource_weight_deltas': [],
                     'cost_actual_vs_estimate': None,
                     'strategy_outcome': None,
                     'capability_gap': ({
                         'gap_id': 'raw', 'summary': gap_summary,
                         'proposed_action': 'add it'} if gap_summary else None)},
    }, kind='episode')


def test_frontier_digest_reads_gaps_hypotheses_antipatterns(store):
    from lib import evolution
    _settle(store, 'g1', 'e1', 'failure', command='true',
            gap_summary='cannot parse tables', failed_stage='parse')
    _settle(store, 'g2', 'e2', 'failure', command='true',
            gap_summary='cannot parse tables', failed_stage='parse')
    evolution.collect_capability_gaps(user_id=USER)
    evolution.distill_strategies(user_id=USER)
    evolution.propose_hypotheses([{
        'domain': 'code', 'feature': 'tool_calls', 'operator': 'gt',
        'threshold': 5, 'direction': 'hurts',
        'statement': 'tool storms hurt'}], user_id=USER)
    digest = evolution.frontier_digest(user_id=USER)
    assert digest['capability_gaps'][0]['samples'] == 2
    assert digest['pending_hypotheses']
    assert digest['anti_patterns'][0]['signature'] == 'failed:parse'


def test_probes_admit_as_bounded_deduplicated_goals(store):
    from lib import evolution
    _settle(store, 'g1', 'e1', 'failure', gap_summary='no ocr capability')
    evolution.collect_capability_gaps(user_id=USER)
    admitted = evolution.propose_probes(user_id=USER, now=100)
    assert len(admitted) == 1
    probe = admitted[0]
    assert probe['exploratory'] is True
    assert probe['criteria'] == []
    assert probe['statement'].startswith('[probe:gap]')
    assert probe['budget_envelope']['max_tokens'] >= 20000
    again = evolution.propose_probes(user_id=USER, now=101)
    assert again == []  # dedup: the same frontier proposes nothing twice


def test_antipattern_retry_inherits_criteria_and_full_rights(store):
    from lib import evolution
    _settle(store, 'g1', 'e1', 'failure', command='pytest -q tests/test_x.py',
            failed_stage='train')
    _settle(store, 'g2', 'e2', 'failure', command='pytest -q tests/test_x.py',
            failed_stage='train')
    evolution.distill_strategies(user_id=USER)
    admitted = evolution.propose_probes(user_id=USER, now=100)
    retries = [p for p in admitted
               if p['statement'].startswith('[probe:anti-pattern]')]
    assert len(retries) == 1
    retry = retries[0]
    assert retry['exploratory'] is False
    assert retry['criteria'][0]['kind'] == 'test_command'
    assert retry['criteria'][0]['params']['command'] == 'pytest -q tests/test_x.py'


def test_open_probe_cap_stops_curriculum(store):
    from lib import evolution
    _settle(store, 'g1', 'e1', 'failure', gap_summary='no ocr')
    evolution.collect_capability_gaps(user_id=USER)
    from lib.evolution import curriculum
    for index in range(curriculum.MAX_OPEN_PROBES):
        evolution.admit_goal({
            'schema_version': 1, 'goal_id': f'probe-manual-{index}',
            'statement': f'[probe:gap] seeded {index}', 'domain': 'general',
            'exploratory': True, 'criteria': [],
            'budget_envelope': {'max_tokens': 1, 'max_wall_clock_seconds': 1,
                                'max_tool_calls': 1},
            'status': 'draft', 'created_at': 1}, user_id=USER)
    assert evolution.propose_probes(user_id=USER) == []


def test_author_seam_refines_but_shape_is_validated(store):
    from lib import evolution
    _settle(store, 'g1', 'e1', 'failure', gap_summary='no ocr')
    evolution.collect_capability_gaps(user_id=USER)

    def fake_author(request):
        assert request['digest']['capability_gaps']
        assert request['candidates']
        return [
            {'domain': 'code',
             'statement': '[probe:gap] Learn OCR via the forged pipeline'},
            {'domain': 'code', 'statement': 'missing marker: rejected'},
            'garbage-entry',
        ]

    admitted = evolution.propose_probes(user_id=USER, author=fake_author,
                                        now=100)
    assert len(admitted) == 1
    assert admitted[0]['statement'].startswith('[probe:gap]')
    assert admitted[0]['domain'] == 'code'
    assert admitted[0]['exploratory'] is True


def test_frontier_band_prioritizes_learnable_over_hopeless(store):
    """A domain at 50% success teaches more than one at 100% failure."""
    from lib import evolution
    from lib.evolution import records
    # 'hopeless' domain: unbroken failures; 'mixed' domain: even odds.
    for index in range(4):
        _settle(store, f'h{index}', f'he{index}', 'failure', command='true',
                domain='hopeless', failed_stage='train')
    for index in range(4):
        _settle(store, f'm{index}', f'me{index}', 'failure', command='true',
                domain='mixed', failed_stage='train')
        _settle(store, f'mw{index}', f'mwe{index}', 'success', command='true',
                domain='mixed')
    evolution.recompute_priors(user_id=USER)
    evolution.distill_strategies(user_id=USER)
    admitted = evolution.propose_probes(user_id=USER, now=100)
    retries = [p for p in admitted
               if p['statement'].startswith('[probe:anti-pattern]')]
    assert retries
    assert retries[0]['domain'] == 'mixed'


def test_probe_flows_through_the_ordinary_loop(store):
    from lib import evolution
    _settle(store, 'g1', 'e1', 'failure', gap_summary='no ocr')
    evolution.collect_capability_gaps(user_id=USER)
    [probe] = evolution.propose_probes(user_id=USER, now=100)
    plan = evolution.build_plan(probe, [{
        'path_id': 'path-a', 'title': 'try the OCR skill',
        'stages': [{'kind': 'execute', 'detail': 'attempt'}],
    }], user_id=USER)
    evolution.save_plan({**plan, 'status': 'confirmed'}, user_id=USER)
    episode = evolution.open_episode(
        goal_id=probe['goal_id'], plan_id=plan['plan_id'],
        selected_path_id='path-a', task_id='task-probe', user_id=USER)
    with pytest.raises(evolution.EvolutionStateError):
        # Exploratory probe: no strategy minting even on a win.
        evolution.settle_episode(
            episode['episode_id'], user_id=USER, status='success',
            actuals={'tokens': 5, 'wall_clock_seconds': 5, 'tool_calls': 1},
            verdict={'outcome': 'success', 'assessed_by': 'human',
                     'criterion_results': []},
            learning={'resource_weight_deltas': [],
                      'cost_actual_vs_estimate': None,
                      'strategy_outcome': {'distilled_template_id': 't',
                                           'anti_pattern': None},
                      'capability_gap': None})
    settled = evolution.settle_episode(
        episode['episode_id'], user_id=USER, status='success',
        actuals={'tokens': 5, 'wall_clock_seconds': 5, 'tool_calls': 1},
        verdict={'outcome': 'success', 'assessed_by': 'human',
                 'criterion_results': []},
        learning={'resource_weight_deltas': [
            {'ref_id': 'paper:x', 'indexer': 'academic', 'delta': 0.1,
             'basis': 'cited_in_success'}],
            'cost_actual_vs_estimate': None, 'strategy_outcome': None,
            'capability_gap': None})
    assert settled['status'] == 'success'
