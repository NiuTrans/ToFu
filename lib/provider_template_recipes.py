"""Provider-template recipes for catalog offerings.

Provider templates are first-run onboarding recipes, not routing authorities.
The authored v1 shape stores provider-scoped model registrations under
``offering_recipes``.  Each recipe names one exact logical ``model_id`` and
keeps provider wire identities in ``request_ids``. The stdlib bootstrap may
derive an in-memory ``models`` view while probing, then stages a secret-free
draft that the full application imports into model-routing v2.

Legacy templates with a top-level ``models`` array remain readable at this
boundary.  New authored templates must use ``offering_recipes`` so the source
shape cannot imply that each provider independently owns a logical model.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from lib.model_registration import normalize_model_entry


RECIPE_VERSION = 'tofu.provider-offering-recipe/v1'
MAX_TEMPLATE_OFFERINGS = 1024


class ProviderTemplateRecipeError(ValueError):
    """A provider template violates the offering-recipe contract."""


def _raw_recipes(template: dict, *, allow_legacy: bool) -> list:
    authored = template.get('offering_recipes')
    legacy = template.get('models')
    if authored is not None and legacy is not None and authored != legacy:
        raise ProviderTemplateRecipeError(
            'offering_recipes and legacy models disagree')
    if authored is not None:
        recipes = authored
    elif allow_legacy and legacy is not None:
        recipes = legacy
    else:
        recipes = []
    if not isinstance(recipes, list):
        raise ProviderTemplateRecipeError('offering_recipes must be an array')
    if len(recipes) > MAX_TEMPLATE_OFFERINGS:
        raise ProviderTemplateRecipeError(
            f'too many offering recipes ({len(recipes)} > '
            f'{MAX_TEMPLATE_OFFERINGS})')
    return recipes


def offering_recipes(template: Any, *, allow_legacy: bool = True) -> list[dict]:
    """Return canonical provider-scoped offering registrations.

    Identity is exact.  A template may expose one offering for a logical model;
    multiple provider wire spellings belong in that offering's ``request_ids``
    pool rather than in duplicate rows.
    """
    if not isinstance(template, dict):
        raise ProviderTemplateRecipeError('provider template must be an object')
    recipes = _raw_recipes(template, allow_legacy=allow_legacy)
    normalized: list[dict] = []
    seen: set[str] = set()
    for index, raw in enumerate(recipes):
        try:
            entry = normalize_model_entry(raw)
        except ValueError as exc:
            raise ProviderTemplateRecipeError(
                f'offering_recipes[{index}]: {exc}') from exc
        model_id = entry['model_id']
        if model_id in seen:
            raise ProviderTemplateRecipeError(
                f'duplicate offering recipe for logical model {model_id!r}')
        seen.add(model_id)
        normalized.append(entry)
    return normalized


def normalize_provider_template(
    raw: Any,
    *,
    allow_legacy: bool = True,
    include_legacy_models: bool = False,
) -> dict:
    """Return one canonical template without mutating its input.

    ``include_legacy_models`` is retained only for offline legacy-template
    tooling. Runtime onboarding never persists that projection.
    """
    if not isinstance(raw, dict):
        raise ProviderTemplateRecipeError('provider template must be an object')
    key = str(raw.get('key') or '').strip()
    if not key:
        raise ProviderTemplateRecipeError('provider template key is required')
    version = raw.get('recipe_version')
    if version not in (None, RECIPE_VERSION):
        raise ProviderTemplateRecipeError(
            f'recipe_version must be {RECIPE_VERSION!r}')

    recipes = offering_recipes(raw, allow_legacy=allow_legacy)
    out = copy.deepcopy(raw)
    out['key'] = key
    out['recipe_version'] = RECIPE_VERSION
    out['offering_recipes'] = recipes
    out.pop('models', None)
    if include_legacy_models:
        out['models'] = copy.deepcopy(recipes)
    return out


def provider_from_template(raw: Any, provider_id: Any) -> dict:
    """Derive a provider row from an authored template recipe.

    This compatibility helper is retained for offline protocol-face audits.
    Runtime onboarding compiles directly into model-routing v2 and never
    persists this legacy shape.
    """
    provider_key = str(provider_id or '').strip()
    if not provider_key:
        raise ProviderTemplateRecipeError('provider_id is required')
    template = normalize_provider_template(raw)
    provider = copy.deepcopy(template)
    recipes = provider.pop('offering_recipes')
    provider.pop('recipe_version', None)
    provider['id'] = provider_key
    provider['models'] = recipes
    return provider


def load_provider_templates() -> list[dict]:
    """Load normalized onboarding recipes from their package-owned sources."""
    from lib.model_info._openai_gpt56 import OPENAI_TEMPLATE

    sources: list[dict] = [copy.deepcopy(OPENAI_TEMPLATE)]
    template_directory = Path(__file__).parents[1] / 'static' / 'provider_templates'
    if template_directory.is_dir():
        for path in sorted(template_directory.glob('*.json')):
            try:
                sources.append(json.loads(path.read_text(encoding='utf-8')))
            except (OSError, json.JSONDecodeError) as exc:
                raise ProviderTemplateRecipeError(
                    f'cannot load provider template {path.name}: {exc}') from exc

    # Deployment-local files intentionally override a package recipe by key.
    by_key: dict[str, dict] = {}
    for raw in sources:
        normalized = normalize_provider_template(raw)
        if (normalized['offering_recipes']
                or normalized.get('category') == 'local'):
            by_key[normalized['key']] = normalized
    return sorted(
        by_key.values(),
        key=lambda row: (
            str(row.get('category') or ''),
            str(row.get('name') or row['key']).casefold(),
        ),
    )


def compile_provider_template_bundle(
    template_key: Any,
    *,
    selected_model_ids: list[str] | None = None,
) -> dict:
    """Compile one onboarding recipe into a secret-free v2 access bundle."""
    key = str(template_key or '').strip()
    template = next(
        (row for row in load_provider_templates() if row['key'] == key), None)
    if template is None:
        raise ProviderTemplateRecipeError(
            f'unknown provider template {key!r}')

    recipes = copy.deepcopy(template['offering_recipes'])
    if selected_model_ids is not None:
        selected = {
            str(model_id or '').strip() for model_id in selected_model_ids
            if str(model_id or '').strip()
        }
        available = {row['model_id'] for row in recipes}
        unknown = sorted(selected - available)
        if unknown:
            raise ProviderTemplateRecipeError(
                'unknown template model IDs: ' + ', '.join(unknown))
        recipes = [row for row in recipes if row['model_id'] in selected]
    if not recipes and template.get('category') != 'local':
        raise ProviderTemplateRecipeError(
            'at least one template model must be selected')

    legacy = copy.deepcopy(template)
    legacy['id'] = template['key']
    legacy['models'] = recipes
    legacy.pop('offering_recipes', None)
    legacy.pop('recipe_version', None)
    # A placeholder selects the API-key credential shape.  It is held only in
    # the private migration plan and is never returned or persisted.
    if template.get('category') != 'local':
        legacy['api_keys'] = ['template-secret-placeholder']

    from lib.model_routing.migration import plan_legacy_migration

    plan = plan_legacy_migration({'providers': [legacy]})
    if plan.blocking_issues:
        raise ProviderTemplateRecipeError('; '.join(
            issue.message for issue in plan.blocking_issues))
    document = plan.document
    credentials = copy.deepcopy(document['credentials'])
    for credential in credentials:
        credential['secret_reference'] = ''
        credential['key_hint'] = ''
    return {
        'provider': copy.deepcopy(document['providers'][0]),
        'provider_access': copy.deepcopy(document['provider_accesses'][0]),
        'connections': copy.deepcopy(document['connections']),
        'credentials': credentials,
        'offerings': copy.deepcopy(document['offerings']),
        'deployments': copy.deepcopy(document['deployments']),
        'creators': copy.deepcopy(document['creators']),
        'models': copy.deepcopy(document['models']),
        # Static recipe headers are folded into the encrypted credential
        # envelope by the browser together with the user-entered API key.
        'credential_extra_headers': copy.deepcopy(
            template.get('extra_headers') or {}),
    }


__all__ = [
    'MAX_TEMPLATE_OFFERINGS',
    'RECIPE_VERSION',
    'ProviderTemplateRecipeError',
    'compile_provider_template_bundle',
    'load_provider_templates',
    'normalize_provider_template',
    'offering_recipes',
    'provider_from_template',
]
