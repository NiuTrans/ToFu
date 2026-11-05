"""Frame policy and content provenance in the actual extension worker."""

import json

import pytest

from tests._browser_extension_probe import run_extension_probe

pytestmark = pytest.mark.unit


@pytest.fixture(scope='module')
def probe():
    return run_extension_probe('frameAuthority')


def test_frame_state_reports_actual_url_and_document(probe):
    assert probe['state']['result']['url'] == 'https://app.test/main'
    assert probe['state']['result']['frame'] == {
        'frameId': 3, 'url': 'https://child.test/article', 'documentId': 'document-child',
    }


def test_frame_read_keeps_child_provenance_without_parent_capture(probe):
    result = probe['read']['result']
    assert result['url'] == 'https://child.test/article'
    assert result['title'] == 'Child'
    assert 'PARENT_NETWORK' not in json.dumps(result)


def test_every_frame_injection_targets_the_authorized_document(probe):
    assert all('error' not in action for action in probe['actions']), probe['actions']
    assert probe['authorizedTargets']
    assert all(target == {'tabId': 7, 'documentIds': ['document-child']}
               for target in probe['authorizedTargets'])


def test_frame_commands_refuse_missing_or_replaced_document(probe):
    assert probe['unpinned'].get('error')
    assert all(row.get('error') for row in probe['stale']), probe['stale']


def test_read_never_combines_two_documents_when_snapshot_enrichment_races(probe):
    assert 'FOREIGN_PRIVATE_CONTENT' not in json.dumps(probe['duringRead'])


def test_missing_frame_content_cannot_be_reported_as_an_empty_parent_read(probe):
    assert probe['empty'].get('error')


def test_wait_propagates_lost_document_without_retrying_another_document(probe):
    assert probe['interruptedWait'].get('error') == 'The authorized document no longer exists'
