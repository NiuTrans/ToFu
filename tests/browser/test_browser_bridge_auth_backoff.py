"""Real worker behavior for authentication retries and reconnecting the popup."""
import pytest

from tests._browser_extension_probe import run_extension_probe

pytestmark = pytest.mark.unit


@pytest.fixture(scope='module')
def auth_probe():
    return run_extension_probe('authBackoff')


def test_401_backs_off_exponentially_with_cap(auth_probe):
    assert [row['delay'] for row in auth_probe['responses']] == [
        9000, 18000, 36000, 72000, 144000, 288000, 300000, 300000]
    assert [row['status']['authFailures'] for row in auth_probe['responses']] == list(range(1, 9))


def test_needs_repair_state_after_give_up_threshold(auth_probe):
    for index, row in enumerate(auth_probe['responses'], 1):
        assert row['status']['needsRepair'] is (index >= 5)
        assert row['badge'] == ('KEY' if index >= 5 else 'ERR')
        assert 're-pair' in row['status']['lastError']


def test_success_resets_the_backoff(auth_probe):
    success = auth_probe['success']
    assert success['delay'] == 100
    assert success['badge'] == 'ON'
    assert success['status']['authFailures'] == 0
    assert success['status']['needsRepair'] is False
    assert success['status']['lastError'] == ''
    assert success['status']['resultQueue'] == 0


def test_single_pending_timer_invariant(auth_probe):
    observations = [*auth_probe['responses'], auth_probe['success'],
                    auth_probe['proxy'], auth_probe['parked'], auth_probe['afterSecret']]
    assert all(row['timerCount'] == 1 for row in observations)
    assert auth_probe['timersAfterStop'] == 0


def test_secret_change_reconnects_immediately(auth_probe):
    assert auth_probe['parked']['status']['needsRepair'] is True
    assert len(auth_probe['requests']) == auth_probe['beforeSecret'] + 1
    assert auth_probe['requests'][-1]['headers']['X-Bridge-Secret'] == 'replacement'
    assert auth_probe['afterSecret']['status']['connected'] is True
    assert auth_probe['afterSecret']['status']['authFailures'] == 0
    assert auth_probe['afterSecret']['status']['needsRepair'] is False


def test_completed_result_survives_every_auth_retry(auth_probe):
    assert all(row['results'] == ['completion'] for row in auth_probe['requests'][:9])
    assert all(row['results'] == [] for row in auth_probe['requests'][9:])
