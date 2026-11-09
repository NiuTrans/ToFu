"""Focused tests for debug/turn_trace_audit.py (read-only; no network, no app).

Both sources are exercised with small synthetic fixtures: the offline
``[TurnTraceSummary]`` log parser over temporary app.log files, and the API
collector over a duck-typed client returning real fold output.
"""

from __future__ import annotations

import json

import pytest

from debug import inspect_conversation as inspector
from debug import turn_trace_audit as audit

pytestmark = pytest.mark.unit

_PREFIX = '2026-10-23 16:{minute:02d}:00 [INFO] lib.tasks_pkg.turn_trace [tofu-agent_1]: [rid:x] '


def _line(minute, task, conv, *, total, wait, admission, retry, spawn, extra=''):
    llm = total - wait - 100
    return (_PREFIX.format(minute=minute)
            + f'[TurnTraceSummary] task={task} conv={conv} status=done totalMs={total} '
            f'llmMs={llm} toolMs=100 waitMs={wait} admissionWaitMs={admission} '
            f'retryWaitMs={retry} compactionMs=0 approvalWaitMs=0 unattributedMs=0 '
            f'ttftMs=50 rounds=2 spawnWaitMs={spawn} flowStepMs=0 coverage=full{extra}\n')


@pytest.fixture
def log_dir(tmp_path):
    (tmp_path / 'app.log.2026-10-22').write_text(
        _line(1, 'task-a', 'conv-1', total=10_000, wait=6_000, admission=5_000,
              retry=1_000, spawn=0)
        + 'noise line without the tag\n'
        # Rebuilt settlement of task-a later in time: the later record wins.
        + _line(2, 'task-a', 'conv-1', total=10_000, wait=7_000, admission=6_000,
                retry=1_000, spawn=0), encoding='utf-8')
    (tmp_path / 'app.log').write_text(
        _line(20, 'task-b', 'conv-1', total=4_000, wait=0, admission=0, retry=0, spawn=0)
        + _line(40, 'task-c', 'conv-2', total=8_000, wait=3_000, admission=0,
                retry=2_000, spawn=1_000, extra=' futureKey=7')
        + _PREFIX.format(minute=41) + '[TurnTraceSummary] task=broken conv=c totalMs=x\n'
        + '\xff garbage bytes are tolerated \n', encoding='utf-8')
    (tmp_path / 'app.log.lock').write_text('', encoding='utf-8')
    return tmp_path


def test_parse_summary_line_reads_keys_and_ignores_unknown_ones():
    record = audit.parse_summary_line(_line(
        5, 'task-z', 'conv-z', total=3_000, wait=2_000, admission=1_500, retry=500,
        spawn=0, extra=' laterField=1'))
    assert record['task'] == 'task-z' and record['conv'] == 'conv-z'
    assert (record['waitMs'], record['admissionWaitMs'], record['retryWaitMs']) == (
        2_000, 1_500, 500)
    assert record['loggedAt'] == '2026-10-23 16:05:00'
    assert audit.parse_summary_line('[TurnTraceTiming] task=a status=done') is None
    assert audit.parse_summary_line('[TurnTraceSummary] task=a totalMs=1') is None


def _fold_rows(task_id, rows):
    from lib.tasks_pkg import turn_trace

    t0 = 1_724_000_000_000
    seeded = [{'event_id': index, 'type': kind, 'payload': payload | {'type': kind},
               'ts_ms': t0 + offset} for index, (kind, offset, payload) in enumerate(rows)]
    original = turn_trace._read_trace_rows
    turn_trace._read_trace_rows = lambda selected: list(seeded)
    try:
        return turn_trace.fold_task_trace(task_id)
    finally:
        turn_trace._read_trace_rows = original


def test_failed_attempt_time_is_parsed_aggregated_and_reported():
    gateway_hold = audit.parse_summary_line(_line(
        6, 'task-504', 'conv-1', total=9_000, wait=300, admission=0, retry=300,
        spawn=0, extra=' failedAttemptMs=7000'))
    older = audit.parse_summary_line(_line(
        7, 'task-old', 'conv-1', total=1_000, wait=0, admission=0, retry=0, spawn=0))
    assert (gateway_hold['failedAttemptMs'], older['failedAttemptMs']) == (7_000, 0)

    trace = _fold_rows('task-api', [
        ('round_start', 0, {'roundNum': 1}),
        ('phase', 100, {'phase': 'waiting_model'}),
        ('phase', 2_100, {'phase': 'retrying', 'attempt': 1, 'statusCode': 504}),
        ('model_request_start', 2_400, {'spanId': 'm2', 'roundNum': 1}),
        ('delta', 3_400, {}),
        ('round_end', 4_000, {'roundNum': 1, 'reason': 'final'}),
        ('done', 4_000, {'finishReason': 'stop'}),
    ])
    from_api = audit.record_from_trace(trace, 'conv-2')
    assert (from_api['failedAttemptMs'], from_api['ttftMs']) == (2_000, 1_000)

    report = audit.build_report([gateway_hold, older, from_api], source='mixed')
    assert report['overall']['failedAttemptMs'] == 9_000
    assert report['conversations']['conv-1']['failedAttemptMs'] == 7_000
    text = '\n'.join(audit.render_text(report))
    assert 'failed attempt (in llm)' in text and 'failedAtt' in text
    (row,) = [line for line in text.splitlines() if line.lstrip().startswith('task-504')]
    assert '7.0s' in row.split()[5]  # total, llm, then the failed-attempt column


