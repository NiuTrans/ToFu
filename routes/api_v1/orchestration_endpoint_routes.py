"""Route-registration adapter for canonical orchestration HTTP contracts."""

from __future__ import annotations

from quart import Blueprint

from lib.orchestration.http_endpoint_contract import (
    orchestration_http_endpoint,
)


def orchestration_route(blueprint: Blueprint, name: str, **options):
    """Create a Blueprint decorator from the canonical endpoint registry."""

    if 'methods' in options:
        raise TypeError('orchestration_route owns the HTTP method')
    contract = orchestration_http_endpoint(name)
    route = blueprint.route(
        contract.route, methods=[contract.method], **options)

    def decorator(view):
        meta = getattr(view, '_api_meta', None)
        if meta is not None:
            declared = dict(meta)
            extensions = dict(declared.get('extensions', {}))
            key = 'x-tofu-response-contract'
            existing = extensions.get(key)
            if existing is not None and existing != contract.response_contract:
                raise ValueError(
                    f'Orchestration response contract conflict: {name!r}')
            extensions[key] = contract.response_contract
            declared['extensions'] = extensions
            view._api_meta = declared
        return route(view)

    return decorator


def orchestration_endpoint_path(name: str, *, method: str) -> str:
    """Return a registry path while asserting a generic route factory's verb."""

    contract = orchestration_http_endpoint(name)
    expected = str(method or '').upper()
    if contract.method != expected:
        raise ValueError(
            f'Orchestration endpoint {name!r} uses {contract.method}, '
            f'not {expected}')
    return contract.route


def orchestration_endpoint_extensions(name: str, *, method: str) -> dict:
    """Return generated OpenAPI extensions for a generic route factory."""

    orchestration_endpoint_path(name, method=method)
    contract = orchestration_http_endpoint(name)
    return {'x-tofu-response-contract': contract.response_contract}


__all__ = [
    'orchestration_endpoint_extensions',
    'orchestration_endpoint_path',
    'orchestration_route',
]
