"""Shared tests for the sidecar-backed trading connection."""

from __future__ import annotations

from copy import deepcopy

import pytest

from tofu_trading.storage import TradingConnection


class InMemoryTradingRepository:
    """Atomic document repository used by SQL-compatibility unit tests."""

    def __init__(self):
        self._items: dict[str, dict] = {}

    def list_prefix(self, prefix: str) -> list[dict]:
        return [
            deepcopy(self._items[key])
            for key in sorted(self._items)
            if key.startswith(prefix)
        ]

    def batch(self, mutations: list[dict]) -> dict:
        candidate = deepcopy(self._items)
        results = []
        for mutation in mutations:
            action = mutation["action"]
            key = (
                mutation["document"]["key"]
                if action == "put"
                else mutation["key"]
            )
            current = candidate.get(key)
            current_version = int(current["version"]) if current else 0
            expected_version = mutation.get("expected_version")
            if (
                expected_version is not None
                and int(expected_version) != current_version
            ):
                raise RuntimeError(
                    f"version conflict for {key}: "
                    f"expected {expected_version}, found {current_version}"
                )
            if action == "put":
                item = {
                    "document": deepcopy(mutation["document"]),
                    "version": current_version + 1,
                }
                candidate[key] = item
                results.append(deepcopy(item))
            elif action == "delete":
                candidate.pop(key, None)
                results.append({"key": key, "deleted": current is not None})
            else:  # pragma: no cover - production rejects this first
                raise AssertionError(f"unsupported mutation action: {action}")
        self._items = candidate
        return {"results": results}


@pytest.fixture
def trading_connection_factory():
    """Return owner-bound connections over one shared in-memory repository."""
    shared_repository = InMemoryTradingRepository()
    opened: list[TradingConnection] = []

    def create(
        owner_user_id: int = 1, *, isolated: bool = False
    ) -> TradingConnection:
        repository = (
            InMemoryTradingRepository() if isolated else shared_repository
        )
        connection = TradingConnection(
            owner_user_id, repository=repository, prepare=False
        )
        opened.append(connection)
        return connection

    yield create

    for connection in opened:
        connection.close()
