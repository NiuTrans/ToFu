"""LLM research evaluation is independent, costed, and disagreement-aware."""

from __future__ import annotations

import json
import threading

import pytest

pytestmark = pytest.mark.unit


def _result():
    return {
        'corpus_arxiv_ids': ['2501.00001', '2501.00002'],
        'survey_md': '# Survey\nCross-paper synthesis.',
        'open_gaps': {'open_gaps': [{'id': 'g1', 'gap': 'testable gap'}]},
        'accepted': [],
        'rejected': [{'title': 'Weak idea', 'overall': 3.0,
                      'reject_stage': 'rubric'}],
        'threshold': 4.0,
        'gate_reached': 'rubric',
    }


def _judgement(score, worth, *, model='judge-model', assessments=None):
    from lib.research.evaluation import EVALUATION_AXES

    return {
        'scores': {axis: score for axis in EVALUATION_AXES},
        'worth_following_up': worth,
        'confidence': 0.8,
        'strengths': ['selective gate'],
        'failure_modes': ['thin_matrix'],
        'recommended_changes': [{
            'target': 'survey', 'priority': 'high',
            'change': 'Require a method-matrix row for every paper.',
            'evidence': 'The method matrix is empty.',
        }],
        'idea_assessments': assessments if assessments is not None else [],
        'verdict': f'{model}: strict result',
    }


def _fake_dispatch(payloads, seen):
    queue = list(payloads)

    def dispatch(messages, *, on_content, **kwargs):
        seen.append((messages, kwargs))
        body = queue.pop(0)
        if isinstance(body, dict):
            body = json.dumps(body)
        on_content(body)
        usage = {
            'prompt_tokens': 100, 'completion_tokens': 20,
            '_dispatch': {'model': f'judge-{len(seen)}', 'provider_id': 'test'},
        }
        return {'content': body}, 'stop', usage

    return dispatch


def test_two_independent_judges_are_medianed_and_costed(monkeypatch):
    import lib.research.evaluation as ev

    seen = []
    monkeypatch.setattr(ev, 'dispatch_stream', _fake_dispatch([
        _judgement(3.0, True), _judgement(4.0, True),
    ], seen))
    got = ev.evaluate_research_result('direction', _result())

    assert got['ok'] is True and got['judge_count'] == 2
    assert got['attempted_judges'] == 2 and got['tiebreaker_used'] is False
    assert got['overall_score'] == 3.5
    assert got['worth_following_up'] is True
    assert got['consensus'] == 'unanimous'
    assert got['usage']['calls'] == 2
    assert got['usage']['prompt_tokens'] == 200
    assert all(call[1]['temperature'] == 0.0 for call in seen)
    assert all(call[1]['thinking_enabled'] is False for call in seen)
    assert all('tools' not in call[1] for call in seen)


def test_split_verdict_uses_third_judge_and_majority(monkeypatch):
    import lib.research.evaluation as ev

    seen = []
    monkeypatch.setattr(ev, 'dispatch_stream', _fake_dispatch([
        _judgement(2.0, False),
        _judgement(4.0, True),
        _judgement(3.0, False),
    ], seen))
    got = ev.evaluate_research_result('direction', _result())

    assert len(seen) == 3 and got['tiebreaker_used'] is True
    assert got['judge_count'] == 3 and got['consensus'] == 'majority'
    assert got['overall_score'] == 3.0
    assert got['worth_following_up'] is False
    assert got['disagreement']['max_axis_delta'] == 2.0


def test_invalid_judges_return_explicit_degraded_result(monkeypatch):
    import lib.research.evaluation as ev

    seen = []
    monkeypatch.setattr(ev, 'dispatch_stream', _fake_dispatch([
        'not JSON', '{"scores": {}}',
    ], seen))
    got = ev.evaluate_research_result('direction', _result())

    assert got['ok'] is False and got['degraded'] is True
    assert got['judge_count'] == 0 and got['attempted_judges'] == 2
    assert got['overall_score'] is None
    assert len(got['errors']) == 2
    assert got['usage']['calls'] == 2


