"""Startup invariants for the orchestration HTTP endpoint registry."""

from __future__ import annotations

import re
from typing import Mapping

from lib.orchestration.definition_contract_registry import (
    definition_write_contract,
)
from lib.orchestration.http_endpoint_model import OrchestrationHttpEndpoint


def validate_orchestration_http_endpoints(
    endpoints: Mapping[str, OrchestrationHttpEndpoint],
) -> None:
    definition_write = definition_write_contract()
    identities: set[tuple[str, str]] = set()
    for name, contract in endpoints.items():
        if not name or name.strip() != name:
            raise ValueError(f'Invalid orchestration endpoint name: {name!r}')
        if not contract.route.startswith('/api/v1/orchestrations'):
            raise ValueError(
                f'Orchestration endpoint escapes API namespace: {name!r}')
        if contract.method not in {'GET', 'POST', 'PUT', 'DELETE'}:
            raise ValueError(
                f'Unsupported orchestration HTTP method: {contract.method!r}')
        if not re.fullmatch(r'[a-z][a-z0-9-]*', contract.response_contract):
            raise ValueError(
                f'Invalid orchestration response contract: {name!r}')
        for kind, arguments in (
            ('path', contract.path_args),
            ('query', contract.query_args), ('body', contract.body_args),
        ):
            fields = tuple(field for field, _index in arguments)
            indices = tuple(index for _field, index in arguments)
            if (len(set(fields)) != len(fields)
                    or len(set(indices)) != len(indices)
                    or any(not field or field.strip() != field
                           for field in fields)
                    or any(not isinstance(index, int) or index < 0
                           for index in indices)):
                raise ValueError(
                    f'Invalid orchestration {kind} mapping: {name!r}')
        placeholders = tuple(re.findall(
            r'<(?:[^:<>]+:)?([^<>]+)>', contract.route))
        if placeholders != contract.path_fields:
            raise ValueError(f'Orchestration path mapping drift: {name!r}')
        scalar_args = (
            contract.body_arg,
            contract.request_options_arg,
            contract.write_version_arg,
            contract.write_contract_arg,
        )
        if any(
            value is not None and (
                not isinstance(value, int) or isinstance(value, bool)
                or value < 0)
            for value in scalar_args
        ):
            raise ValueError(
                f'Invalid orchestration positional argument: {name!r}')
        if contract.body_args and contract.body_arg is not None:
            raise ValueError(f'Ambiguous orchestration body mapping: {name!r}')
        payload_indices = {
            index
            for arguments in (
                contract.path_args, contract.query_args, contract.body_args)
            for _field, index in arguments
        }
        if contract.body_arg is not None:
            payload_indices.add(contract.body_arg)
        if contract.request_options_arg in payload_indices:
            raise ValueError(
                f'Orchestration request options overlap payload: {name!r}')
        write_operations = {'create', *definition_write['operations']}
        if (contract.write_operation
                and contract.write_operation not in write_operations):
            raise ValueError(f'Invalid definition write operation: {name!r}')
        guarded = contract.write_operation in definition_write['operations']
        write_args = (
            contract.write_version_arg,
            contract.write_contract_arg,
        )
        if ((guarded and any(value is None for value in write_args))
                or (not guarded and any(
                    value is not None for value in write_args))):
            raise ValueError(f'Incomplete definition write mapping: {name!r}')
        identity = (contract.route, contract.method)
        if identity in identities:
            raise ValueError(
                f'Duplicate orchestration endpoint identity: {identity!r}')
        identities.add(identity)


__all__ = ['validate_orchestration_http_endpoints']