def test_logs_mode_streams_rotations_dedupes_and_splits_wait(log_dir):
    paths = audit.summary_log_files(log_dir)
    assert [path.name for path in paths] == ['app.log.2026-10-22', 'app.log']
    records, duplicates = audit.scan_summary_logs(paths)
    assert duplicates == 1
    by_task = {record['task']: record for record in records}
    assert set(by_task) == {'task-a', 'task-b', 'task-c'}
    assert by_task['task-a']['admissionWaitMs'] == 6_000  # the later line wins
    report = audit.build_report(records, source='logs', duplicates=duplicates)
    conv1 = report['conversations']['conv-1']
    assert (conv1['tasks'], conv1['totalMs'], conv1['waitMs']) == (2, 14_000, 7_000)
    assert (conv1['admissionWaitMs'], conv1['retryWaitMs'], conv1['otherWaitMs']) == (
        6_000, 1_000, 0)
    conv2 = report['conversations']['conv-2']
    assert (conv2['retryWaitMs'], conv2['spawnWaitMs']) == (2_000, 1_000)
    assert report['overall']['totalMs'] == 22_000


def test_logs_mode_cli_filters_by_time_and_conversation(log_dir, capsys):
    assert audit.main(['--logs', str(log_dir), '--since', '2026-10-23 16:10', '--json']) == 0
    report = json.loads(capsys.readouterr().out)
    assert sorted(record['task'] for record in report['tasks']) == ['task-b', 'task-c']
    assert audit.main(['--logs', str(log_dir), 'conv-2']) == 0
    text = capsys.readouterr().out
    assert 'task-c' in text and 'task-a' not in text and 'admission_wait' in text


class _FakeAPI:
    """Duck-typed stand-in for the inspector's loopback client."""

    origin = 'http://127.0.0.1:0'

    def __init__(self, responses):
        self.responses = responses
        self.paths = []

    def get(self, path, **_kwargs):
        self.paths.append(path)
        response = self.responses[path]
        if isinstance(response, Exception):
            raise response
        return response


def _settled_trace(task_id):
    from lib.tasks_pkg import turn_trace

    t0 = 1_724_000_000_000
    rows = [
        ('round_start', 0, {'roundNum': 1}),
        ('round_end', 1_000, {'roundNum': 1, 'reason': 'tools'}),
        ('tool_start', 2_000, {'roundNum': 1, 'toolName': 'run_command',
                               'toolCallId': 'c1', 'tStart': t0 + 2_000}),
        ('workspace_admission_wait', 9_000, {
            'roundNum': 1, 'toolCallId': 'c1', 'toolName': 'run_command',
            'waitStartedAtMs': t0 + 2_000, 'waitEndedAtMs': t0 + 9_000, 'waitedMs': 7_000,
            'mode': 'write', 'lockDomain': 'workspace', 'lockRoot': 'repo',
            'outcome': 'admitted', 'holderTool': 'run_command',
            'holderConversation': 'conv-holder', 'holderHeldMs': 65_000}),
        ('tool_result', 10_000, {'roundNum': 1, 'toolCallId': 'c1', 'status': 'done',
                                 'tEnd': t0 + 10_000}),
        ('round_start', 11_000, {'roundNum': 2}),
        ('round_end', 12_000, {'roundNum': 2, 'reason': 'final'}),
        ('done', 12_000, {'finishReason': 'stop'}),
    ]
    seeded = [{'event_id': index, 'type': kind, 'payload': payload | {'type': kind},
               'ts_ms': t0 + offset} for index, (kind, offset, payload) in enumerate(rows)]
    original = turn_trace._read_trace_rows
    turn_trace._read_trace_rows = lambda selected: list(seeded)
    try:
        return turn_trace.fold_task_trace(task_id)
    finally:
        turn_trace._read_trace_rows = original


