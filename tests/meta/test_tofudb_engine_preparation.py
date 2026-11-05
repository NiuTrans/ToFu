"""A source-built tofu-db engine never spends a test's deadline or hides failure.

A cold cargo build of the engine outlasts pytest-timeout's per-test hang
guard. Built inside the first dependent test's setup, it errored that test
and every test queued behind the build lock (14 TofuDB context-read cases in
one run), and the interrupted build then made later callers silently skip.
The build now runs with the caller's deadline paused (it keeps its own
deadline), a failed build fails every later caller, and an interrupted build
is retried.
"""
import signal
import time

import pytest

from tests.support import tofudb_binary as binary_support

pytestmark = pytest.mark.unit


@pytest.fixture
def fresh_resolution(monkeypatch, tmp_path):
    monkeypatch.delenv('TOFUDB_DIFFERENTIAL_BIN', raising=False)
    monkeypatch.setattr(binary_support, '_RESOLVED', None)
    monkeypatch.setattr(binary_support, '_RESOLUTION_ATTEMPTED', False)
    monkeypatch.setattr(binary_support, '_FAILURE', None)
    cargo = tmp_path / 'cargo'
    cargo.write_text('#!/bin/sh\nexit 0\n')
    monkeypatch.setattr(binary_support, '_CARGO_CANDIDATES', [cargo])
    return tmp_path


def _armed_seconds() -> float:
    return signal.getitimer(signal.ITIMER_REAL)[0]


@pytest.mark.timeout(1)
def test_a_cold_build_does_not_spend_the_test_deadline(fresh_resolution, monkeypatch):
    built = fresh_resolution / 'tofu-db'

    def slow_build(project_root, cargo):
        time.sleep(1.5)
        return built

    monkeypatch.setattr(binary_support, '_cargo_build', slow_build)

    assert binary_support.ensure_engine_binary(fresh_resolution) == built
    # The hang guard is re-armed for the rest of the test.
    assert 0 < _armed_seconds() <= 1


@pytest.mark.timeout(30)
def test_a_failed_build_fails_every_later_caller(fresh_resolution, monkeypatch):
    calls = []

    def failed(project_root, cargo):
        calls.append(project_root)
        raise RuntimeError('tofu-db source build failed; refusing a stale binary fallback: E0425')

    monkeypatch.setattr(binary_support, '_cargo_build', failed)
    for _ in range(2):
        with pytest.raises(RuntimeError, match='E0425'):
            binary_support.ensure_engine_binary(fresh_resolution)
    assert calls == [fresh_resolution]
    assert _armed_seconds() > 0


class _Interrupted(BaseException):
    """Stands in for pytest-timeout's Failed outcome or a KeyboardInterrupt."""


def test_an_interrupted_build_is_retried_not_skipped(fresh_resolution, monkeypatch):
    built = fresh_resolution / 'tofu-db'
    outcomes = [_Interrupted(), built]

    def build(project_root, cargo):
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(binary_support, '_cargo_build', build)
    with pytest.raises(_Interrupted):
        binary_support.ensure_engine_binary(fresh_resolution)
    assert binary_support.ensure_engine_binary(fresh_resolution) == built
