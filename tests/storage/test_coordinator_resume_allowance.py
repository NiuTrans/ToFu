"""Accepted successors conserve usage through each real segmented authority."""
import pytest

from tests.project_mod.test_project_dispatch_repository import context as context
from tests.project_mod.test_coordinator_workers import (
    runtime as runtime, base_runtime as base_runtime,
)
from tests.storage.test_coordinator_segmented_allowance import segmented_runtime as segmented_runtime
from tests.project_mod import test_coordinator_request_budget as contracts

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api


@pytest.fixture(autouse=True)
def coordinator_authority_owner(context, monkeypatch):
    # The coordinator contracts have one explicit owner. Native Hello must
    # admit that same identity instead of the benchmark fixture's default.
    from scripts import tofudb_perf_compare
    monkeypatch.setattr(tofudb_perf_compare, '_OWNER_ID', context.repo.owner)


@pytest.mark.parametrize(('operation', 'status', 'cause'), [
    ('continue', 'failed', 'generation_error'),
    ('regenerate', 'completed', 'model_finished'),
])
def test_resume_preserves_durable_occurrences(segmented_runtime, operation, status, cause):
    contracts.test_accepted_resume_continues_the_worker_tool_reservation_counter(
        segmented_runtime, operation, status, cause)
