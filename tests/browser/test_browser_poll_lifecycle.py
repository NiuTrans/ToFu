"""Retired browser polls cannot dispatch work or publish into a new connection."""
import pytest

from tests._browser_extension_probe import run_extension_probe

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('phase', ['stop', 'body'])
def test_stop_releases_the_request_and_retains_its_unacknowledged_results(phase):
    probe = run_extension_probe('pollLifecycle:' + phase)
    assert probe['aborted'] is True
    assert probe['timersAfterStop'] == 0
    assert probe['stopped']['resultQueue'] == 1
    assert probe['afterLate']['resultQueue'] == 1
    assert probe['afterLate']['connected'] is False
    assert probe['executions'] == []
    assert probe['timersAfterLate'] == 0


def test_retired_auth_failure_cannot_overwrite_the_restarted_connection():
    probe = run_extension_probe('pollLifecycle:restart')
    assert probe['aborted'] is True
    assert probe['afterStale']['authFailures'] == 0
    assert probe['afterStale']['lastError'] == ''
    assert probe['afterFresh']['connected'] is True
    assert probe['afterFresh']['resultQueue'] == 0
    assert probe['executions'] == ['fresh-command']


def test_switching_servers_never_replays_a_previous_servers_results():
    probe = run_extension_probe('pollLifecycle:switch')
    assert probe['newServerResultIds'] == []
    assert probe['status']['resultQueue'] == 1
    assert probe['returnedIds'] == ['first-result']
    assert probe['afterReturn']['resultQueue'] == 0
    assert probe['executions'] == []


def test_accepted_file_transfer_keeps_its_issuing_server_and_credential():
    probe = run_extension_probe('fileTransferConnectionSwitch')
    assert probe['targetFetches'] == 1
    assert probe['serverRequests']
    assert all(row['url'].startswith('https://tofu.test/')
               and row['clientId'] == 'probe-client'
               and row['secret'] == 'probe-secret'
               for row in probe['serverRequests'])
    assert probe['result']['location'] == 'server_staging'


@pytest.mark.parametrize('change', ['server', 'secret'])
def test_late_automatic_pairing_never_replaces_new_connection_credentials(change):
    probe = run_extension_probe(f'repairConnectionSwitch:{change}')
    assert probe['repaired'] is False
    assert probe['secret'] == 'replacement'
    assert probe['createdTabOptions'] == []
    assert probe['status']['repairBusy'] is False


@pytest.mark.parametrize('target,accepted', [
    ('exact', True), ('child', True), ('prefix', False),
    ('otherHost', False), ('otherPort', False), ('malformed', False),
])
def test_repair_checks_exact_server_in_both_worker_and_serialized_page(target, accepted):
    probe = run_extension_probe('repairIdentity')
    assert probe['tabs'][target] is accepted
    page = probe['pages'][target]
    assert page['calls'] == int(accepted)
    assert ('token' in page['result']) is accepted


@pytest.mark.parametrize('phase', ['start', 'wait'])
def test_device_download_receipt_keeps_its_issuing_device(phase):
    probe = run_extension_probe('deviceDownloadConnection:' + phase)
    assert probe['result']['location'] == 'device_downloads'
    assert probe['result']['clientId'] == 'issuing-device'


def test_resource_setup_failure_does_not_drop_other_commands_in_the_poll():
    probe = run_extension_probe('pollCommandResourceFailure')
    assert [row['id'] for row in probe['results']] == ['first', 'second']
    assert all(row['error'] == 'resource setup failed' for row in probe['results'])
    assert probe['status']['commandsFailed'] == 2
    assert probe['status']['inflight'] == 0
    assert probe['delays'] == [0]
