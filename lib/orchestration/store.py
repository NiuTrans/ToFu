"""Persistence boundary for user-authored orchestration definitions.

The HTTP routes and chat dispatcher used to know that definitions lived in a
JSON array and each implemented its own lookup/mutation loop.  This module is
the only place that owns that storage shape.  Callers deal in entries or
definition snapshots and can replace the store later without changing flow
resolution or execution code.
"""

from __future__ import annotations

import copy
import secrets
import time
from dataclasses import dataclass
from typing import Callable

from lib.config_dir import config_path
from lib.json_store import (
    JsonStoreReadError,
    read_json,
    update_json_atomic,
)


def _default_id() -> str:
    return 'orch_' + hex(int(time.time() * 1000))[2:] + secrets.token_hex(2)


@dataclass(frozen=True)
class OrchestrationStoreMutationResult:
    """Outcome of one atomic compare-and-set definition mutation."""

    entry: dict | None
    conflict: bool = False
    current_updated_at: int | None = None
    deleted: bool = False


# Compatibility alias for extensions that adopted the guarded-update result
# before deletion joined the same mutation interface.
OrchestrationStoreUpdateResult = OrchestrationStoreMutationResult


class OrchestrationStore:
    """Atomic repository for orchestration definition snapshots."""

    def __init__(self, path: str | None = None,
                 *, id_factory: Callable[[], str] | None = None):
        self.path = path or config_path('orchestrations.json')
        self._id_factory = id_factory or _default_id

    def _entries(self, value) -> list[dict]:
        if not isinstance(value, list) or not all(
                isinstance(entry, dict) for entry in value):
            raise JsonStoreReadError(
                f'invalid orchestration definition catalogue: {self.path}')
        ids = [entry.get('id') for entry in value]
        if (any(not isinstance(entry_id, str) or not entry_id for entry_id in ids)
                or len(set(ids)) != len(ids)
                or any(not isinstance(entry.get('definition'), dict)
                       for entry in value)):
            raise JsonStoreReadError(
                f'ambiguous orchestration definition catalogue: {self.path}')
        return value

    def list_entries(self) -> list[dict]:
        data = self._entries(read_json(self.path, default=[], strict=True))
        return copy.deepcopy(data)

    def get_entry(self, orchestration_id: str) -> dict | None:
        if not orchestration_id:
            return None
        for entry in self.list_entries():
            if entry.get('id') == orchestration_id:
                return entry
        return None

    def get_definition(self, orchestration_id: str) -> dict | None:
        entry = self.get_entry(orchestration_id)
        definition = (entry or {}).get('definition')
        return copy.deepcopy(definition) if isinstance(definition, dict) else None

    def create(self, definition: dict) -> dict:
        now = int(time.time() * 1000)
        entry = {
            'id': self._id_factory(),
            'name': definition.get('name'),
            'definition': copy.deepcopy(definition),
            'createdAt': now,
            'updatedAt': now,
        }

        def _mutate(entries):
            entries = self._entries(entries)
            if any(item['id'] == entry['id'] for item in entries):
                raise ValueError(
                    f'orchestration id collision: {entry["id"]}')
            entries.append(copy.deepcopy(entry))
            return entries

        update_json_atomic(self.path, _mutate, default=[], strict=True)
        return copy.deepcopy(entry)

    def update(self, orchestration_id: str, definition: dict) -> dict | None:
        """Compatibility update for callers that do not send a version."""
        return self.update_if_current(orchestration_id, definition).entry

    def update_if_current(
        self,
        orchestration_id: str,
        definition: dict,
        *,
        expected_updated_at: int | None = None,
    ) -> OrchestrationStoreMutationResult:
        """Replace one definition if its persisted version still matches.

        The lookup, comparison and mutation all happen inside the
        ``update_json_atomic`` lock.  Comparing before entering this callback
        would leave a time-of-check/time-of-use gap between concurrent server
        processes or browser tabs.
        """
        outcome: list[OrchestrationStoreMutationResult] = []

        def _mutate(entries):
            entries = self._entries(entries)
            for entry in entries:
                if not isinstance(entry, dict) or entry.get('id') != orchestration_id:
                    continue
                raw_version = entry.get('updatedAt')
                current_version = (
                    raw_version
                    if isinstance(raw_version, int)
                    and not isinstance(raw_version, bool)
                    and raw_version >= 0
                    else None
                )
                if (expected_updated_at is not None
                        and current_version != expected_updated_at):
                    outcome.append(OrchestrationStoreMutationResult(
                        None,
                        conflict=True,
                        current_updated_at=current_version,
                    ))
                    # ``None`` means no disk rewrite; the current entry stays
                    # byte-for-byte intact after a stale client loses CAS.
                    return None
                entry['name'] = definition.get('name')
                entry['definition'] = copy.deepcopy(definition)
                entry['updatedAt'] = max(
                    int(time.time() * 1000),
                    (current_version or 0) + 1,
                )
                saved = copy.deepcopy(entry)
                outcome.append(OrchestrationStoreMutationResult(
                    saved,
                    current_updated_at=saved['updatedAt'],
                ))
                break
            return entries

        update_json_atomic(self.path, _mutate, default=[], strict=True)
        return (outcome[0] if outcome
                else OrchestrationStoreMutationResult(None))

    def delete(self, orchestration_id: str) -> bool:
        """Compatibility delete for callers that do not send a version."""
        return self.delete_if_current(orchestration_id).deleted

    def delete_if_current(
        self,
        orchestration_id: str,
        *,
        expected_updated_at: int | None = None,
    ) -> OrchestrationStoreMutationResult:
        """Delete one definition only if its persisted version still matches."""
        outcome: list[OrchestrationStoreMutationResult] = []

        def _mutate(entries):
            entries = self._entries(entries)
            for index, entry in enumerate(entries):
                if (not isinstance(entry, dict)
                        or entry.get('id') != orchestration_id):
                    continue
                raw_version = entry.get('updatedAt')
                current_version = (
                    raw_version
                    if isinstance(raw_version, int)
                    and not isinstance(raw_version, bool)
                    and raw_version >= 0
                    else None
                )
                if (expected_updated_at is not None
                        and current_version != expected_updated_at):
                    outcome.append(OrchestrationStoreMutationResult(
                        None,
                        conflict=True,
                        current_updated_at=current_version,
                    ))
                    return None
                del entries[index]
                outcome.append(OrchestrationStoreMutationResult(
                    None,
                    deleted=True,
                    current_updated_at=current_version,
                ))
                return entries
            return None

        update_json_atomic(self.path, _mutate, default=[], strict=True)
        return (outcome[0] if outcome
                else OrchestrationStoreMutationResult(None))


__all__ = [
    'OrchestrationStore',
    'OrchestrationStoreMutationResult', 'OrchestrationStoreUpdateResult',
]
