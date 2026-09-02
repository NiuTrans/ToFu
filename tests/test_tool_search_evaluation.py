import pytest

from evaluations.tool_search.dataset import CASES, CATALOG, SEARCH_TEXT_BY_NAME
from evaluations.tool_search.evaluation import (
    apply_tool_selections,
    apply_model_queries,
    evaluate_retrieval,
    flatten_episodes,
    merge_simulated_users,
)
from lib.tools.gateway import search_executable_catalog
from evaluations.tool_search.qwen_reference import qwen_keyword_search


pytestmark = pytest.mark.unit


def test_simulator_cannot_mutate_oracle_target():
    cases = merge_simulated_users(CASES, {'cases': [{
        'id': CASES[0]['id'], 'target': 'run_command',
        'utterances': ['A fresh user request.'],
    }]})
    assert cases[0]['target'] == CASES[0]['target']
    assert 'A fresh user request.' in cases[0]['utterances']


def test_malformed_agent_decision_falls_back_to_user_query():
    episodes = flatten_episodes(merge_simulated_users(CASES[:1], {}))
    decided = apply_model_queries(episodes, {'decisions': [{
        'episode_id': episodes[0]['episode_id'], 'action': 'direct',
    }]})
    assert decided[0]['action'] == 'search'
    assert decided[0]['query'] == decided[0]['utterance']


def test_evaluation_has_stable_exact_ground_truth_metrics():
    cases = merge_simulated_users(CASES[:1], {'cases': [{
        'id': CASES[0]['id'], 'utterances': ['search file contents'],
    }]})
    report = evaluate_retrieval(
        CATALOG, flatten_episodes(cases), search=search_executable_catalog,
        search_text_by_name=SEARCH_TEXT_BY_NAME)
    assert report['episodes'] == 3
    assert 0 <= report['recall_at_1'] <= report['recall_at_5'] <= 1
    assert report['rows'][0]['target'] == 'grep_search'


def test_selection_scoring_uses_oracle_not_model_verdict():
    report = {'rows': [{
        'episode_id': 'x:0', 'target': 'right', 'matches': ['right', 'wrong'],
    }]}
    scored = apply_tool_selections(report, {'selections': [{
        'episode_id': 'x:0', 'tool_name': 'wrong', 'correct': True,
    }]})
    assert scored['end_to_end_accuracy'] == 0
    assert scored['rows'][0]['selection_correct'] is False


def test_direct_hidden_name_is_scored_without_search_round():
    report = evaluate_retrieval(
        CATALOG, [{
            'episode_id': 'direct:0', 'case_id': 'direct',
            'target': 'apply_diff', 'utterance': 'call apply_diff',
            'action': 'direct', 'direct_name': 'apply_diff', 'query': '',
        }], search=search_executable_catalog)
    assert report['direct_calls'] == 1
    assert report['direct_accuracy'] == 1
    assert report['recall_at_1'] == 1


def test_qwen_reference_arm_is_deterministic():
    result = qwen_keyword_search(
        CATALOG, 'cancel recurring reminder', limit=5,
        search_text_by_name=SEARCH_TEXT_BY_NAME)
    assert result['items'][0]['name'] == 'scheduler_cancel'
