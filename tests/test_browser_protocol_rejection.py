"""Browser handshake failures stay strict and return actionable errors."""

from __future__ import annotations

import pytest

from tests._browser_extension_probe import run_extension_probe


pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_client_registry():
    from lib.browser.queue import _state

    with _state._clients_lock:
        _state._clients.clear()
    yield
    with _state._clients_lock:
        _state._clients.clear()


@pytest.mark.parametrize('version', [None, '', False, 'not-a-version', 1])
def test_poll_rejects_missing_or_old_protocol_with_upgrade_message(version):
    from lib.browser.protocol import BrowserProtocolRejected
    from lib.browser.queue import get_connected_clients, mark_poll

    with pytest.raises(
            BrowserProtocolRejected,
            match='Browser protocol 2 is required'):
        mark_poll(
            'old-extension',
            owner_user_id='101',
            protocol_version=version,
            capabilities=[],
        )

    assert get_connected_clients(owner_user_id='101') == []


def test_extension_parks_protocol_rejection_instead_of_retrying_every_3s():
    probe = run_extension_probe('protocol426')

    assert probe['reportedProtocolHeader'] == '2'
    assert probe['state']['queue'] == ['completed-result']
    assert probe['state']['connected'] is False
    assert 'upgrade required' in probe['state']['lastError'].lower()
    assert probe['state']['upgradeDelay'] == 300_000
    assert probe['state']['ordinaryDelay'] == 3_000
    assert probe['scheduled'][-1]['delay'] == 300_000
    assert probe['scheduled'][-1]['delay'] != probe['state']['ordinaryDelay']


def test_extension_preserves_results_and_honors_server_rate_backoff():
    probe = run_extension_probe('rate429')

    assert probe['state']['queue'] == ['completed-result']
    assert probe['state']['connected'] is True
    assert probe['state']['lastError'] == ''
    assert probe['scheduled'][-1]['delay'] == 7_000


def test_extension_batches_results_and_compacts_one_extreme_payload():
    probe = run_extension_probe('resultBatch')

    assert len(probe['small']['batchIds']) == 32
    assert len(probe['small']['remainingIds']) == 8
    assert probe['oversize']['id'] == 'oversize'
    assert probe['oversize']['result'] is None
    assert 'exceeded' in probe['oversize']['error']


def test_extension_bisects_rejected_batch_before_sacrificing_results():
    probe = run_extension_probe('payload413')

    assert probe['preserved'] == [
        'result-0', 'result-1', 'result-2', 'result-3']
    assert probe['batchLimit'] == 2
    assert probe['nextBatch'] == ['result-0', 'result-1']
    assert probe['scheduled'][-1]['delay'] == 0


def test_extension_replays_results_after_transport_failure():
    probe = run_extension_probe('transportFailure')

    assert probe['queue'] == ['completed-result']
    assert probe['scheduled'][-1]['delay'] == 3_000