def test_api_mode_folds_recent_conversations_and_reports_skips():
    trace = _settled_trace('task-1')
    api = _FakeAPI({
        '/api/v4/conversations?limit=2': {'items': [{'id': 'conv-a'}, {'id': 'conv-b'}]},
        '/api/v4/tasks/by-conv/conv-a?limit=5': {'tasks': [
            {'taskId': 'task-1'}, {'taskId': 'task-live'}, {'taskId': 'task-gone'}]},
        '/api/v4/tasks/by-conv/conv-b?limit=5': inspector.DiagnosticAPIError('HTTP 404'),
        '/api/v4/tasks/task-1/trace': trace,
        '/api/v4/tasks/task-live/trace': {**trace, 'running': True},
        '/api/v4/tasks/task-gone/trace': {'eventsAvailable': False},
    })
    collected = audit.collect_api_records(api, [], tasks_per_conversation=5, recent=2)
    assert collected['incomplete'] == ''
    (record,) = collected['tasks']
    summary = trace['summary']
    assert record['task'] == 'task-1' and record['conv'] == 'conv-a'
    assert record['admissionWaitMs'] == 7_000
    assert record['admissionWaitMs'] + record['retryWaitMs'] + record['spawnWaitMs'] \
        == summary['waitMs'] == record['waitMs']
    assert record['rounds'] == 2 and record['totalMs'] == summary['totalMs']
    assert sorted((item['task'], item['reason']) for item in collected['skipped']) == [
        ('', 'HTTP 404'), ('task-gone', 'no timing evidence'), ('task-live', 'running')]
    (wait,) = collected['admissionWaits']
    assert (wait['ms'], wait['holderTool'], wait['holderConversation'], wait['heldMs']) == (
        7_000, 'run_command', 'conv-holder', 65_000)
    report = audit.build_report(collected['tasks'], skipped=collected['skipped'],
                                admission_waits=collected['admissionWaits'], source='api')
    text = '\n'.join(audit.render_text(report))
    assert 'Longest admission waits' in text and 'holder=run_command' in text


def test_api_record_keeps_the_persisted_wait_split_when_spans_were_dropped():
    trace = _settled_trace('task-1')
    assert trace['summary']['admissionWaitMs'] == 7_000
    bounded = {**trace, 'droppedSpans': 1,
               'spans': [span for span in trace['spans'] if span['kind'] != 'admission_wait']}
    record = audit.record_from_trace(bounded, 'conv-a')
    assert (record['admissionWaitMs'], record['retryWaitMs'], record['spawnWaitMs']) == (
        7_000, 0, 0)
    report = audit.build_report([record], source='api')
    assert report['overall']['otherWaitMs'] == 0

    # A document written before the persisted split recomputes it from the
    # kept spans: a lower bound whose remainder is reported as other wait.
    legacy_summary = {key: value for key, value in trace['summary'].items()
                      if key not in {'admissionWaitMs', 'retryWaitMs', 'spawnWaitMs'}}
    legacy = audit.record_from_trace({**bounded, 'summary': legacy_summary}, 'conv-a')
    assert legacy['admissionWaitMs'] == 0
    assert audit.build_report([legacy], source='api')['overall']['otherWaitMs'] == 7_000


def test_api_mode_reports_budget_exhaustion_as_incomplete():
    api = _FakeAPI({
        '/api/v4/tasks/by-conv/conv-a?limit=3': {'tasks': [{'taskId': 'task-1'}]},
        '/api/v4/tasks/task-1/trace': inspector.DiagnosticBudgetError('budget spent'),
    })
    collected = audit.collect_api_records(api, ['conv-a'], tasks_per_conversation=3, recent=1)
    assert collected['tasks'] == [] and collected['incomplete'] == 'budget spent'


def test_cli_rejects_mixed_sources(log_dir):
    with pytest.raises(SystemExit):
        audit.main(['--logs', str(log_dir), '--api-url', 'http://127.0.0.1:1'])
    with pytest.raises(SystemExit):
        audit.main(['--since', '2026-10-23'])


def _exported_events(path, *, task_id='task-replay', conv='conv-replay'):
    """A small server-clocked export: startup prompts, one round, settlement."""
    origin = 1_800_000_000_000
    events = [
        ('phase', 1_000, {'phase': 'executor_queued'}),
        ('phase', 1_500, {'phase': 'working', 'detailKey': 'stream.phase.workerStarting'}),
        ('phase', 2_000, {'phase': 'working', 'detailKey': 'stream.phase.startupContext'}),
        ('round_start', 4_000, {'roundNum': 0}),
        ('model_request_start', 4_200, {'spanId': 'm1', 'roundNum': 1,
                                        'requestLevel': 'logical'}),
        ('model_request_complete', 6_000, {'spanId': 'm1', 'status': 'succeeded'}),
        ('round_end', 6_300, {'roundNum': 0}),
        ('done', 6_500, {'finishReason': 'stop'}),
    ]
    rows = [{'seq': seq, 'type': kind, 'taskId': task_id, 'conversationId': conv,
             'executionClockId': 'clock', 'executionOriginAtMs': origin,
             'executionAtMs': origin + offset, **payload}
            for seq, (kind, offset, payload) in enumerate(events)]
    path.write_text(json.dumps({'taskId': task_id, 'events': rows}), encoding='utf-8')
    return path


