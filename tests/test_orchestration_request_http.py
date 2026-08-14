"""Shared orchestration HTTP request-preparation contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from routes.api_v1.orchestration_request_http import (
    OrchestrationHttpPreparation,
    orchestration_request_response,
)


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def test_accepted_none_is_distinct_from_a_rejected_request():
    preparation = OrchestrationHttpPreparation.accept(None)

    assert preparation.accepted
    assert preparation.require() is None
    assert tuple(preparation) == (None, None)
    assert orchestration_request_response(
        preparation, lambda value: ('handled', value),
    ) == ('handled', None)


def test_rejected_request_returns_failure_without_running_handler():
    failure = object()
    seen = []
    preparation = OrchestrationHttpPreparation.reject(failure)

    assert not preparation.accepted
    assert tuple(preparation) == (None, failure)
    assert orchestration_request_response(
        preparation, lambda value: seen.append(value),
    ) is failure
    assert seen == []
    with pytest.raises(RuntimeError, match='rejected'):
        preparation.require()


def test_dispatch_adopts_legacy_preparation_pairs_at_one_boundary():
    failure = object()

    assert orchestration_request_response(
        ('value', None), lambda value: value.upper(),
    ) == 'VALUE'
    assert orchestration_request_response(
        (None, failure), lambda _value: pytest.fail('handler ran'),
    ) is failure


def test_invalid_preparation_states_fail_at_the_shared_boundary():
    with pytest.raises(ValueError, match='cannot have a failure'):
        OrchestrationHttpPreparation(
            accepted=True, value='value', failure=object())
    with pytest.raises(ValueError, match='requires a failure'):
        OrchestrationHttpPreparation(accepted=False)


def test_every_orchestration_preparer_and_route_uses_the_shared_result():
    route_dir = ROOT / 'routes/api_v1'
    preparers = (
        'orchestration_authoring_http.py',
        'orchestration_definition_request_http.py',
        'orchestration_mutation_http.py',
        'orchestration_run_http.py',
        'orchestration_task_list_http.py',
    )
    for name in preparers:
        source = (route_dir / name).read_text()
        assert 'OrchestrationHttpPreparation' in source, name
        assert '-> tuple[' not in source, name

    route_dispatches = {
        'orchestration_definition_routes.py': 2,
        'orchestration_authoring_routes.py': 2,
        'orchestration_runtime_routes.py': 1,
        'orchestration_runtime_start_http.py': 1,
        'orchestration_task_routes.py': 1,
        'orchestration_mutation_routes.py': 2,
    }
    for name, count in route_dispatches.items():
        source = (route_dir / name).read_text()
        assert source.count('orchestration_request_response(') == count, name
        assert ', failure =' not in source, name
        assert 'assert prepared is not None' not in source, name
