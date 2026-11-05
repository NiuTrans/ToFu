"""Receipt settlement and command teardown without Chrome or network timing."""
import json
from pathlib import Path
import subprocess

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def probe(mode):
    result = subprocess.run(
        ['node', str(ROOT / 'tests/support/browser_worker_owners_probe.cjs'), mode,
         str(ROOT / 'browser_extension/worker')],
        cwd=ROOT, check=True, capture_output=True, text=True, timeout=15)
    return json.loads(result.stdout)


def test_receipts_settle_once_preserve_fifo_and_never_cross_servers():
    result = probe('receipts')
    assert result['leased'] == 3
    assert result['rolledBack'] is True
    assert result['duplicateAck'] is False
    assert [row['id'] for row in result['others']] == ['b1']
    assert [row['id'] for row in result['replay']] == ['a1', 'a2', 'a3']
    assert [row['id'] for row in result['renewed']] == ['credential-renewal']
    assert result['remaining'] == result['retainedScopes'] == 0


def test_oversized_results_compact_and_413_retries_preserve_each_receipt():
    result = probe('payloads')
    assert [row['id'] for row in result['normalized']] == ['circular', 'unicode', 'small']
    assert all(row['result'] is None and 'transport limit' in row['error']
               for row in result['normalized'][:2])
    assert result['normalized'][2]['result'] == 'ok'
    assert result['initialSize'] == 4
    assert result['splitIds'] == ['0', '1']
    assert result['singletonId'] == '0'
    assert result['firstBackoff'] is result['singletonBackoff'] is False
    assert result['compacted']['result'] is None
    assert result['floorBackoff'] is True
    assert [row['id'] for row in result['remaining']] == ['0', '1', '2', '3']


def test_settled_server_scopes_release_transient_storage():
    result = probe('retention')
    assert result == {'delivered': 8192, 'largestLive': 32, 'scopesBeforeStop': 1,
                      'scopesAfterStop': 0, 'outstanding': 0}


def test_command_deduplication_is_scoped_and_cleanup_precedes_delivery():
    result = probe('commandScopes')
    assert result['during']['inflight'] == 2
    assert result['beforeCleanup']['status']['inflight'] == 2
    assert result['beforeCleanup']['queued'] == 0
    assert result['created'] == result['closed'] == 2
    assert result['after'] == {'inflight': 0, 'commandsExecuted': 2, 'commandsFailed': 0}
    assert result['settlements'] == ['https://a.test', 'https://b.test']
    assert result['a'][0]['result'] == {'serverUrl': 'https://a.test'}
    assert result['b'][0]['result'] == {'serverUrl': 'https://b.test'}


def test_resource_creation_execution_and_cleanup_failures_settle_once():
    cases = probe('commandFailures')
    for phase, result in cases.items():
        assert result['settled'] == 1
        assert result['closed'] == (0 if phase == 'create' else 1)
        assert result['status'] == {'inflight': 0, 'commandsExecuted': 0, 'commandsFailed': 1}
        assert len(result['results']) == 1
        assert result['results'][0]['result'] is None
    assert cases['create']['results'][0]['error'] == 'create failed'
    assert cases['execute']['results'][0]['error'] == 'execute failed'
    assert cases['execute+cleanup']['results'][0]['error'] == 'execute failed'
    assert 'cleanup failed' in cases['cleanup']['results'][0]['error']
