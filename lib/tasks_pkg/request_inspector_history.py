"""Bounded chronological continuation for selected long-history snapshots.

Dependencies: semantic event queries, the canonical snapshot replay decoder and
the caller's existing byte-bounded inspector cache. No disk or model authority;
lost continuation state can be rebuilt from the durable event source.
"""

from __future__ import annotations

import threading
import time

from lib.task_event_contract import EVENT_READ_GUARD_SCOPED_CONTRACT
from lib.tasks_pkg.snapshot_delta import SnapshotReplayState

SNAPSHOT_HISTORY_TYPES = ("messages_snapshot", "tool_wire_projection")


def is_event_page_size_error(exc):
    """Size rejection from event.list, never generic pressure or corruption.

    Native dispatch intentionally omits internal error text. Its deterministic
    materialized-page rejection therefore uses the public payload-size code.
    Retain the specific-text legacy path for older SQL/native peers.
    """
    code = getattr(exc, "code", "")
    return code == "storage_payload_too_large" or (
        code == "database_unavailable" and "event page exceeds 8 MiB" in str(exc)
    )


def read_source_guard(client, task_id):
    """Require the exact replay dependency scope before admitting pages."""
    value = client.query(
        "event.bounds",
        {
            "task_id": task_id,
            "include_read_guard": True,
            "types": list(SNAPSHOT_HISTORY_TYPES),
        },
        deadline=5,
    )
    if not isinstance(value, dict):
        raise ValueError("Event source bounds are unavailable")
    guard = value.get("read_guard")
    if (
        not isinstance(guard, dict)
        or guard.get("contract") != EVENT_READ_GUARD_SCOPED_CONTRACT
        or guard.get("types") != list(SNAPSHOT_HISTORY_TYPES)
        or not isinstance(guard.get("stamp"), str)
        or len(guard["stamp"]) != 64
        or any(char not in "0123456789abcdef" for char in guard["stamp"])
    ):
        raise ValueError("Event source guard is unavailable")
    for name in ("retained_count", "base_cursor", "next_cursor"):
        if type(value.get(name)) is not int or value[name] < 0:
            raise ValueError("Event source bounds are invalid")
    if (
        value["retained_count"] == 0
        and (value["base_cursor"] != 0 or value["next_cursor"] != 0)
    ) or (value["retained_count"] > 0 and value["next_cursor"] <= value["base_cursor"]):
        raise ValueError("Event source bounds are invalid")
    return value


class SnapshotHistoryRead:
    """One source-fenced scan; retains dependencies and selected results only."""

    def __init__(self, task_id, guard, *, rounds, turn, kind, last):
        self.task_id = task_id
        self.guard = guard
        self.rounds = frozenset(rounds) if rounds is not None else None
        self.turn = turn
        self.kind = kind
        self.last = last
        self.after = -1
        self.page_limit = 1000
        self.scanned = 0
        self.complete = False
        self.index = SnapshotReplayState()
        self.selected = {}
        self.wire_rows = []
        self.lock = threading.Lock()

    def cache_components(self):
        return (
            self.index,
            self.selected,
            self.wire_rows,
            self.guard,
            self.task_id,
            self.rounds,
            self.turn,
            self.kind,
        )

    def _accept(self, row, kind_of):
        sequence = row.get("sequence")
        payload = row.get("event")
        if (
            type(sequence) is not int
            or sequence <= self.after
            or sequence < 0
            or not isinstance(payload, dict)
        ):
            raise ValueError("Event page did not advance with valid rows")
        if sequence >= self.guard["next_cursor"]:
            raise ValueError("Event page exceeded its source fence")
        event_type = payload.get("type")
        event = {
            "event_id": sequence,
            "type": event_type,
            "payload": payload,
            "ts_ms": int(row.get("created_at_ms") or 0),
        }
        if event_type == "messages_snapshot":
            self.index.append(sequence, event)
            match = (
                (self.kind is None or kind_of(payload) == self.kind)
                and (not self.turn or payload.get("turn", "") == self.turn)
                and (self.rounds is None or str(payload.get("roundNum")) in self.rounds)
            )
            if match:
                address = "last" if self.last else str(payload.get("roundNum"))
                previous = self.selected.get(address)
                if previous is not None:
                    self.index.discard(previous["event_id"])
                self.selected[address] = event
            else:
                self.index.discard(sequence)
        elif (
            event_type == "tool_wire_projection"
            and self.rounds is not None
            and str(payload.get("roundNum")) in self.rounds
            and (not self.turn or payload.get("turn", "") == self.turn)
        ):
            self.wire_rows.append(event)
        # Advance only after decoding succeeds; malformed rows must never be
        # skipped by a later continuation.
        self.after = sequence
        self.scanned += 1

    def advance(
        self,
        client,
        *,
        kind_of,
        measure,
        work_budget,
        event_budget,
        byte_budget,
        deadline,
    ):
        """Consume one bounded slice. A budget boundary does not prove EOF."""
        consumed = 0
        decoded_bytes = 0
        while not self.complete:
            if self.after >= self.guard["next_cursor"] - 1:
                self.complete = True
                break
            remaining = deadline - time.monotonic()
            if (
                consumed >= event_budget
                or decoded_bytes >= byte_budget
                or remaining <= 0
            ):
                break
            limit = min(self.page_limit, event_budget - consumed)
            try:
                rows = client.query(
                    "event.list",
                    {
                        "task_id": self.task_id,
                        "after_sequence": self.after,
                        "limit": limit,
                        "types": list(SNAPSHOT_HISTORY_TYPES),
                    },
                    deadline=min(5, remaining),
                )
            except Exception as exc:
                if (
                    is_event_page_size_error(exc)
                    and self.page_limit > 1
                ):
                    self.page_limit = max(1, self.page_limit // 2)
                    continue
                raise
            if not isinstance(rows, list) or len(rows) > limit:
                raise ValueError("Event page is invalid")
            if not rows:
                self.complete = True
                break
            decoded_bytes += measure(rows, byte_budget)
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("Event row is invalid")
                self._accept(row, kind_of)
            consumed += len(rows)
            if measure(self, work_budget) > work_budget:
                raise ValueError("Snapshot dependencies exceed the read memory budget")
            if len(rows) < limit:
                self.complete = True
        return self.complete

    def event_rows(self):
        if not self.complete:
            raise ValueError("Snapshot source scan is not complete")
        return sorted(
            [
                dict(row, payload=self.index.payload(row["event_id"]))
                for row in self.selected.values()
            ]
            + self.wire_rows,
            key=lambda row: row["event_id"],
        )
