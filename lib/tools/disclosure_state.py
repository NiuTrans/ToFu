"""Bounded task-level Tool Search disclosure state.

Tool registration and provider-wire exposure have separate owners.  This
module records only schemas already returned by ``search_tools`` during one
task, so later searches can suppress an unchanged disclosure while allowing a
revised schema to be returned again. The compact ledger holds identities only;
raw schemas remain in the immutable executable catalog.

Each ledger entry also carries a bounded argument signature (required /
optional argument names, values dropped) so the composer's ``disclosed_tools``
tail block can remind the model which deferred tools it can already call after
compaction has summarized the original search result away.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any


TOOL_DISCLOSURE_STATE_CONTRACT = "tofu.tool-disclosure-state/v1"
TOOL_DISCLOSURE_STATE_TASK_KEY = "_toolDisclosureState"
TOOL_DISCLOSURE_STATE_MAXIMUM = 512


def schema_fingerprint(schema: Any) -> str:
    """Return a stable short identity for one model-facing argument schema."""
    payload = json.dumps(
        schema if isinstance(schema, Mapping) else {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


_SIGNATURE_ARGUMENT_NAME_LIMIT = 16
_SIGNATURE_ARGUMENT_NAME_LENGTH = 64


def _argument_signature(schema: Any) -> dict[str, list[str]]:
    """Return bounded required/optional argument names for one JSON schema."""
    if not isinstance(schema, Mapping):
        return {}
    properties = schema.get("properties")
    prop_names = [
        str(name)[:_SIGNATURE_ARGUMENT_NAME_LENGTH]
        for name in (properties or {})
        if isinstance(name, str)
    ] if isinstance(properties, Mapping) else []
    required_all = [
        name[:_SIGNATURE_ARGUMENT_NAME_LENGTH]
        for name in schema.get("required") or ()
        if isinstance(name, str) and name
    ]
    required_set = set(required_all)
    required = required_all[:_SIGNATURE_ARGUMENT_NAME_LIMIT]
    optional = [
        name for name in prop_names if name not in required_set
    ][:_SIGNATURE_ARGUMENT_NAME_LIMIT]
    out: dict[str, list[str]] = {}
    if required:
        out["required"] = required
    if optional:
        out["optional"] = optional
    return out


def _normalized_signature(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, list[str]] = {}
    for key in ("required", "optional"):
        names = value.get(key)
        if (not isinstance(names, Iterable)
                or isinstance(names, (str, bytes))):
            continue
        cleaned = [
            str(name)[:_SIGNATURE_ARGUMENT_NAME_LENGTH]
            for name in names
            if isinstance(name, str) and name
        ][:_SIGNATURE_ARGUMENT_NAME_LIMIT]
        if cleaned:
            out[key] = cleaned
    return out

def _normalized_entries(value: Any) -> list[dict[str, str]]:
    source = value.get("entries") if isinstance(value, Mapping) else value
    if not isinstance(source, Iterable) or isinstance(source, (str, bytes)):
        return []
    entries: list[dict[str, str]] = []
    by_name: dict[str, int] = {}
    for raw in source:
        if not isinstance(raw, Mapping):
            continue
        name = str(raw.get("name") or "").strip()[:160]
        fingerprint = str(raw.get("schemaFingerprint") or "").strip()[:64]
        if not name or not fingerprint:
            continue
        row: dict[str, Any] = {"name": name, "schemaFingerprint": fingerprint}
        signature = _normalized_signature(raw.get("signature"))
        if signature:
            row["signature"] = signature
        previous = by_name.get(name)
        if previous is None:
            by_name[name] = len(entries)
            entries.append(row)
        else:
            entries[previous] = row
    return entries[-TOOL_DISCLOSURE_STATE_MAXIMUM:]


def disclosed_entries(task: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return normalized disclosure ledger entries, oldest first."""
    return _normalized_entries(task.get(TOOL_DISCLOSURE_STATE_TASK_KEY))


def disclosed_names_for_catalog(
    task: Mapping[str, Any], catalog: Iterable[Any],
) -> frozenset[str]:
    """Return catalog names whose current schema was already disclosed."""
    fingerprints = {
        row["name"]: row["schemaFingerprint"]
        for row in _normalized_entries(task.get(TOOL_DISCLOSURE_STATE_TASK_KEY))
    }
    disclosed: set[str] = set()
    for tool in catalog:
        if not isinstance(tool, Mapping):
            continue
        function = tool.get("function")
        function = function if isinstance(function, Mapping) else tool
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        schema = function.get("parameters")
        if fingerprints.get(name) == schema_fingerprint(schema):
            disclosed.add(name)
    return frozenset(disclosed)


def record_search_items(
    task: dict[str, Any],
    items: Any,
    *,
    catalog: Iterable[Any] | None = None,
) -> None:
    """Record catalog identities for schemas returned by a successful search."""
    if not isinstance(items, list) or not items:
        return
    catalog_schemas: dict[str, Any] = {}
    for tool in catalog or ():
        if not isinstance(tool, Mapping):
            continue
        function = tool.get("function")
        function = function if isinstance(function, Mapping) else tool
        name = str(function.get("name") or "").strip()
        if name:
            catalog_schemas[name] = function.get("parameters")
    entries = _normalized_entries(task.get(TOOL_DISCLOSURE_STATE_TASK_KEY))
    order = [row["name"] for row in entries]
    fingerprints = {
        row["name"]: row["schemaFingerprint"] for row in entries
    }
    signatures = {
        row["name"]: row["signature"] for row in entries if row.get("signature")
    }
    for item in items:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip()[:160]
        if not name:
            continue
        schema = catalog_schemas.get(name, item.get("arguments_schema"))
        if name in fingerprints:
            order.remove(name)
        order.append(name)
        fingerprints[name] = schema_fingerprint(schema)
        signature = _argument_signature(schema)
        if signature:
            signatures[name] = signature
        else:
            signatures.pop(name, None)
    order = order[-TOOL_DISCLOSURE_STATE_MAXIMUM:]
    task[TOOL_DISCLOSURE_STATE_TASK_KEY] = {
        "contractVersion": TOOL_DISCLOSURE_STATE_CONTRACT,
        "entries": [
            {
                "name": name,
                "schemaFingerprint": fingerprints[name],
                **({"signature": signatures[name]}
                   if signatures.get(name) else {}),
            }
            for name in order
        ],
    }


__all__ = [
    "TOOL_DISCLOSURE_STATE_CONTRACT",
    "TOOL_DISCLOSURE_STATE_MAXIMUM",
    "TOOL_DISCLOSURE_STATE_TASK_KEY",
    "disclosed_entries",
    "disclosed_names_for_catalog",
    "record_search_items",
    "schema_fingerprint",
]
