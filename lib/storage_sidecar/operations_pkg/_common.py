"""Shared payload validators, JSON codec helpers, and the OperationSpec type."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass
import time
from typing import Any, Callable

import orjson

from lib.log import get_logger
from lib.storage.errors import StorageError
from lib.storage_sidecar.projection_codec import (
    ProjectionCodecError,
    decode_projection_from_storage,
    encode_projection_for_storage,
)
from lib.storage_sidecar.reclaim_policy import requires_offline_compaction
from lib.storage.protocol import validate_finite_json_numbers
from lib.storage_sidecar.adapters.base import Session


logger = get_logger(__name__)


def _required_text(payload: Mapping[str, Any], key: str, maximum: int = 512) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise StorageError(
            "database_protocol_error", f"Invalid {key} in storage request"
        )
    return value


def _integer(
    payload: Mapping[str, Any],
    key: str,
    *,
    default: int | None = None,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    value = payload.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise StorageError(
            "database_protocol_error", f"Invalid {key} in storage request"
        )
    if minimum is not None and value < minimum:
        raise StorageError(
            "database_protocol_error", f"Invalid {key} in storage request"
        )
    if maximum is not None and value > maximum:
        raise StorageError(
            "database_protocol_error", f"Invalid {key} in storage request"
        )
    return value


def _number(
    payload: Mapping[str, Any],
    key: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    value = payload.get(key)
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not minimum <= float(value) <= maximum
    ):
        raise StorageError(
            "database_protocol_error", f"Invalid {key} in storage request"
        )
    return float(value)


def _expected_version(payload: Mapping[str, Any]) -> int | None:
    if "expected_version" not in payload:
        return None
    return _integer(payload, "expected_version", minimum=0)


def _dump(value: Any) -> bytes:
    validate_finite_json_numbers(value)
    try:
        storage_value = encode_projection_for_storage(value)
        return orjson.dumps(storage_value, option=orjson.OPT_SORT_KEYS)
    except ProjectionCodecError as exc:
        raise StorageError(
            "database_protocol_error", "Projection storage encoding is invalid"
        ) from exc
    except (TypeError, orjson.JSONEncodeError) as exc:
        raise StorageError(
            "database_protocol_error", "Storage value is not serializable"
        ) from exc


def _load(value: Any) -> Any:
    # PostgreSQL JSON/JSONB columns are decoded by psycopg before they reach
    # the adapter, while SQLite returns the canonical bytes/text we wrote.
    # Accept both representations so semantic operations stay backend-neutral.
    if value is None or isinstance(value, (list, int, float, bool)):
        return value
    if isinstance(value, dict):
        decoded = value
    else:
        if isinstance(value, memoryview):
            value = value.tobytes()
        if isinstance(value, str):
            value = value.encode("utf-8")
        decoded = orjson.loads(value)
    try:
        return decode_projection_from_storage(decoded)
    except ProjectionCodecError as exc:
        raise StorageError(
            "database_integrity", "Stored turn projection encoding is invalid"
        ) from exc


def _wire_document(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"$bytes": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {key: _wire_document(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_wire_document(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class OperationSpec:
    kind: str
    receipt_required: bool
    handler: Callable[[Session, Mapping[str, Any]], Any]
    # Command-domain hooks run after the semantic handler but before the
    # transaction commits.  They are the mandatory seam for transactional
    # outbox/change-log capture: a future handler cannot publish a state
    # change before its database mutation is durable, or forget to make that
    # state change replayable.  The callback may wrap/replace the result.
    after: Callable[
        [Session, str, Mapping[str, Any], Any], Any
    ] | None = None


def _schema_version(session: Session, _payload: Mapping[str, Any]) -> Any:
    row = session.fetch_one(
        "SELECT meta_value FROM storage_meta WHERE meta_key = ?",
        ("schema_version",),
    )
    return {"version": int(row["meta_value"]) if row else 0}


def _system_reclaim(session: Session, payload: Mapping[str, Any]) -> Any:
    """Bounded online free-page reclamation for the SQLite authority.

    With ``auto_vacuum=INCREMENTAL`` (already enabled on this authority),
    ``PRAGMA incremental_vacuum(N)`` relocates up to N live tail pages into
    freelist holes and truncates the file — the only whole-file-shrink path
    that never takes an exclusive lock, so it runs on the writer lane between
    user transactions.  Slices are bounded by pages AND a wall budget; the
    caller loops until ``freelist`` stops shrinking.  This is the sidecar-era
    successor of the legacy ``sqlite_maintenance.incremental_vacuum``, which
    only ever ran on the legacy loop — in sidecar mode free pages were never
    reclaimed at all (2026-08-20 measurement).

    PostgreSQL is intentionally a no-op: its relation bloat is autovacuum's
    domain, and a storage-level RECLAIM would fight it.
    """
    max_pages = _integer(
        payload, "max_pages", default=8192, minimum=1, maximum=1_048_576
    )
    min_free_pages = _integer(payload, "min_free_pages", default=1024, minimum=0)
    budget_ms = _integer(payload, "budget_ms", default=250, minimum=10, maximum=60_000)
    if getattr(session, "backend", "") != "sqlite":
        return {
            "reclaimed": 0,
            "skipped": "not a SQLite backend",
            "backend": getattr(session, "backend", "unknown"),
        }
    mode_row = session.fetch_one("PRAGMA auto_vacuum")
    mode = int(next(iter(mode_row.values()))) if mode_row else 0
    if mode != 2:
        # Never convert the mode implicitly: flipping auto_vacuum rewrites
        # the whole database on the next commit.
        return {
            "reclaimed": 0,
            "auto_vacuum": mode,
            "skipped": "auto_vacuum is not INCREMENTAL",
        }
    before_row = session.fetch_one("PRAGMA freelist_count")
    before = int(next(iter(before_row.values()))) if before_row else 0
    if before < min_free_pages:
        return {"reclaimed": 0, "freelist": before, "auto_vacuum": mode}
    page_count_row = session.fetch_one("PRAGMA page_count")
    page_size_row = session.fetch_one("PRAGMA page_size")
    page_count = int(next(iter(page_count_row.values()))) if page_count_row else 0
    page_size = int(next(iter(page_size_row.values()))) if page_size_row else 0
    freelist_ratio = before / max(1, page_count)
    # Incremental vacuum is a steady-state broom, not a bulk compactor.  At
    # the default 8,192-page slice/catch-up cadence, 1,048,576 free pages are
    # already at least 128 commits and roughly an hour of continuous writer
    # maintenance.  When that backlog also occupies a quarter of the file,
    # touching random tail pages online creates more user-visible risk than
    # value.  Report the explicit offline boundary without executing even one
    # relocation; the maintenance owner then keeps retention alive but stops
    # futile reclaim probes until restart.
    # An operator may enlarge one steady-state slice, but that must not turn
    # the slice knob into an implicit override of the online/offline safety
    # boundary. One million 4 KiB pages is already 4 GiB of holes.
    if requires_offline_compaction(before, page_count):
        return {
            "reclaimed": 0,
            "freelist": before,
            "auto_vacuum": mode,
            "offline_required": True,
            "page_count": page_count,
            "page_size": page_size,
            "file_bytes": page_count * page_size,
            "freelist_bytes": before * page_size,
            "freelist_ratio": round(freelist_ratio, 6),
            "reason": "bulk freelist exceeds the bounded online reclaim envelope",
        }
    target = min(before, max_pages)
    deadline = time.monotonic() + budget_ms / 1000.0
    after = before
    # A wall check around one giant ``incremental_vacuum(target)`` is not a
    # wall budget: SQLite does not return control between those pages.  On the
    # 411 GiB production authority the former 8,192-page call held the only
    # writer beyond the 2 s interactive acquisition ceiling.  Ask for exactly
    # one page per statement so the deadline is observed between relocation
    # units.  A single page is the smallest interruptible unit SQLite offers.
    while before - after < target and time.monotonic() < deadline:
        session.execute("PRAGMA incremental_vacuum(1)")
        row = session.fetch_one("PRAGMA freelist_count")
        new_after = int(next(iter(row.values()))) if row else after
        if new_after >= after:
            break
        after = new_after
    return {"reclaimed": max(0, before - after), "freelist": after, "auto_vacuum": mode}