def test_primary_judges_overlap_with_a_two_worker_ceiling(monkeypatch):
    import lib.research.evaluation as ev

    monkeypatch.setenv('TOFU_PRODUCTION_LLM_FANOUT', '2')
    monkeypatch.setenv('TOFU_PRODUCTION_LLM_MAX_429_ATTEMPTS', '3')
    lock = threading.Lock()
    first_pair = threading.Barrier(2)
    state = {'issued': 0, 'active': 0, 'peak': 0}

    def dispatch(messages, *, on_content, **kwargs):
        from lib.paper.agent_loop_policy import PAPER_AGENT_ROUTE_MAX_RETRIES
        assert kwargs['max_retries'] == PAPER_AGENT_ROUTE_MAX_RETRIES
        assert kwargs['max_429_attempts'] == 3
        with lock:
            index = state['issued']
            state['issued'] += 1
            state['active'] += 1
            state['peak'] = max(state['peak'], state['active'])
        try:
            if index < 2:
                first_pair.wait(timeout=2)
            body = json.dumps(_judgement(3.0 + index / 10, True))
            on_content(body)
            return {'content': body}, 'stop', {
                'prompt_tokens': 100, 'completion_tokens': 20,
                '_dispatch': {'model': f'judge-{index}', 'provider_id': 'test'},
            }
        finally:
            with lock:
                state['active'] -= 1

    monkeypatch.setattr(ev, 'dispatch_stream', dispatch)
    got = ev.evaluate_research_result('direction', _result(), judges=3)

    assert got['judge_count'] == 3 and got['attempted_judges'] == 3
    assert got['usage']['calls'] == 3
    assert state == {'issued': 3, 'active': 0, 'peak': 2}


def test_text_false_is_not_coerced_to_true_and_unknown_text_is_invalid():
    import lib.research.evaluation as ev

    raw = _judgement(3.0, 'false')
    assert ev._clean_judgement(raw)['worth_following_up'] is False
    raw['worth_following_up'] = 'maybe'
    assert ev._clean_judgement(raw) is None

def test_idea_assessments_are_cleaned_and_medianed_per_idea(monkeypatch):
    import lib.research.evaluation as ev

    seen = []
    monkeypatch.setattr(ev, 'dispatch_stream', _fake_dispatch([
        _judgement(4.0, True, assessments=[
            {'idea': 'Idea A', 'score': 4.5, 'verdict': 'mechanism is causal',
             'main_risk': 'thin baseline'},
            {'idea': 'Idea B', 'score': 2.0, 'verdict': 'A+B relabel',
             'main_risk': 'no new invariant'},
            {'idea': '', 'score': 3.0},
            {'idea': 'No score'},
            {'idea': 'Bad score', 'score': 9},
        ]),
        _judgement(4.0, True, assessments=[
            {'idea': 'Idea A', 'score': 3.5, 'verdict': 'mechanism is causal',
             'main_risk': 'data budget confound'},
        ]),
    ], seen))
    got = ev.evaluate_research_result('direction', _result())

    assert got['ok'] is True and got['schema_version'] == 2
    assessments = got['idea_assessments']
    assert [row['idea'] for row in assessments] == ['Idea A', 'Idea B']
    idea_a = assessments[0]
    assert idea_a['score'] == 4.0 and idea_a['judge_count'] == 2
    assert idea_a['verdicts'] == ['mechanism is causal']
    assert idea_a['main_risks'] == ['thin baseline', 'data budget confound']
    idea_b = assessments[1]
    assert idea_b['score'] == 2.0 and idea_b['judge_count'] == 1
    # Judge payloads stay intact for audit, each with cleaned assessments.
    assert got['judges'][0]['idea_assessments'][0]['idea'] == 'Idea A'
    assert len(got['judges'][0]['idea_assessments']) == 2


def test_idea_assessments_absent_in_old_judges_yields_empty_list(monkeypatch):
    import lib.research.evaluation as ev

    def legacy_judgement():
        row = _judgement(4.0, True)
        del row['idea_assessments']
        return row

    seen = []
    monkeypatch.setattr(ev, 'dispatch_stream', _fake_dispatch([
        legacy_judgement(), legacy_judgement(),
    ], seen))
    got = ev.evaluate_research_result('direction', _result())

    assert got['ok'] is True and got['idea_assessments'] == []