def test_events_replay_refolds_with_this_checkout_and_names_startup(tmp_path, capsys):
    exported = _exported_events(tmp_path / 'events.json')
    assert audit.main(['--events', str(exported), '--json']) == 0
    report = json.loads(capsys.readouterr().out)
    (record,) = report['tasks']
    assert (record['task'], record['conv']) == ('task-replay', 'conv-replay')
    assert (record['totalMs'], record['unattributedMs']) == (6_500, 0)
    assert (record['startupMs'], record['roundPrepMs'], record['roundSettleMs'],
            record['finalizeMs']) == (4_000, 200, 300, 200)
    assert record['orchestrationMs'] == 4_700 and record['llmMs'] == 1_800
    assert report['startupStages'] == {'dispatch': 1_000, 'executor_queue': 500,
                                       'worker_start': 500, 'context': 2_000}
    assert report['spanEvidence'] is True and report['unattributedByPhase'] == []
    assert audit.main(['--events', str(exported)]) == 0
    text = capsys.readouterr().out
    assert 'Startup stages' in text and 'every listed gap is attributed' in text


def test_unattributed_by_phase_names_position_and_visible_prompt():
    trace = {
        'spans': [{'kind': 'round', 'tStart': 100, 'tEnd': 200},
                  {'kind': 'round', 'tStart': 300, 'tEnd': 400}],
        'gaps': [{'tStart': 0, 'tEnd': 50}, {'tStart': 150, 'tEnd': 160},
                 {'tStart': 220, 'tEnd': 260}, {'tStart': 400, 'tEnd': 430}],
        'statusHistory': [{'phase': 'working', 'detailKey': 'stream.phase.startupTools',
                           'tStart': 0, 'tEnd': 30},
                          {'phase': 'tool_exec', 'tStart': 210, 'tEnd': 240}],
    }
    assert audit.unattributed_by_phase(trace) == {
        ('before first round', 'stream.phase.startupTools'): 30,
        ('before first round', '(no prompt)'): 20,
        ('inside a round', '(no prompt)'): 10,
        ('between rounds', 'tool_exec'): 20,
        ('between rounds', '(no prompt)'): 20,
        ('after last round', '(no prompt)'): 30,
    }


def test_events_replay_is_a_local_source(tmp_path, log_dir):
    exported = _exported_events(tmp_path / 'events.json')
    with pytest.raises(SystemExit):
        audit.main(['--events', str(exported), '--api-url', 'http://127.0.0.1:1'])
    with pytest.raises(SystemExit):
        audit.main(['--events', str(exported), '--logs', str(log_dir)])


def test_events_replay_lists_spawn_waits_with_their_command(tmp_path, capsys):
    origin = 1_800_000_000_000
    events = [
        ('round_start', 0, {'roundNum': 0}),
        ('tool_start', 100, {'roundNum': 2, 'toolCallId': 'c2', 'toolName': 'run_command',
                             'query': 'make test', 'tStart': origin + 100}),
        ('tool_progress', 3_000, {'roundNum': 2, 'toolCallId': 'c2',
                                  'execStartTs': origin + 2_900}),
        ('tool_result', 4_000, {'roundNum': 2, 'toolCallId': 'c2', 'status': 'done',
                                'tEnd': origin + 4_000}),
        ('round_end', 4_000, {'roundNum': 0}),
        ('done', 4_000, {}),
    ]
    rows = [{'seq': seq, 'type': kind, 'taskId': 'task-spawn', 'executionClockId': 'clock',
             'executionOriginAtMs': origin, 'executionAtMs': origin + offset, **payload}
            for seq, (kind, offset, payload) in enumerate(events)]
    exported = tmp_path / 'events.json'
    exported.write_text(json.dumps({'taskId': 'task-spawn', 'events': rows}), encoding='utf-8')
    assert audit.main(['--events', str(exported), '--json']) == 0
    (spawn,) = json.loads(capsys.readouterr().out)['spawnWaits']
    assert spawn == {'task': 'task-spawn', 'conv': '-', 'ms': 2_800, 'toolName': 'run_command',
                     'toolCallId': 'c2', 'query': 'make test'}
    assert audit.main(['--events', str(exported)]) == 0
    assert 'cmd=make test' in capsys.readouterr().out
