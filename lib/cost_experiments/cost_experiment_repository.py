"""Owner-scoped repository for bounded cost-experiment outcome scans.

Entry point: :func:`scan_cost_experiment_outcomes`. It resumes the compact
Sidecar projection with an opaque record cursor, enforces one process-local
scan ceiling, and returns only report inputs. The repository depends on the
semantic storage client, never a database path or SQL dialect.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from lib.storage.errors import StorageError


_SCAN_ROWS_PER_RPC = 256
_MAX_SOURCE_ROWS = 10_000


def _protocol_error(message: str) -> StorageError:
    return StorageError("database_protocol_error", message)


def scan_cost_experiment_outcomes(
    *,
    user_id: int,
    completed_at_gte: int,
    experiment_id: str,
    limit: int,
    storage_client: Any | None = None,
) -> dict[str, Any]:
    """Return owner-visible outcomes without restarting a cold BLOB scan.

    At most ``_MAX_SOURCE_ROWS`` generic task-result records are inspected.
    Each semantic RPC advances at most ``_SCAN_ROWS_PER_RPC`` rows, so an
    interrupted read retries only its bounded page. A legacy/older Sidecar
    response without cursor fields remains a valid single-page result.
    """
    try:
        normalized_user_id = int(user_id)
        normalized_cutoff = int(completed_at_gte)
        normalized_limit = int(limit)
    except (TypeError, ValueError, OverflowError) as error:
        raise _protocol_error("Invalid cost-experiment scan bounds") from error
    if normalized_user_id <= 0 or normalized_cutoff < 0:
        raise _protocol_error("Invalid cost-experiment owner or cutoff")
    if normalized_limit < 1 or normalized_limit > _MAX_SOURCE_ROWS:
        raise _protocol_error("Invalid cost-experiment result limit")
    normalized_experiment_id = str(experiment_id or "")
    if not normalized_experiment_id or len(normalized_experiment_id) > 128:
        raise _protocol_error("Invalid cost-experiment id")

    if storage_client is None:
        from lib.storage import get_storage_client

        storage_client = get_storage_client()

    cursor = ""
    scanned = 0
    invalid = 0
    source_capped = False
    records_by_task: dict[str, dict[str, Any]] = {}
    while scanned < _MAX_SOURCE_ROWS:
        page_scan_limit = min(
            _SCAN_ROWS_PER_RPC,
            _MAX_SOURCE_ROWS - scanned,
        )
        result = storage_client.query(
            "task_results.cost_experiment_scan",
            {
                "user_id": normalized_user_id,
                "completed_at_gte": normalized_cutoff,
                "experiment_id": normalized_experiment_id,
                "limit": min(_MAX_SOURCE_ROWS, normalized_limit + 1),
                "scan_limit": page_scan_limit,
                "after_key": cursor,
            },
        )
        if not isinstance(result, Mapping):
            raise _protocol_error("Malformed cost-experiment scan response")
        page_records = result.get("records") or []
        if not isinstance(page_records, list):
            raise _protocol_error("Malformed cost-experiment scan records")
        for row in page_records:
            if not isinstance(row, Mapping):
                invalid += 1
                continue
            task_id = str(row.get("task_id") or "")
            if not task_id:
                invalid += 1
                continue
            records_by_task[task_id] = dict(row)
        try:
            invalid += max(0, int(result.get("invalid") or 0))
            page_scanned = max(0, int(result.get("scanned") or 0))
        except (TypeError, ValueError, OverflowError) as error:
            raise _protocol_error(
                "Malformed cost-experiment scan counters"
            ) from error
        scanned += page_scanned
        source_capped = source_capped or bool(result.get("capped"))

        # Compatibility with the pre-pagination semantic operation and simple
        # test adapters: absence of both fields means the response is complete.
        if "exhausted" not in result and "next_cursor" not in result:
            break
        if bool(result.get("exhausted")):
            break
        next_cursor = result.get("next_cursor")
        if (
            not isinstance(next_cursor, str)
            or not next_cursor
            or next_cursor <= cursor
            or page_scanned <= 0
        ):
            raise _protocol_error(
                "Cost-experiment scan cursor did not advance"
            )
        cursor = next_cursor
    else:
        source_capped = True

    ordered = sorted(
        records_by_task.values(),
        key=lambda item: (
            int(item.get("completed_at") or 0),
            str(item.get("task_id") or ""),
        ),
        reverse=True,
    )
    capped = source_capped or len(ordered) > normalized_limit
    return {
        "records": ordered[:normalized_limit],
        "invalid": invalid,
        "scanned": min(scanned, _MAX_SOURCE_ROWS),
        "capped": capped,
    }


__all__ = ["scan_cost_experiment_outcomes"]
