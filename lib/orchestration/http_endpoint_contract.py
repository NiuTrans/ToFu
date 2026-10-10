"""Public interface for canonical orchestration HTTP endpoint contracts."""

from __future__ import annotations

from typing import Mapping

from lib.orchestration.http_endpoint_model import (
    OrchestrationHttpEndpoint as OrchestrationHttpEndpoint,
)
from lib.orchestration.http_endpoint_registry import (
    ORCHESTRATION_HTTP_ENDPOINTS,
)
from lib.orchestration.http_endpoint_validation import (
    validate_orchestration_http_endpoints,
)


validate_orchestration_http_endpoints(ORCHESTRATION_HTTP_ENDPOINTS)


def orchestration_http_endpoint(name: str) -> OrchestrationHttpEndpoint:
    """Return one endpoint contract, failing loudly for unknown names."""
    try:
        return ORCHESTRATION_HTTP_ENDPOINTS[name]
    except KeyError as exc:
        raise KeyError(
            f'Unknown orchestration HTTP endpoint: {name!r}') from exc


def orchestration_http_endpoints() -> Mapping[str, OrchestrationHttpEndpoint]:
    """Return the immutable endpoint registry."""
    return ORCHESTRATION_HTTP_ENDPOINTS


def orchestration_http_endpoint_dicts() -> dict[str, dict[str, object]]:
    """Return a serialization-safe snapshot for generators and parity tests."""
    return {
        name: contract.as_dict()
        for name, contract in ORCHESTRATION_HTTP_ENDPOINTS.items()
    }


__all__ = [
    'OrchestrationHttpEndpoint',
    'orchestration_http_endpoint',
    'orchestration_http_endpoint_dicts',
    'orchestration_http_endpoints',
]
