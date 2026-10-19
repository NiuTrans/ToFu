"""Batch presentation must distinguish planned edits from applied mutations."""

import pytest

from lib.tools.meta import build_project_tool_meta

pytestmark = pytest.mark.unit


def _args(count=3):
    return {
        'description': 'Update related files',
        'edits': [
            {'path': f'{index}.py', 'operation': 'replace',
             'anchor': 'old', 'content': 'new'}
            for index in range(count)
        ],
    }


def test_atomic_preflight_skips_are_not_successes():
    result = build_project_tool_meta('edit_file', _args(), (
        'Applied 0/3 edits (1 failed); atomic preflight failed; '
        'no files written; 2 skipped\n'
        '[1] SKIP 0.py [replace]: Not applied because atomic preflight failed.\n'
        '[2] FAIL 1.py [replace] [match-not-found]: Search text not found.\n'
        '[3] SKIP 2.py [replace]: Not applied because atomic preflight failed.'
    ))
    assert result['writeOk'] is False
    summaries = result['editSummaries']
    assert [item['status'] for item in summaries] == ['fail'] * 3
    assert [item.get('skipped', False) for item in summaries] == [True, False, True]
    assert summaries[0]['detail'].endswith('Not applied because atomic preflight failed.')
    assert summaries[2]['operation'] == 'replace'


def test_sparse_result_indexes_do_not_shift_success_to_another_edit():
    result = build_project_tool_meta('edit_file', _args(), (
        'Applied 1/3 edits (2 failed)\n'
        '[1] FAIL 0.py: missing anchor\n'
        '[warning] Other edits were not executed.\n'
        '[3] OK 2.py: applied'
    ))
    assert [item['status'] for item in result['editSummaries']] == ['fail', 'fail', 'ok']
    assert result['writeOk'] is False


@pytest.mark.parametrize('receipt', [
    'Applied 1/1 edits (0 failed)\n[1] SKIP Not applied',
    'Applied 1/1 edits (0 failed)\n[1] UNKNOWN No success receipt',
    'Applied 1/1 edits (0 failed)',
    'Applied 1/1 edits (0 failed)\n[1] OK applied\n[1] FAIL conflicting receipt',
])
def test_summary_count_cannot_invent_success(receipt):
    result = build_project_tool_meta('edit_file', _args(1), receipt)
    assert result['writeOk'] is False
    assert result['editSummaries'][0]['status'] == 'fail'


def test_successful_batch_retains_existing_presentation_contract():
    result = build_project_tool_meta('edit_file', _args(2), (
        'Applied 2/2 edits (0 failed)\n'
        '[1] OK 0.py: applied\n[2] OK 1.py: applied'
    ))
    assert result['writeOk'] is True
    assert [item['status'] for item in result['editSummaries']] == ['ok', 'ok']
    assert all('skipped' not in item for item in result['editSummaries'])
