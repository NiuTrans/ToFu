"""Storage operations reject a missing owner instead of selecting user 1."""

from __future__ import annotations

import pytest

from lib.storage.errors import StorageError
from lib.storage_sidecar.operations_pkg._conversations import (
    _conversation_count,
    _conversation_identity,
    _conversation_list,
)
from lib.storage_sidecar.operations_pkg._records import (
    _task_results_cost_experiment_scan,
)


pytestmark = pytest.mark.unit


def test_conversation_identity_requires_explicit_owner() -> None:
    with pytest.raises(StorageError, match="user_id"):
        _conversation_identity({"conv_id": "conv-owner-required"})


@pytest.mark.parametrize("operation", (_conversation_list, _conversation_count))
@pytest.mark.parametrize("payload", ({}, {"user_id": None}))
def test_conversation_collection_reads_require_explicit_owner(
    operation,
    payload,
) -> None:
    class SessionMustNotRun:
        backend = "sqlite"

        def fetch_all(self, *_args: object, **_kwargs: object) -> list[object]:
            raise AssertionError("missing-owner request reached storage")

        def fetch_one(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("missing-owner request reached storage")

    with pytest.raises(StorageError, match="user_id"):
        operation(SessionMustNotRun(), payload)


def test_cost_experiment_scan_requires_explicit_owner() -> None:
    class SessionMustNotRun:
        backend = "sqlite"

        def fetch_all(self, *_args: object, **_kwargs: object) -> list[object]:
            raise AssertionError("missing-owner request reached storage")

    with pytest.raises(StorageError, match="user_id"):
        _task_results_cost_experiment_scan(
            SessionMustNotRun(),  # type: ignore[arg-type]
            {
                "completed_at_gte": 0,
                "experiment_id": "owner-required",
            },
        )
