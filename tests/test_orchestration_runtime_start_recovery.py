"""Durable-start failure recovery keeps the primary failure authoritative."""

from __future__ import annotations

import pytest

import lib.orchestration.runtime_start_recovery as recovery


pytestmark = pytest.mark.unit


class _FailingRuntime:
    def finish(self, *_args, **_kwargs):
        raise OSError('runtime cleanup offline')


class _FailingRuns:
    def transition_status(self, *_args, **_kwargs):
        raise OSError('durable cleanup offline')


def test_recovery_never_masks_primary_failure(caplog):
    primary = RuntimeError('worker spawn failed')

    recovery.recover_failed_durable_start(
        _FailingRuntime(), _FailingRuns(), 'run-1', primary)

    messages = [record.getMessage() for record in caplog.records]
    assert any('failed to close runtime task run=run-1' in message
               for message in messages)
    assert any('failed to record durable start failure run=run-1' in message
               for message in messages)
