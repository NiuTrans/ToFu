"""Focused tests for debug/tool_call_success_report.py (read-only)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / 'debug'))

from tool_call_success_report import build_report, main, render_text  # noqa: E402


def _write_audit(path, rows):
    path.write_text(
        '\n'.join(json.dumps(row) for row in rows) + '\n', encoding='utf-8')


@pytest.fixture
def audit_file(tmp_path):
    path = tmp_path / 'audit.log'
    _write_audit(path, [
        {'event': 'tool_call_outcome', 'model': 'kimi-k3',
         'tool': 'read_files', 'outcome': 'ok', 'status': 'done'},
        {'event': 'tool_call_outcome', 'model': 'kimi-k3',
         'tool': 'read_files', 'outcome': 'repaired', 'status': 'done',
         'repairs': ['tool_name_alias']},
        {'event': 'tool_call_outcome', 'model': 'kimi-k3',
         'tool': 'serch', 'outcome': 'rejected', 'status': 'rejected',
         'rejection_kind': 'hallucinated', 'attempted': 'serch'},
        {'event': 'tool_call_outcome', 'model': 'kimi-k3',
         'tool': 'run_command', 'outcome': 'error', 'status': 'error'},
        {'event': 'tool_call_outcome', 'model': 'glm-5',
         'tool': 'apply_diff', 'outcome': 'rejected',
         'status': 'rejected', 'rejection_kind': 'gateway_invalid_arguments'},
        {'event': 'ptc_program_outcome', 'model': 'kimi-k3',
         'backend': 'local_toolscript', 'status': 'completed',
         'children': 3, 'rejected_children': 1, 'syntax_repairs': 0,
         'duration_ms': 1200},
        {'event': 'ptc_program_outcome', 'model': 'kimi-k3',
         'backend': 'local_toolscript', 'status': 'error',
         'children': 0, 'rejected_children': 0, 'syntax_repairs': 2,
         'error_code': 'syntax_error', 'duration_ms': 40},
        {'event': 'tool_gateway_repaired', 'model': 'kimi-k3',
         'tool': 'read_files', 'attempted': 'read_file',
         'resolved': 'read_files'},
        {'event': 'unrelated_event', 'model': 'kimi-k3'},
        {'event': 123, 'model': 'kimi-k3'},
    ])
    with path.open('a', encoding='utf-8') as handle:
        handle.write('not-json-at-all\n\n')
    return path


def test_build_report_aggregates_outcomes(audit_file):
    report = build_report([str(audit_file)])

    calls = report['tool_calls']
    assert calls['total'] == 5
    assert calls['by_outcome']['ok'] == 1
    assert calls['by_outcome']['repaired'] == 1
    assert calls['by_outcome']['rejected'] == 2
    assert calls['by_outcome']['error'] == 1
    assert calls['first_attempt_rate'] == 0.2
    assert calls['with_repair_rate'] == 0.4

    rows = {row['name']: row for row in calls['by_model_tool']}
    assert rows['read_files']['total'] == 2
    assert rows['read_files']['first_attempt_rate'] == 0.5

    assert ('hallucinated', 1) in report['rejection_kinds']
    assert ('gateway_invalid_arguments', 1) in report['rejection_kinds']
    assert ('serch', 1) in report['hallucinated_names']
    assert ('tool_name_alias', 1) in report['repair_patterns']
    assert ('read_file -> read_files', 1) in \
        report['gateway_alias_leaderboard']


def test_build_report_aggregates_programs(audit_file):
    report = build_report([str(audit_file)])

    rows = report['programs']['by_model_backend']
    assert len(rows) == 1
    row = rows[0]
    assert row['name'] == 'local_toolscript'
    assert row['total'] == 2
    assert row['completed'] == 1
    assert row['error'] == 1
    assert row['clean'] == 1
    assert row['clean_rate'] == 0.5
    assert row['rejected_children'] == 1
    assert ('syntax_error', 1) in report['programs']['error_codes']


def test_model_filter_narrows_rows(audit_file):
    report = build_report([str(audit_file)], model='glm-5')

    assert report['tool_calls']['total'] == 1
    assert report['tool_calls']['by_model_tool'][0]['name'] == 'apply_diff'


def test_render_text_sections(audit_file):
    report = build_report([str(audit_file)])
    text = render_text(report, top=5)

    assert 'first-attempt' in text
    assert 'hallucinated' in text
    assert 'PTC program authoring' in text


def test_main_missing_file_warns_but_succeeds(tmp_path, capsys):
    rc = main(['--file', str(tmp_path / 'absent.log')])

    assert rc == 0
    assert 'warning' in capsys.readouterr().err
