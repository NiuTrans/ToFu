"""Validated browser projection of canonical orchestration endpoints."""

from __future__ import annotations

from typing import Mapping

from lib.orchestration.browser_endpoint_policy import (
    ORCHESTRATION_CLIENT_METHODS,
    ORCHESTRATION_RESPONSE_OPTIONS,
    ORCHESTRATION_RESPONSE_REQUIRED_FIELDS,
)
from lib.orchestration.browser_endpoint_validation import (
    validate_orchestration_browser_endpoint_policy,
)
from lib.orchestration.http_endpoint_contract import (
    orchestration_http_endpoints,
)


validate_orchestration_browser_endpoint_policy(
    orchestration_http_endpoints(),
    ORCHESTRATION_RESPONSE_OPTIONS,
    ORCHESTRATION_CLIENT_METHODS,
    ORCHESTRATION_RESPONSE_REQUIRED_FIELDS,
)


def orchestration_response_options() -> Mapping[str, str]:
    return ORCHESTRATION_RESPONSE_OPTIONS


def orchestration_response_contract_dicts() -> dict[str, dict[str, object]]:
    return {
        name: {
            'optionName': option,
            'requiredFields': list(
                ORCHESTRATION_RESPONSE_REQUIRED_FIELDS.get(name, ())),
        }
        for name, option in ORCHESTRATION_RESPONSE_OPTIONS.items()
    }


def orchestration_client_methods() -> Mapping[str, tuple[str, str]]:
    return ORCHESTRATION_CLIENT_METHODS


def orchestration_browser_request_contract_dicts(
) -> dict[str, dict[str, object]]:
    """Join backend HTTP identity with its browser request policy."""
    return {
        name: {
            'resultMethod': ORCHESTRATION_CLIENT_METHODS[name][0],
            'directMethod': ORCHESTRATION_CLIENT_METHODS[name][1],
            'optionName': ORCHESTRATION_RESPONSE_OPTIONS[
                endpoint.response_contract],
            'responseContract': endpoint.response_contract,
            'responseRequiredFields': list(
                ORCHESTRATION_RESPONSE_REQUIRED_FIELDS.get(
                    endpoint.response_contract, ())),
        }
        for name, endpoint in orchestration_http_endpoints().items()
    }


__all__ = [
    'orchestration_browser_request_contract_dicts',
    'orchestration_client_methods',
    'orchestration_response_contract_dicts',
    'orchestration_response_options',
]
