"""Structured, bounded previews supplied by tool-result producers.

This module defines the model-neutral sidecar shape used when a tool's legacy
text result contains several independently meaningful items.  The raw tool
result remains the durable artifact payload; projection items exist only until
the result-envelope boundary has produced the bounded model-visible value.

Entry points:
``file_read_result_projection_item`` builds one bounded ``read_files`` item;
``normalize_file_read_projection_items`` validates producer items and supplies
an identity-only fallback from the original tool arguments.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


FILE_READ_RESULT_ITEM_TYPE = "file_read/v1"
TOOL_RESULT_PROJECTION_ITEMS_KEY = "_toolResultProjectionItems"
TOOL_RESULT_PRODUCER_METADATA_KEY = "_toolResultProducerMetadata"

_FILE_READ_SOURCE_PREVIEW_CHARS = 8_192
_FILE_READ_PATH_CHARS = 384
_FILE_READ_MAX_ITEMS = 20
_FILE_READ_STATUSES = frozenset({
    "ok", "partial", "error", "skipped", "unknown",
})


def _clip_middle(value: Any, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    head = max(1, (limit - 1) // 2)
    return text[:head] + "…" + text[-(limit - head - 1):]


def _read_status(text: str) -> str:
    stripped = text.lstrip()
    if stripped.startswith(("Error:", "File not found:", "Error reading ")):
        return "error"
    diagnostic = stripped[:512] + stripped[-512:]
    if any(marker in diagnostic for marker in (
            "batch budget exceeded", "[truncated", "… [truncated",
            "[DATA FILE — showing first", "[Binary file:")):
        return "partial"
    return "ok"


def file_read_result_projection_item(
    *,
    index: int,
    path: Any,
    result: Any,
    start_line: Any = None,
    end_line: Any = None,
    status: str | None = None,
) -> dict[str, Any]:
    """Build one bounded per-file preview without retaining another raw copy."""
    media = ""
    if isinstance(result, Mapping) and result.get("__screenshot__"):
        text = str(result.get("_text_fallback") or "Image loaded.")
        media = str(result.get("format") or "image")[:32]
    elif isinstance(result, str):
        text = result
    else:
        text = str(result)

    effective_status = str(status or _read_status(text)).strip().lower()
    if effective_status not in _FILE_READ_STATUSES:
        effective_status = "unknown"

    preview = text[:_FILE_READ_SOURCE_PREVIEW_CHARS]
    requested_range: dict[str, int] = {}
    for source, target in ((start_line, "startLine"), (end_line, "endLine")):
        if source is None:
            continue
        try:
            requested_range[target] = int(source)
        except (TypeError, ValueError):
            continue

    item: dict[str, Any] = {
        "type": FILE_READ_RESULT_ITEM_TYPE,
        "index": max(1, int(index or 1)),
        "path": _clip_middle(path, _FILE_READ_PATH_CHARS),
        "status": effective_status,
        "rawBytes": len(text.encode("utf-8", errors="replace")),
        "preview": preview,
        "previewTruncated": len(preview) < len(text),
    }
    if requested_range:
        item["requestedRange"] = requested_range
    if media:
        item["media"] = media
    return item


def _requested_file_specs(tool_arguments: Mapping[str, Any] | None) -> list[dict]:
    if not isinstance(tool_arguments, Mapping):
        return []
    reads = tool_arguments.get("reads")
    if reads is None and tool_arguments.get("path") is not None:
        reads = [tool_arguments]
    if not isinstance(reads, Sequence) or isinstance(reads, (str, bytes)):
        return []

    requested: list[dict] = []
    for raw in reads[:_FILE_READ_MAX_ITEMS]:
        if isinstance(raw, Mapping) and raw.get("path") is not None:
            requested.append({
                "path": str(raw.get("path") or ""),
                "start_line": raw.get("start_line"),
                "end_line": raw.get("end_line"),
            })
        elif isinstance(raw, str) and raw.strip():
            requested.append({"path": raw.strip()})
    return requested


def normalize_file_read_projection_items(
    raw_items: Sequence[Any] | None,
    tool_arguments: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return ordered file items and never silently omit a requested path.

    Producer items are authoritative for status and preview.  Original tool
    arguments are only an identity fallback for execution adapters that cannot
    yet emit sidecar items, and to name a requested path missing from a partial
    producer result without falsely claiming that its read succeeded.
    """
    normalized: list[dict[str, Any]] = []
    if isinstance(raw_items, Sequence) and not isinstance(
            raw_items, (str, bytes)):
        for position, raw in enumerate(raw_items[:_FILE_READ_MAX_ITEMS], 1):
            if not isinstance(raw, Mapping):
                continue
            if raw.get("type") != FILE_READ_RESULT_ITEM_TYPE:
                continue
            normalized.append(file_read_result_projection_item(
                index=raw.get("index") or position,
                path=raw.get("path") or "?",
                result=str(raw.get("preview") or ""),
                start_line=(raw.get("requestedRange") or {}).get("startLine")
                if isinstance(raw.get("requestedRange"), Mapping) else None,
                end_line=(raw.get("requestedRange") or {}).get("endLine")
                if isinstance(raw.get("requestedRange"), Mapping) else None,
                status=str(raw.get("status") or "unknown"),
            ))
            item = normalized[-1]
            try:
                item["rawBytes"] = max(0, int(raw.get("rawBytes") or 0))
            except (TypeError, ValueError):
                pass
            item["previewTruncated"] = bool(
                raw.get("previewTruncated") or item["previewTruncated"])
            if raw.get("media"):
                item["media"] = str(raw.get("media"))[:32]

    requested = _requested_file_specs(tool_arguments)
    represented_paths = {str(item.get("path") or "") for item in normalized}
    for spec in requested:
        path = _clip_middle(spec.get("path"), _FILE_READ_PATH_CHARS)
        if path in represented_paths:
            continue
        normalized.append(file_read_result_projection_item(
            index=len(normalized) + 1,
            path=path,
            result=("No independently attributable preview was emitted for "
                    "this requested file."),
            start_line=spec.get("start_line"),
            end_line=spec.get("end_line"),
            status="unknown",
        ))
        represented_paths.add(path)

    normalized.sort(key=lambda item: int(item.get("index") or 0))
    for index, item in enumerate(normalized[:_FILE_READ_MAX_ITEMS], 1):
        item["index"] = index
    return normalized[:_FILE_READ_MAX_ITEMS]


def is_file_read_projection(items: Sequence[Any] | None) -> bool:
    return bool(items) and all(
        isinstance(item, Mapping)
        and item.get("type") == FILE_READ_RESULT_ITEM_TYPE
        for item in items
    )


__all__ = [
    "FILE_READ_RESULT_ITEM_TYPE",
    "TOOL_RESULT_PRODUCER_METADATA_KEY",
    "TOOL_RESULT_PROJECTION_ITEMS_KEY",
    "file_read_result_projection_item",
    "is_file_read_projection",
    "normalize_file_read_projection_items",
]
