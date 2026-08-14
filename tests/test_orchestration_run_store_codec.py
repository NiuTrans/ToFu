"""Fail-closed JSON boundaries for durable orchestration persistence."""

from __future__ import annotations

import pytest

from lib.orchestration.run_event_repository import (
    OrchestrationRunEventRepository,
)
from lib.orchestration.run_header_repository import (
    OrchestrationRunHeaderRepository,
)
from lib.orchestration.run_store_codec import (
    decode_run_json,
    encode_run_json,
)
from lib.orchestration.run_store_port import OrchestrationRunStoreError


pytestmark = pytest.mark.unit


class _Database:
    def __init__(self):
        self.executed = False

    def execute(self, *_args, **_kwargs):
        self.executed = True
        raise AssertionError('invalid payload must fail before SQL')


@pytest.mark.parametrize(
    'value',
    [
        {'unsupported': object()},
        {'not_json': float('nan')},
        {'not_json': float('inf')},
    ],
)
def test_json_encoding_rejects_lossy_or_nonstandard_payloads(value):
    with pytest.raises(OrchestrationRunStoreError, match='encode'):
        encode_run_json(value)


def test_legacy_corrupt_reads_remain_tolerant():
    fallback = {'legacy': True}
    assert decode_run_json('{broken', fallback) is fallback
    with pytest.raises(OrchestrationRunStoreError, match='decode'):
        decode_run_json('{broken', fallback, strict=True)


def test_repositories_never_report_success_after_payload_loss():
    database = _Database()
    headers = OrchestrationRunHeaderRepository(
        lambda: database, lambda: 1000)
    events = OrchestrationRunEventRepository(
        lambda: database, lambda: 1000)
    invalid = {'unsupported': object()}

    assert headers.create('run', definition=invalid) is False
    assert headers.update_status('run', 'error', error=invalid) is False
    assert headers.retire_interrupted(invalid) is None
    assert events.append('run', 0, invalid) is False
    assert database.executed is False
