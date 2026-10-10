"""Sidecar repository for owner-scoped orchestration definitions.

This is the only persistence adapter for authored DAG definitions.  It carries
an explicit repository owner and tenant boundary on every semantic operation;
HTTP routes, chat selection, and subflow lookup never read JSON files or issue
SQL.  Compare-and-set update/delete decisions execute inside one Sidecar
transaction.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
import secrets
import time
from typing import Callable
import uuid

from lib.identity import require_user_id
from lib.orchestration.definition_version import require_definition_version
from lib.storage.client import StorageClient


def _default_id() -> str:
    return "orch_" + secrets.token_hex(8)


@dataclass(frozen=True, slots=True)
class OrchestrationStoreMutationResult:
    """Outcome of one atomic compare-and-set definition mutation."""

    entry: dict | None
    conflict: bool = False
    current_updated_at: int | None = None
    deleted: bool = False


class OrchestrationStore:
    """Complete definition repository bound to one authenticated owner."""

    def __init__(
        self,
        owner_user_id: int,
        *,
        tenant_id: str | None = None,
        id_factory: Callable[[], str] | None = None,
        client: Callable[..., StorageClient] | None = None,
    ):
        self.owner_user_id = require_user_id(
            owner_user_id, context="orchestration definition owner")
        self.tenant_id = str(tenant_id or "").strip()
        self._id_factory = id_factory or _default_id
        if client is None:
            from lib.storage import get_storage_client

            client = get_storage_client
        self._client = client

    def _boundary(self) -> dict[str, object]:
        return {
            "user_id": self.owner_user_id,
            "tenant_id": self.tenant_id,
        }

    def _storage(self, *, write: bool = False) -> StorageClient:
        return self._client(write=write)

    @staticmethod
    def _command_id(operation: str, orchestration_id: str) -> str:
        return f"{operation}:{orchestration_id}:{uuid.uuid4().hex}"

    def list_entries(self) -> list[dict]:
        rows = self._storage().query(
            "orchestration.definition.list", self._boundary())
        return copy.deepcopy(rows)

    def get_entry(self, orchestration_id: str) -> dict | None:
        if not orchestration_id:
            return None
        row = self._storage().query(
            "orchestration.definition.get",
            {
                **self._boundary(),
                "orchestration_id": orchestration_id,
            },
        )
        return None if row is None else copy.deepcopy(row)

    def get_definition(self, orchestration_id: str) -> dict | None:
        entry = self.get_entry(orchestration_id)
        definition = (entry or {}).get("definition")
        return copy.deepcopy(definition) if isinstance(definition, dict) else None

    def create(self, definition: dict) -> dict:
        orchestration_id = self._id_factory()
        if not isinstance(orchestration_id, str) or not orchestration_id:
            raise ValueError("orchestration id factory returned an invalid id")
        row = self._storage(write=True).command(
            "orchestration.definition.create",
            {
                **self._boundary(),
                "orchestration_id": orchestration_id,
                "definition": copy.deepcopy(definition),
                "now_ms": int(time.time() * 1000),
            },
            self._command_id("orchestration.definition.create", orchestration_id),
        )
        if not isinstance(row, dict):
            raise RuntimeError("orchestration definition create returned no entry")
        return copy.deepcopy(row)

    @staticmethod
    def _mutation_result(value: object) -> OrchestrationStoreMutationResult:
        if not isinstance(value, dict):
            raise RuntimeError("orchestration definition mutation returned no result")
        current = value.get("current_updated_at")
        return OrchestrationStoreMutationResult(
            entry=(
                copy.deepcopy(value["entry"])
                if isinstance(value.get("entry"), dict)
                else None
            ),
            conflict=bool(value.get("conflict")),
            current_updated_at=(
                current
                if isinstance(current, int) and not isinstance(current, bool)
                else None
            ),
            deleted=bool(value.get("deleted")),
        )

    def update_if_current(
        self,
        orchestration_id: str,
        definition: dict,
        *,
        expected_updated_at: int,
    ) -> OrchestrationStoreMutationResult:
        expected_updated_at = require_definition_version(
            expected_updated_at, field="expected_updated_at")
        payload: dict[str, object] = {
            **self._boundary(),
            "orchestration_id": orchestration_id,
            "definition": copy.deepcopy(definition),
            "now_ms": int(time.time() * 1000),
            "expected_updated_at": expected_updated_at,
        }
        value = self._storage(write=True).command(
            "orchestration.definition.update",
            payload,
            self._command_id("orchestration.definition.update", orchestration_id),
        )
        return self._mutation_result(value)

    def delete_if_current(
        self,
        orchestration_id: str,
        *,
        expected_updated_at: int,
    ) -> OrchestrationStoreMutationResult:
        expected_updated_at = require_definition_version(
            expected_updated_at, field="expected_updated_at")
        payload: dict[str, object] = {
            **self._boundary(),
            "orchestration_id": orchestration_id,
            "expected_updated_at": expected_updated_at,
        }
        value = self._storage(write=True).command(
            "orchestration.definition.delete",
            payload,
            self._command_id("orchestration.definition.delete", orchestration_id),
        )
        return self._mutation_result(value)


__all__ = ["OrchestrationStore", "OrchestrationStoreMutationResult"]
