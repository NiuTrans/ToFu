"""Search evidence covers misses, recovery, both entry points and sink failure."""
import json

import pytest

from lib.tasks_pkg import tool_search_outcome as evidence
from lib.tasks_pkg.handlers import tool_gateway as handler
from scripts.report_tool_search_outcomes import summarize

pytestmark = pytest.mark.unit


def tool(name):
    return {'type': 'function', 'function': {
        'name': name, 'description': name,
        'parameters': {'type': 'object', 'properties': {}}}}


@pytest.fixture
def records(monkeypatch):
    rows = []
    monkeypatch.setattr(evidence, 'audit_log',
                        lambda event, **detail: rows.append({'event': event, **detail}))
    monkeypatch.setattr(handler, '_finalize', lambda *args, **kwargs: None)
    return rows


def search(task, query, **arguments):
    return handler.handle_search_tools(
        task, {}, 'search_tools', 'search-1', {'query': query, **arguments},
        32, {'llmRound': 32}, {}, None, False)


def test_precise_missing_name_retains_attribution_without_claiming_pollution(records):
    task = {'id': 'task-1', 'convId': 'conv-1', '_userId': 7, 'model': 'fixture',
            '_attemptId': 'attempt-1', '_executable_tool_catalog': [tool('read_files')]}
    search(task, 'steer_agent_run')
    row, = records
    assert row['outcome'] == 'empty' and row['reason'] == 'exact_name_absent'
    assert row['query'] == 'steer_agent_run' and row['query_is_tool_name']
    assert row['query_name_in_catalog'] is False
    assert (row['conv_id'], row['task_id'], row['attempt_id'], row['llm_round']) == (
        'conv-1', 'task-1', 'attempt-1', 32)
    assert row['user_id'] == '7'
    assert len(row['catalog_names_sha256']) == 64


def test_visible_name_is_not_reported_as_absent_and_success_is_denominator(records):
    task = {'_executable_tool_catalog': [tool('read_files'), tool('write_files')],
            '_tool_schema': [tool('read_files')]}
    search(task, 'read_files')
    search(task, 'write_files')
    assert [row['reason'] for row in records] == ['already_visible', 'matched']
    assert records[1]['result_count'] == 1


def test_invalid_query_and_fail_open_are_errors_even_with_recovery_results(records, monkeypatch):
    task = {'_executable_tool_catalog': [tool('read_files')]}
    search(task, 'x' * 513)
    assert records[0]['reason'] == 'invalid_query'
    assert records[0]['query_truncated'] and len(records[0]['query']) == 512
    def fail(*args, **kwargs):
        raise RuntimeError('private error detail')
    monkeypatch.setattr(handler, 'search_executable_catalog', fail)
    _, content, _ = search(task, 'read')
    assert json.loads(content)['fail_open'] is True
    row = records[1]
    assert row['outcome'] == 'error' and row['reason'] == 'retrieval_exception'
    assert row['result_count'] == 1 and row['exception_type'] == 'RuntimeError'
    assert 'private error detail' not in json.dumps(row)


def test_program_search_uses_same_evidence(records):
    task = {'model': 'fixture', '_executable_tool_catalog': [tool('read_files')]}
    _, content, _ = handler.handle_execute_tools(
        task, {}, 'execute_tools', 'program-1', {
            'program': 'return catalog.search("steer_agent_run");'},
        1, {'llmRound': 32}, {'tools': {'programmaticCalling': 'on'}}, None, False)
    assert json.loads(content)['status'] == 'ok'
    row, = records
    assert row['source'] == 'catalog.search' and row['llm_round'] == 32
    assert row['reason'] == 'exact_name_absent'


