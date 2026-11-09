"""Run every protocol case against every adapter under the recorded baseline.

``baseline.json`` says, per adapter and case, ``pass``, ``xfail`` (fails today:
the cause with file:line and whether the core or the adapter owns the fix) or
``n/a`` (the platform offers nothing to exercise). An expected failure is
strict: a case that starts passing fails the run until the baseline is
updated, so the baseline moves with every migration step
(docs/CHANNEL_PROTOCOL.md, Conformance and Migration).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.channels.conformance.cases import CASES
from tests.channels.conformance.drivers import DRIVERS
from tests.channels.conformance.harness import Env, NotApplicable

pytestmark = pytest.mark.unit

BASELINE = json.loads(Path(__file__).with_name('baseline.json').read_text(encoding='utf-8'))
_ROOT = Path(__file__).resolve().parents[3]


def _params():
    for adapter in DRIVERS:
        for case in CASES:
            expected = BASELINE['results'][adapter][case]
            marks = []
            if expected['status'] == 'xfail':
                marks.append(pytest.mark.xfail(strict=True, reason=expected['cause']))
            yield pytest.param(adapter, case, expected, id=f'{adapter}-{case}', marks=marks)


@pytest.fixture
def env(records, store, router, dispatch, mailbox, monkeypatch, tmp_path, vault):
    return Env(store=store, records=records, dispatch=dispatch, monkeypatch=monkeypatch, tmp_path=tmp_path,
               vault=vault)


@pytest.mark.parametrize('adapter, case, expected', list(_params()))
def test_conformance(env, adapter, case, expected):
    driver = DRIVERS[adapter](env)
    driver.setup()
    try:
        if expected['status'] == 'n/a':
            try:
                CASES[case](driver)
            except NotApplicable:
                pytest.skip(f'n/a: {expected["reason"]}')
            pytest.fail('baseline.json says n/a, but the case ran: record its outcome')
        try:
            CASES[case](driver)
        except NotApplicable as error:
            pytest.fail(f'the case is not applicable ({error}), but baseline.json expects {expected["status"]}')
    finally:
        driver.teardown()


def test_the_baseline_covers_every_adapter_and_case_with_traceable_causes():
    assert set(BASELINE['results']) == set(DRIVERS)
    for adapter, results in BASELINE['results'].items():
        assert set(results) == set(CASES), adapter
        for case, expected in results.items():
            assert expected['status'] in ('pass', 'xfail', 'n/a'), (adapter, case)
            if expected['status'] == 'n/a':
                assert expected.get('reason'), (adapter, case)
            if expected['status'] == 'xfail':
                assert expected.get('owner') in ('core', 'adapter', 'core+adapter'), (adapter, case)
                for location in expected['cause'].split('`')[1::2]:
                    if ':' in location and '/' in location:
                        path, line = location.rsplit(':', 1)
                        assert (_ROOT / path).is_file(), (adapter, case, path)
                        if line.isdigit():
                            assert int(line) <= len((_ROOT / path).read_text(encoding='utf-8').splitlines()), location
