"""Invariants for browser policies joined to orchestration HTTP endpoints."""

from __future__ import annotations

from typing import Mapping

from lib.orchestration.http_endpoint_model import OrchestrationHttpEndpoint


def validate_orchestration_browser_endpoint_policy(
    endpoints: Mapping[str, OrchestrationHttpEndpoint],
    response_options: Mapping[str, str],
    client_methods: Mapping[str, tuple[str, str]],
    response_required_fields: Mapping[str, tuple[str, ...]] | None = None,
) -> None:
    if set(client_methods) != set(endpoints):
        raise ValueError(
            'Orchestration HTTP/client method contract coverage mismatch')
    response_names = {
        endpoint.response_contract for endpoint in endpoints.values()
    }
    if set(response_options) != response_names:
        raise ValueError(
            'Orchestration HTTP/response policy coverage mismatch')
    if any(
        not result_method or not direct_method
        for result_method, direct_method in client_methods.values()
    ):
        raise ValueError('Empty orchestration browser client method')
    if any(not option for option in response_options.values()):
        raise ValueError('Empty orchestration browser response option')
    required = response_required_fields or {}
    if not set(required) <= set(response_options):
        raise ValueError('Unknown orchestration browser response field policy')
    if any(
        not fields or len(fields) != len(set(fields))
        or any(not isinstance(field, str) or not field for field in fields)
        for fields in required.values()
    ):
        raise ValueError('Invalid orchestration browser response fields')


__all__ = ['validate_orchestration_browser_endpoint_policy']
