"""Memory observations cannot turn root accounting into task refusal."""
import pytest
from lib.agent_core.admission import AdmissionController

pytestmark = pytest.mark.unit


def test_memory_cannot_refuse_execution(monkeypatch):
    # Retired admission knobs left in an old environment have no effect.
    monkeypatch.setenv('TOFU_ADMISSION_CGROUP_PCT', '50')
    monkeypatch.setenv('TOFU_ADMISSION_PROCESS_RSS', '1')
    monkeypatch.setenv('TOFU_MAX_INFLIGHT_TASKS', '1')
    import lib.cgroup_guard as cgroup
    monkeypatch.setattr(cgroup, 'pressure', lambda: pytest.fail('request-time memory probe'))
    gate = AdmissionController()
    tokens = [gate.acquire() for _ in range(64)]
    assert len(set(tokens)) == 64
    assert gate.resource_snapshot() == {
        'capacity': 0, 'used': 64, 'blocked': 0, 'pressure': False}
    assert all(gate.release(token) for token in tokens)
    assert gate.in_flight == 0
