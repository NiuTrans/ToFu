"""Memory observations cannot turn root accounting into task refusal."""
import pytest
from lib.agent_core.admission import AdmissionController

pytestmark = pytest.mark.unit


def test_pressure_knobs_cannot_refuse_execution(monkeypatch):
    monkeypatch.setenv('TOFU_ADMISSION_CGROUP_PCT', '50')
    monkeypatch.setenv('TOFU_ADMISSION_PROCESS_RSS', '1')
    monkeypatch.setenv('TOFU_MAX_INFLIGHT_TASKS', '1')
    import lib.cgroup_guard as cgroup
    monkeypatch.setattr(cgroup, 'pressure', lambda: pytest.fail('request-time memory probe'))
    monkeypatch.setattr(cgroup, 'process_rss_admission_snapshot',
                        lambda: pytest.fail('request-time RSS probe'))
    gate = AdmissionController()
    first, second = gate.acquire(), gate.acquire()
    assert first != second
    assert gate.resource_snapshot() == {
        'capacity': 0, 'used': 2, 'blocked': 0, 'pressure': False}
    assert gate.release(first)
    assert gate.release(second)
