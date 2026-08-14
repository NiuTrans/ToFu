"""Shared durable-run repository failure policy."""

from __future__ import annotations

from pathlib import Path

import pytest

from lib.orchestration.run_repository_call import (
    run_store_attempt,
    run_store_require,
)
from lib.orchestration.run_store_port import OrchestrationRunStoreError


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def test_best_effort_write_preserves_success_and_fallback_semantics():
    assert run_store_attempt(
        'create_run(run-1)', lambda: True, fallback=False,
    ) is True
    assert run_store_attempt(
        'create_run(run-1)',
        lambda: (_ for _ in ()).throw(OSError('offline')),
        fallback=False,
    ) is False


def test_required_read_translates_dependency_failure_with_cause():
    failure = OSError('offline')

    with pytest.raises(
        OrchestrationRunStoreError,
        match='failed to read orchestration run',
    ) as caught:
        run_store_require(
            'get_run(run-1)',
            'failed to read orchestration run run-1',
            lambda: (_ for _ in ()).throw(failure),
        )

    assert caught.value.__cause__ is failure


def test_required_read_preserves_existing_store_error_identity():
    failure = OrchestrationRunStoreError('corrupt durable event')

    with pytest.raises(OrchestrationRunStoreError) as caught:
        run_store_require(
            'get_event_page(run-1)',
            'failed to replay orchestration run run-1',
            lambda: (_ for _ in ()).throw(failure),
        )

    assert caught.value is failure


def test_database_repositories_delegate_failure_policy_once():
    directory = ROOT / 'lib/orchestration'
    owners = (
        'run_event_repository.py',
        'run_header_repository.py',
        'run_deletion_repository.py',
    )
    combined = ''
    for name in owners:
        source = (directory / name).read_text()
        combined += source
        assert 'except Exception' not in source, name
        assert 'run_repository_call import' in source, name

    assert combined.count('run_store_attempt(') == 5
    assert combined.count('run_store_require(') == 3
