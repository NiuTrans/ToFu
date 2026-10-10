"""Real read-only HTTP status latency on the fixture's scratch authority."""
import json
import time
import urllib.request
import pytest

pytestmark = pytest.mark.visual


def test_real_database_status_never_reports_running_engine_uninstalled(live_server):
    durations = []
    for _ in range(5):
        start = time.perf_counter()
        with urllib.request.urlopen(live_server + '/api/v4/database/status', timeout=10) as response:
            assert response.status == 200
            envelope = json.load(response)
            assert 'data' in envelope and 'meta' in envelope
            status = envelope['data']
        durations.append((time.perf_counter() - start) * 1000)
        assert 'engine' in status, 'Unexpected status envelope keys: ' + ', '.join(status.keys())
        engine = status['engine']
        assert engine['ready'] is True
        assert engine['engines'][engine['backend']]['available'] is True
        assert engine['engines']['tofudb']['launchVerification'] == 'not-checked'
        assert engine['filesystem']['evidence'] == 'read-only'
    print('Scratch HTTP database/status timing: ' + json.dumps({
        'cold_ms': round(durations[0], 3),
        'warm_ms': [round(value, 3) for value in durations[1:]],
        'production_latency_proven': False,
    }))
    # This is a coarse regression fence, not a claimed production SLO.
    assert max(durations) < 5000, 'Light settings status regressed to multi-second admission work'
