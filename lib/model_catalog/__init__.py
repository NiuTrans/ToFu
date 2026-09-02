"""Pure normalized model-catalog domain.

This package is the single translation boundary between legacy provider
snapshots (``providers[].models``) and the authored catalog persisted as
``server_config.json.model_catalog``.  It performs no file I/O and activates
no dispatcher/pricing state; routes own persistence and hot reload.
"""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, cast

from lib.model_registration import (
    ModelRegistrationError,
    normalize_model_entry,  # pyright: ignore[reportUnknownVariableType]
)


CONTRACT_VERSION = 'tofu.model-catalog/v1'
MAX_MODELS = 1_024
MAX_OFFERINGS = 4_096
MAX_IDENTIFIER_LENGTH = 256

JsonObject = dict[str, Any]


class ModelCatalogError(ValueError):
    """A catalog does not satisfy the normalized v1 contract."""


def _identifier(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise ModelCatalogError(f'{field} must be a string')
    identifier = value.strip()
    if not identifier or len(identifier) > MAX_IDENTIFIER_LENGTH:
        raise ModelCatalogError(
            f'{field} must be 1..{MAX_IDENTIFIER_LENGTH} characters')
    if any(ord(character) < 32 for character in identifier):
        raise ModelCatalogError(f'{field} contains control characters')
    return identifier


def _mapping(value: Any, *, field: str) -> JsonObject:
    if not isinstance(value, Mapping):
        raise ModelCatalogError(f'{field} must be an object')
    result: JsonObject = {}
    for raw_key, item in value.items():
        if not isinstance(raw_key, str):
            raise ModelCatalogError(f'{field} keys must be strings')
        result[raw_key] = item
    return result


def _string_list(
    value: Any, *, field: str, reject_duplicates: bool = False,
) -> list[str]:
    if not isinstance(value, list):
        raise ModelCatalogError(f'{field} must be an array')
    result: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        item = _identifier(raw, field=f'{field}[{index}]')
        if item in seen:
            if reject_duplicates:
                raise ModelCatalogError(f'{field} contains duplicate {item!r}')
            continue
        seen.add(item)
        result.append(item)
    return result


def _provider_id(provider: Mapping[str, Any]) -> str:
    raw = provider.get('id') or provider.get('key') or provider.get('brand')
    return _identifier(raw, field='provider.id')


def offering_id(provider_id: str, model_id: str) -> str:
    """Return the deterministic provider/model offering identity."""
    provider = _identifier(provider_id, field='provider_id')
    model = _identifier(model_id, field='model_id')
    digest = hashlib.sha256(f'{provider}\0{model}'.encode()).hexdigest()
    return f'off_{digest}'


def strip_provider_models(
    providers: Iterable[Mapping[str, Any]],
) -> list[JsonObject]:
    """Deep-copy provider shells without their derived model projection."""
    if isinstance(providers, (str, bytes, Mapping)):
        raise ModelCatalogError('providers must be an array')
    shells: list[JsonObject] = []
    seen: set[str] = set()
    for raw in providers:
        provider = _mapping(raw, field='provider')
        provider_id = _provider_id(provider)
        if provider_id in seen:
            raise ModelCatalogError(f'duplicate provider id: {provider_id}')
        seen.add(provider_id)
        shell = copy.deepcopy(provider)
        shell.pop('models', None)
        shells.append(shell)
    return shells


def provider_shells(config: Mapping[str, Any]) -> list[JsonObject]:
    """Return validated provider shells from one server-config document."""
    providers = config.get('providers') or []
    if not isinstance(providers, list):
        raise ModelCatalogError('providers must be an array')
    return strip_provider_models(providers)


def public_provider_metadata(
    config: Mapping[str, Any],
) -> dict[str, JsonObject]:
    """Return the allow-listed provider metadata safe for catalog clients."""
    safe_fields = ('name', 'label', 'brand', 'protocol', 'enabled')
    result: dict[str, JsonObject] = {}
    for shell in provider_shells(config):
        provider_id = _provider_id(shell)
        row: dict[str, Any] = {'id': provider_id}
        for field in safe_fields:
            value = shell.get(field)
            if isinstance(value, (str, bool)):
                row[field] = value
        result[provider_id] = row
    return result


def _normalized_configuration(
    raw: Mapping[str, Any], *, field: str,
) -> JsonObject:
    sensitive = {
        'api_key', 'api_keys', 'base_url', 'password', 'secret', 'token',
        'headers', 'extra_headers',
    }
    present = sensitive.intersection(raw)
    if present:
        names = ', '.join(sorted(present))
        raise ModelCatalogError(f'{field} contains provider secrets: {names}')
    for list_field in ('request_ids', 'aliases', 'capabilities'):
        value = raw.get(list_field)
        if value is None:
            continue
        if not isinstance(value, list):
            raise ModelCatalogError(f'{field}.{list_field} must be an array')
        if len(value) > 256:
            raise ModelCatalogError(
                f'{field}.{list_field} exceeds limit 256')
        for index, item in enumerate(value):
            _identifier(item, field=f'{field}.{list_field}[{index}]')
    for numeric_field in ('rpm', 'latency', 'context_window'):
        if isinstance(raw.get(numeric_field), bool):
            raise ModelCatalogError(
                f'{field}.{numeric_field} must be numeric')
    pricing = raw.get('pricing')
    if isinstance(pricing, Mapping):
        for numeric_field in (
            'input', 'output', 'cacheWriteMul', 'cacheReadMul',
        ):
            if isinstance(pricing.get(numeric_field), bool):
                raise ModelCatalogError(
                    f'{field}.pricing.{numeric_field} must be numeric')
    model_id = _identifier(raw.get('model_id'), field=f'{field}.model_id')
    entry = copy.deepcopy(dict(raw))
    entry['model_id'] = model_id
    entry.pop('enabled', None)
    entry.pop('_catalog_revision', None)
    try:
        normalized = cast(
            JsonObject,
            normalize_model_entry(entry, reject_legacy_cost=True),
        )
    except ModelRegistrationError as exc:
        raise ModelCatalogError(f'{field}: {exc}') from exc
    normalized.pop('model_id', None)
    return normalized


def normalize_catalog(
    raw: Mapping[str, Any], *, provider_ids: Iterable[str] | None = None,
    revision: int | None = None,
) -> JsonObject:
    """Validate and canonicalize a complete catalog document.

    Map keys and body identities must match exactly. Every offering belongs to
    one known provider and one logical model, and every route lists exactly the
    offerings for its model. Logical ``enabled`` is recomputed from offerings;
    a submitted disabled logical model cascades to all of its offerings.
    """
    catalog = _mapping(raw, field='catalog')
    allowed_root = {
        'contract_version', 'revision', 'models', 'offerings', 'routes',
    }
    unknown_root = set(catalog) - allowed_root
    if unknown_root:
        raise ModelCatalogError(
            f'catalog has unknown fields: {", ".join(sorted(unknown_root))}')
    if catalog.get('contract_version') != CONTRACT_VERSION:
        raise ModelCatalogError(
            f'contract_version must be {CONTRACT_VERSION!r}')
    raw_revision = catalog.get('revision') if revision is None else revision
    if isinstance(raw_revision, bool) or not isinstance(raw_revision, int):
        raise ModelCatalogError('revision must be an integer')
    if raw_revision < 0:
        raise ModelCatalogError('revision must be non-negative')

    raw_models = _mapping(catalog.get('models'), field='models')
    raw_offerings = _mapping(catalog.get('offerings'), field='offerings')
    raw_routes = _mapping(catalog.get('routes'), field='routes')
    if len(raw_models) > MAX_MODELS:
        raise ModelCatalogError(f'models exceeds limit {MAX_MODELS}')
    if len(raw_offerings) > MAX_OFFERINGS:
        raise ModelCatalogError(f'offerings exceeds limit {MAX_OFFERINGS}')
    if len(raw_routes) != len(raw_models):
        raise ModelCatalogError('routes must contain exactly one row per model')

    known_providers = None
    if provider_ids is not None:
        known_providers = {
            _identifier(value, field='provider_id') for value in provider_ids
        }

    models: dict[str, JsonObject] = {}
    submitted_model_enabled: dict[str, bool] = {}
    for map_key, value in raw_models.items():
        model_id = _identifier(map_key, field='models key')
        model = _mapping(value, field=f'models.{model_id}')
        body_id = _identifier(
            model.get('model_id'), field=f'models.{model_id}.model_id')
        if body_id != model_id:
            raise ModelCatalogError(
                f'model key/body identity mismatch: {model_id!r} != {body_id!r}')
        enabled = model.get('enabled')
        if not isinstance(enabled, bool):
            raise ModelCatalogError(f'models.{model_id}.enabled must be boolean')
        capabilities = _string_list(
            model.get('capabilities'),
            field=f'models.{model_id}.capabilities',
        )
        normalized_model = copy.deepcopy(model)
        normalized_model['model_id'] = model_id
        normalized_model['enabled'] = enabled
        normalized_model['capabilities'] = capabilities
        provenance = normalized_model.get('provenance')
        if provenance is not None and not isinstance(provenance, dict):
            raise ModelCatalogError(
                f'models.{model_id}.provenance must be an object')
        models[model_id] = normalized_model
        submitted_model_enabled[model_id] = enabled

    offerings: dict[str, JsonObject] = {}
    offerings_by_model: dict[str, list[str]] = {
        model_id: [] for model_id in models
    }
    for map_key, value in raw_offerings.items():
        oid = _identifier(map_key, field='offerings key')
        offering = _mapping(value, field=f'offerings.{oid}')
        body_id = _identifier(
            offering.get('offering_id'),
            field=f'offerings.{oid}.offering_id',
        )
        if body_id != oid:
            raise ModelCatalogError(
                f'offering key/body identity mismatch: {oid!r} != {body_id!r}')
        provider_id = _identifier(
            offering.get('provider_id'),
            field=f'offerings.{oid}.provider_id',
        )
        model_id = _identifier(
            offering.get('model_id'), field=f'offerings.{oid}.model_id')
        if known_providers is not None and provider_id not in known_providers:
            raise ModelCatalogError(f'unknown provider: {provider_id}')
        if model_id not in models:
            raise ModelCatalogError(
                f'offering {oid} references unknown model {model_id}')
        expected_oid = offering_id(provider_id, model_id)
        if oid != expected_oid:
            raise ModelCatalogError(
                f'offering_id must be deterministic {expected_oid!r}')
        enabled = offering.get('enabled')
        if not isinstance(enabled, bool):
            raise ModelCatalogError(f'offerings.{oid}.enabled must be boolean')
        configuration = _mapping(
            offering.get('configuration'),
            field=f'offerings.{oid}.configuration',
        )
        configuration = _normalized_configuration(
            {**configuration, 'model_id': model_id},
            field=f'offerings.{oid}.configuration',
        )
        normalized_offering = copy.deepcopy(offering)
        normalized_offering.update({
            'offering_id': oid,
            'provider_id': provider_id,
            'model_id': model_id,
            'enabled': enabled,
            'configuration': configuration,
        })
        provenance = normalized_offering.get('provenance')
        if provenance is not None and not isinstance(provenance, dict):
            raise ModelCatalogError(
                f'offerings.{oid}.provenance must be an object')
        offerings[oid] = normalized_offering
        offerings_by_model[model_id].append(oid)

    routes: dict[str, JsonObject] = {}
    referenced: set[str] = set()
    for map_key, value in raw_routes.items():
        model_id = _identifier(map_key, field='routes key')
        if model_id not in models:
            raise ModelCatalogError(f'route references unknown model {model_id}')
        route = _mapping(value, field=f'routes.{model_id}')
        body_id = _identifier(
            route.get('model_id'), field=f'routes.{model_id}.model_id')
        if body_id != model_id:
            raise ModelCatalogError(
                f'route key/body identity mismatch: {model_id!r} != {body_id!r}')
        if route.get('strategy') != 'score':
            raise ModelCatalogError(f'routes.{model_id}.strategy must be score')
        route_offerings = _string_list(
            route.get('offering_ids'),
            field=f'routes.{model_id}.offering_ids',
            reject_duplicates=True,
        )
        expected = set(offerings_by_model[model_id])
        if set(route_offerings) != expected:
            raise ModelCatalogError(
                f'route/offering mismatch for model {model_id}')
        if not route_offerings:
            raise ModelCatalogError(
                f'model {model_id} must have at least one offering')
        referenced.update(route_offerings)
        normalized_route = copy.deepcopy(route)
        normalized_route.update({
            'model_id': model_id,
            'offering_ids': sorted(route_offerings),
            'strategy': 'score',
        })
        routes[model_id] = normalized_route
    if referenced != set(offerings):
        raise ModelCatalogError('every offering must be referenced by one route')

    for model_id, model in models.items():
        model_offerings = [offerings[oid] for oid in offerings_by_model[model_id]]
        if not submitted_model_enabled[model_id]:
            for offering in model_offerings:
                offering['enabled'] = False
        model['enabled'] = any(row['enabled'] for row in model_offerings)
        capabilities = sorted({
            capability
            for row in model_offerings
            for capability in row['configuration'].get('capabilities', [])
        })
        model['capabilities'] = capabilities or model['capabilities']

    return {
        'contract_version': CONTRACT_VERSION,
        'revision': raw_revision,
        'models': dict(sorted(models.items())),
        'offerings': dict(sorted(offerings.items())),
        'routes': dict(sorted(routes.items())),
    }


def catalog_from_providers(
    providers: Sequence[Mapping[str, Any]], *,
    previous: Mapping[str, Any] | None = None,
    source: str = 'legacy',
) -> JsonObject:
    """Compile a provider snapshot into one deterministic catalog."""
    if not isinstance(providers, list):
        raise ModelCatalogError('providers must be an array')
    shells = strip_provider_models(providers)
    provider_ids = [_provider_id(shell) for shell in shells]
    previous_catalog = (
        normalize_catalog(previous) if isinstance(previous, Mapping) else None
    )
    previous_revision = (
        int(previous_catalog['revision']) if previous_catalog is not None else -1
    )

    offerings: dict[str, JsonObject] = {}
    raw_models: dict[str, list[JsonObject]] = {}
    for raw_provider in providers:
        provider = _mapping(raw_provider, field='provider')
        provider_id = _provider_id(provider)
        models = provider.get('models') or []
        if not isinstance(models, list):
            raise ModelCatalogError(f'provider {provider_id}.models must be an array')
        marker = provider.get('_catalog_revision')
        marker_current = (
            isinstance(marker, int) and not isinstance(marker, bool)
            and marker >= previous_revision
        )
        for index, raw_model in enumerate(models):
            row = _mapping(
                raw_model, field=f'provider {provider_id}.models[{index}]')
            model_id = _identifier(
                row.get('model_id') or row.get('id'),
                field=f'provider {provider_id}.models[{index}].model_id',
            )
            oid = offering_id(provider_id, model_id)
            if oid in offerings:
                raise ModelCatalogError(
                    f'duplicate provider/model offering: {provider_id}/{model_id}')
            prior = (
                previous_catalog['offerings'].get(oid)
                if previous_catalog is not None else None
            )
            if prior is not None and not marker_current:
                offering = copy.deepcopy(prior)
                logical_seed = {
                    **offering['configuration'],
                    'model_id': model_id,
                    'enabled': offering['enabled'],
                }
            else:
                configuration = _normalized_configuration(
                    {**row, 'model_id': model_id},
                    field=f'provider {provider_id}.models[{index}]',
                )
                offering = {
                    'offering_id': oid,
                    'provider_id': provider_id,
                    'model_id': model_id,
                    'enabled': bool(row.get('enabled', True)),
                    'configuration': configuration,
                    'provenance': {'source': str(source or 'legacy')},
                }
                logical_seed = row
            offerings[oid] = offering
            raw_models.setdefault(model_id, []).append(logical_seed)

    models: dict[str, JsonObject] = {}
    routes: dict[str, JsonObject] = {}
    for model_id, seeds in sorted(raw_models.items()):
        ids = sorted(
            oid for oid, row in offerings.items() if row['model_id'] == model_id)
        capabilities = sorted({
            capability
            for oid in ids
            for capability in offerings[oid]['configuration'].get(
                'capabilities', [])
        })
        model: dict[str, Any] = {
            'model_id': model_id,
            'enabled': any(offerings[oid]['enabled'] for oid in ids),
            'capabilities': capabilities or ['text'],
            'provenance': {'source': str(source or 'legacy')},
        }
        seed = seeds[0]
        for field in (
            'display_name', 'brand', 'thinking_default', 'capability_profile',
        ):
            if field in seed:
                model[field] = copy.deepcopy(seed[field])
        models[model_id] = model
        routes[model_id] = {
            'model_id': model_id,
            'offering_ids': ids,
            'strategy': 'score',
        }

    revision = previous_revision + 1 if previous_catalog is not None else 0
    return normalize_catalog({
        'contract_version': CONTRACT_VERSION,
        'revision': revision,
        'models': models,
        'offerings': offerings,
        'routes': routes,
    }, provider_ids=provider_ids)


def resolve_catalog(config: Mapping[str, Any]) -> JsonObject:
    """Resolve persisted authority or migrate legacy providers in memory."""
    shells = provider_shells(config)
    provider_ids = [_provider_id(shell) for shell in shells]
    persisted = config.get('model_catalog')
    if isinstance(persisted, Mapping):
        return normalize_catalog(persisted, provider_ids=provider_ids)
    providers = config.get('providers') or []
    if not isinstance(providers, list):
        raise ModelCatalogError('providers must be an array')
    return catalog_from_providers(providers, source='legacy')


def project_providers(
    shells: Iterable[Mapping[str, Any]], catalog: Mapping[str, Any],
) -> list[JsonObject]:
    """Project catalog offerings onto provider shells for legacy dispatch."""
    providers = strip_provider_models(shells)
    provider_map = {_provider_id(row): row for row in providers}
    normalized = normalize_catalog(catalog, provider_ids=provider_map)
    grouped: dict[str, list[JsonObject]] = {
        provider_id: [] for provider_id in provider_map
    }
    for offering in normalized['offerings'].values():
        configuration = copy.deepcopy(offering['configuration'])
        configuration['model_id'] = offering['model_id']
        configuration['enabled'] = offering['enabled']
        logical = normalized['models'][offering['model_id']]
        if logical.get('display_name') and not configuration.get('display_name'):
            configuration['display_name'] = logical['display_name']
        grouped[offering['provider_id']].append(configuration)
    for provider_id, provider in provider_map.items():
        provider['models'] = sorted(
            grouped[provider_id], key=lambda row: row['model_id'])
        provider['_catalog_revision'] = normalized['revision']
    return providers


__all__ = [
    'CONTRACT_VERSION',
    'MAX_MODELS',
    'MAX_OFFERINGS',
    'ModelCatalogError',
    'catalog_from_providers',
    'normalize_catalog',
    'offering_id',
    'project_providers',
    'provider_shells',
    'public_provider_metadata',
    'resolve_catalog',
    'strip_provider_models',
]
