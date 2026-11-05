"""Semantic comparisons enter an asynchronously opened search projection."""
from types import SimpleNamespace

import pytest

from tests.tofudb import test_tofudb_differential as differential

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('name', ['search limit bounds', 'search snippet radius bound'])
def test_search_validation_baseline_waits_for_projection(monkeypatch, name):
    clock = [100.0]
    monkeypatch.setattr(differential, 'time', SimpleNamespace(
        monotonic=lambda: clock[0], time_ns=lambda: int(clock[0] * 10**9),
        sleep=lambda delay: clock.__setitem__(0, clock[0] + delay)))

    class Driver:
        calls = 0

        def execute(self, *args, **kwargs):
            self.calls += 1
            if self.calls <= 2:
                return differential.Outcome(False, error_code='database_unavailable')
            return differential.Outcome(True, [])

    driver = Driver()
    outcome = differential._execute_step(driver, differential._search_scripts()[name][0], [])
    assert outcome.ok and outcome.value == []
    assert driver.calls >= 3
