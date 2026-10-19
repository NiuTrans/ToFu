"""Provider batching observes one committed cadence without claiming write authority."""
import pytest
from lib import turn_lifecycle as lifecycle

pytestmark = pytest.mark.unit


def test_deadline_follows_committed_observation_and_clear(monkeypatch):
    monkeypatch.setenv('TOFU_TURN_DELTA_RECORD_MS', '300')
    monkeypatch.setattr(lifecycle, '_delta_last_recorded', {})
    now = [10.]
    monkeypatch.setattr(lifecycle.time, 'monotonic', lambda: now[0])
    assert lifecycle.next_progress_projection_deadline('owned-attempt') is None
    assert lifecycle._delta_throttle_allows('owned-attempt')
    lifecycle._delta_throttle_stamp('owned-attempt')
    assert lifecycle.next_progress_projection_deadline('owned-attempt') == pytest.approx(10.3)
    assert not lifecycle._delta_throttle_allows('owned-attempt')
    now[0] = 10.301
    assert lifecycle._delta_throttle_allows('owned-attempt')
    # Observing readiness never advances the committed cadence.
    assert lifecycle.next_progress_projection_deadline('owned-attempt') == pytest.approx(10.3)
    lifecycle._delta_throttle_clear('owned-attempt')
    assert lifecycle.next_progress_projection_deadline('owned-attempt') is None


def test_eager_projection_policy_never_adds_another_wait(monkeypatch):
    monkeypatch.setenv('TOFU_TURN_DELTA_RECORD_MS', '0')
    assert lifecycle.next_progress_projection_deadline('owned-attempt') == 0
    assert lifecycle._delta_throttle_allows('owned-attempt')
