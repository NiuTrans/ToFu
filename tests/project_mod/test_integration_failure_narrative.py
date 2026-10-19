"""Integration failures remain visible through the selected storage authority."""
import pytest

from tests.storage.test_storage_application_contract import (
    application_storage as application_storage,
    application_storage_owner_id as application_storage_owner_id,
)

pytestmark = pytest.mark.api


@pytest.mark.parametrize('reason', ['conflicting path ' * 180, '合并冲突：文件已修改😀' * 180],
                         ids=['ascii', 'unicode'])
def test_long_integration_failure_is_bounded_replay_safe_and_visible(
        application_storage, application_storage_owner_id, tmp_path, reason):
    from lib.conversations.project_brain import read_projection, record_integration_failure

    project = str(tmp_path / 'project')
    owner = application_storage_owner_id
    for _ in range(2):
        record_integration_failure(project, work_id='work-1', reason=reason, user_id=owner)
    rows = read_projection(project, user_id=owner)['narratives']
    assert len(rows) == 1
    assert rows[0]['kind'] == 'integration_failed'
    assert rows[0]['workId'] == 'work-1'
    assert rows[0]['text'].startswith('Integration failed for work-1: ')
    assert reason[:30] in rows[0]['text']
    assert len(rows[0]['text'].encode('utf-8')) <= 720
    assert '\ufffd' not in rows[0]['text']


def test_distinct_long_failures_keep_independent_idempotency_identity(
        application_storage, application_storage_owner_id, tmp_path):
    from lib.conversations.project_brain import read_projection, record_integration_failure

    project = str(tmp_path / 'project')
    prefix = 'same long diagnostic prefix ' * 50
    for suffix in ('first conflict', 'second conflict'):
        record_integration_failure(
            project, work_id='work-1', reason=prefix + suffix,
            user_id=application_storage_owner_id)
    rows = read_projection(project, user_id=application_storage_owner_id)['narratives']
    assert len(rows) == 2
    assert rows[0]['text'] == rows[1]['text']
    assert rows[0]['sequence'] != rows[1]['sequence']
