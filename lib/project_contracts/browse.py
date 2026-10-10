"""Transport-independent directory-picker request admission.

Entry points: normalize_browse_request and browse_request_schema. HTTP and
control RPC consume this boundary before invoking project filesystem services;
contracts/control_rpc_v1.yaml declares the corresponding method parameters.
Dependencies: standard-library mappings and types; no runtime services.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

BROWSE_PATH_MAX_CHARS = 4096


def browse_request_schema() -> dict[str, Any]:
    """Return detached JSON-schema metadata for the public request."""
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "path": {
                "type": ["string", "null"],
                "maxLength": BROWSE_PATH_MAX_CHARS,
                "pattern": r"^[^\u0000]*$",
            },
            "showHidden": {"type": "boolean", "default": False},
        },
    }


BROWSE_REQUEST_FIELDS = frozenset(browse_request_schema()["properties"])


def normalize_browse_request(params: Mapping[str, Any]) -> tuple[str | None, bool]:
    """Validate once for either transport; blank/null paths select the home view."""
    unknown = set(params) - BROWSE_REQUEST_FIELDS
    if unknown:
        raise ValueError("Unknown project.browse parameter: " + sorted(unknown)[0])
    path = params.get("path")
    if path is not None and not isinstance(path, str):
        raise ValueError("path must be a string")
    if path is not None and (len(path) > BROWSE_PATH_MAX_CHARS or "\0" in path):
        raise ValueError("path exceeds the project browse limit")
    show_hidden = params.get("showHidden", False)
    if not isinstance(show_hidden, bool):
        raise ValueError("showHidden must be a boolean")
    return (path.strip() or None) if path is not None else None, show_hidden
