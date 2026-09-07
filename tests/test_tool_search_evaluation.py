import pytest
import json

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


def test_browser_namespace_finds_server_download_from_original_chinese_intent():
    namespace_by_name = {
        tool['function']['name']: (
            'browser' if tool['function']['name'].startswith('browser_')
            else 'general')
        for tool in CATALOG
    }
    result = search_executable_catalog(
        CATALOG,
        '把浏览器页面里的最新版压缩包下载到服务器本地',
        namespace='browser',
        limit=5,
        namespace_by_name=namespace_by_name,
        search_text_by_name=SEARCH_TEXT_BY_NAME,
    )
    assert result['items'][0]['name'] == 'browser_download_url_to_server'
    assert result['items'][0]['detail_level'] == 'compact'


def test_legacy_download_name_resolves_to_one_canonical_browser_tool():
    result = search_executable_catalog(
        CATALOG, 'download_url_to_server', limit=3,
        search_text_by_name=SEARCH_TEXT_BY_NAME)
    assert result['resolved_name'] == 'browser_download_url_to_server'
    assert result['items'][0]['name'] == 'browser_download_url_to_server'
    assert len(result['items']) == 1
    assert 'canonical' in result['notice']


def test_tool_search_result_is_compact_and_hard_bounded():
    from lib.tools.gateway import LOCAL_TOOL_SEARCH_MAX_RESULT_CHARS

    contracts = {
        tool['function']['name']: {
            'contractVersion': 2,
            'arguments_schema': tool['function']['parameters'],
            'help': 'full help ' * 5000,
            'permission': 'read',
            'idempotency': 'read_only',
            'ptcEligible': False,
            'errors': [{
                'code': f'error_{index}',
                'message': 'very long diagnostic ' * 100,
            } for index in range(30)],
        }
        for tool in CATALOG
    }
    broad = search_executable_catalog(
        CATALOG, 'search create browser file tool', limit=20,
        search_text_by_name=SEARCH_TEXT_BY_NAME,
        contract_documents_by_name=contracts)
    payload = json.dumps(broad, ensure_ascii=False, separators=(',', ':'))
    assert len(payload) <= LOCAL_TOOL_SEARCH_MAX_RESULT_CHARS
    assert all('help' not in item and 'errors' not in item
               for item in broad['items'])
    assert all(len(item['description']) <= 240 for item in broad['items'])

    exact = search_executable_catalog(
        CATALOG, 'browser_download_url_to_server', limit=3,
        search_text_by_name=SEARCH_TEXT_BY_NAME,
        contract_documents_by_name=contracts)
    assert exact['items'][0]['detail_level'] == 'exact'
    assert len(exact['items'][0]['help']) <= 1200
    assert len(exact['items'][0]['errors']) <= 8


def test_evaluation_reports_original_intent_and_retrieval_latency():
    episodes = flatten_episodes(merge_simulated_users(
        [next(case for case in CASES
              if case['id'] == 'browser_server_download')], {}))
    decided = apply_model_queries(episodes, {'decisions': [{
        'episode_id': episodes[0]['episode_id'],
        'action': 'search',
        'query': 'download save archive server browser',
    }]})
    report = evaluate_retrieval(
        CATALOG, decided, search=search_executable_catalog,
        search_text_by_name=SEARCH_TEXT_BY_NAME)
    assert report['original_intent_recall_at_5'] == 1
    assert report['retrieval_calls'] == len(decided) + 1
    assert report['search_latency_ms_p95'] >= 0
    assert report['rows'][0]['original_query'] == episodes[0]['utterance']