def test_program_retrieval_exception_is_audited_and_still_fails(records, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError('private exception')
    monkeypatch.setattr(handler, 'search_executable_catalog', fail)
    with pytest.raises(RuntimeError, match='private exception'):
        handler.handle_execute_tools(
            {'model': 'fixture', '_executable_tool_catalog': [tool('read_files')]},
            {}, 'execute_tools', 'program-1', {'program': 'return catalog.search("read");'},
            1, {'llmRound': 32}, {'tools': {'programmaticCalling': 'on'}}, None, False)
    row, = records
    assert row['source'] == 'catalog.search' and row['reason'] == 'retrieval_exception'
    assert row['exception_type'] == 'RuntimeError' and row['fail_open'] is False


def test_sink_failure_never_changes_search(monkeypatch):
    monkeypatch.setattr(handler, '_finalize', lambda *args, **kwargs: None)
    def fail(*args, **kwargs):
        raise OSError('sink unavailable')
    monkeypatch.setattr(evidence, 'audit_log', fail)
    _, content, _ = search({'_executable_tool_catalog': [tool('read_files')]}, 'read_files')
    assert json.loads(content)['items'][0]['name'] == 'read_files'


@pytest.mark.parametrize(('result', 'names', 'visible', 'arguments', 'reason'), [
    ({'status': 'ok', 'items': []}, [], [], {}, 'catalog_empty'),
    ({'status': 'ok', 'items': []}, ['read_files', 'search_tools'],
     ['read_files'], {}, 'all_tools_disclosed'),
    ({'status': 'ok', 'items': []}, ['read_files'], [], {}, 'no_match'),
    ({'status': 'ok', 'items': [], 'available_namespaces': ['general']},
     ['read_files'], [], {'namespace': 'wrong'}, 'namespace_absent'),
    ({'status': 'ok', 'items': []}, ['read_files'], [],
     {'query': 'read_files', 'namespace': 'other'}, 'namespace_filtered_name'),
    ({'status': 'ok', 'items': [], 'total': 2}, ['read_files'], [],
     {'cursor': 'end'}, 'page_exhausted'),
    ({'status': 'ok', 'items': [], 'total': 2}, ['read_files'], [],
     {'query': 'read_files', 'namespace': 'general', 'cursor': 'end'}, 'page_exhausted'),
    ({'status': 'ok', 'items': [], 'capability_availability': [
        {'reason_code': 'browser_extension_disconnected'}]}, ['read_files'], [],
     {}, 'browser_extension_disconnected'),
])
def test_empty_results_have_factual_reasons(records, result, names, visible, arguments, reason):
    evidence.audit_search_outcome({}, arguments, result, source='search_tools',
                                  call_id='call', llm_round=0,
                                  catalog_names=names, visible_names=visible)
    assert records[0]['outcome'] == 'empty'
    assert records[0]['reason'] == reason


def test_report_streams_rotations_filters_and_bounds_failures(tmp_path):
    paths = [tmp_path / 'audit.log.1', tmp_path / 'audit.log']
    rows = [{'event': 'tool_search_outcome', 'model': 'fixture', 'source': 'search_tools',
             'outcome': 'empty', 'reason': 'exact_name_absent', 'query': 'steer_agent_run'}] * 25
    paths[0].write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
    paths[1].write_text('broken\n' + 'x' * 40000 + '\n' + json.dumps({
        'event': 'tool_search_outcome', 'model': 'fixture', 'outcome': 'matched'}) + '\n')
    result = summarize(paths)
    assert (result['observations'], result['empty'], result['matched']) == (26, 25, 1)
    assert result['malformed_lines'] == 2
    assert len(result['recent_unsuccessful']) == 20
    assert summarize(paths, query='steer_agent_run')['observations'] == 25
    assert summarize(paths, model='different')['observations'] == 0


def test_visibility_counts_match_case_insensitive_disjoint_search_sets(records):
    evidence.audit_search_outcome({}, {'query': 'scale'}, {
        'status': 'ok', 'items': [], 'already_visible': 'scale'},
        source='search_tools', call_id='call', llm_round=0,
        catalog_names=['scale', 'read_files', 'write_files'],
        visible_names=['SCALE'], disclosed_names=['scale', 'READ_FILES'])
    row, = records
    assert (row['visible_count'], row['disclosed_count'], row['hidden_count']) == (1, 1, 1)
    assert row['query_is_tool_name'] and row['query_name_in_catalog']
    assert row['resolved_name'] == 'scale'


def test_redundant_repeated_search_keeps_canonical_name_and_repeat_count(records):
    task = {'_executable_tool_catalog': [tool('read_files')],
            '_tool_schema': [tool('read_files')]}
    search(task, '+read_files')
    search(task, '+read_files')
    assert records[1]['resolved_name'] == 'read_files'
    assert records[1]['repeated_query'] == 2
    assert records[1]['reason'] == 'already_visible'


def test_report_rates_query_frequency_and_timestamp_order_ignore_duplicate_files(tmp_path):
    older = tmp_path / 'audit.log.1'
    newer = tmp_path / 'audit.log'
    def row(timestamp, **values):
        return dict(event='tool_search_outcome', model='fixture', source='search_tools',
                    outcome='empty', reason='exact_name_absent', query='steer_agent_run',
                    timestamp=timestamp, **values)
    rows = [row('2026-10-12T01:00:00Z'), row('2026-10-12T02:00:00Z', repeated_query=2)]
    older.write_text('\n'.join(json.dumps(value) for value in rows) + '\n')
    newest = dict(rows[0], timestamp='2026-10-13T01:00:00Z',
                  reason='already_visible', query='+read_files')
    matched = dict(newest, outcome='matched', reason='matched', query='write_files')
    error = dict(newest, outcome='error', reason='retrieval_exception', query='read')
    newer.write_text('\n'.join(json.dumps(value) for value in [newest, matched, error]) + '\n')
    alias = tmp_path / 'audit.alias'
    alias.symlink_to(newer)
    result = summarize([newer, older, newer, alias])
    assert result['observations'] == 5
    model, = result['by_model']
    assert (model['matched'], model['empty'], model['errors'], model['redundant_lookups']) == (1, 3, 1, 1)
    assert (model['match_rate'], model['empty_rate'], model['error_rate']) == (0.2, 0.6, 0.2)
    assert result['frequent_unsuccessful_queries'][0]['query'] == 'steer_agent_run'
    assert result['frequent_unsuccessful_queries'][0]['count'] == 2
    assert result['repeated_searches'] == 1
    times = [value['timestamp'] for value in result['recent_unsuccessful']]
    assert times == sorted(times)


def test_recent_window_keeps_newest_even_when_old_rotation_is_read_last(tmp_path):
    newer = tmp_path / 'newer'
    older = tmp_path / 'older'
    rows = [dict(event='tool_search_outcome', outcome='empty', reason='no_match',
                 timestamp=f'2026-10-13T00:00:{index:02d}Z') for index in range(25)]
    newer.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
    older.write_text(json.dumps(dict(rows[0], timestamp='2026-10-12T00:00:00Z')) + '\n')
    result = summarize([newer, older])
    assert len(result['recent_unsuccessful']) == 20
    assert result['recent_unsuccessful'][0]['timestamp'] == rows[5]['timestamp']


def test_report_high_cardinality_and_deep_invalid_json_keep_totals_and_bounded_output(tmp_path):
    path = tmp_path / 'audit.log'
    rows = [dict(event='tool_search_outcome', outcome='empty', reason='no_match',
                 model=f'model-{index}', query=f'query-{index}') for index in range(1050)]
    path.write_text('[' * 2000 + ']' * 1999 + '\n' + '\n'.join(json.dumps(row) for row in rows) + '\n')
    result = summarize([path])
    assert result['observations'] == 1050 and result['malformed_lines'] == 1
    assert result['aggregation_overflow'] == dict(groups=True, models=True, queries=True)
    assert len(result['groups']) == 50 and len(result['by_model']) == 50
    assert len(result['frequent_unsuccessful_queries']) == 20
