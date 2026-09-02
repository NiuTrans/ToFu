"""Provider-template recipes for catalog offerings.

Provider templates are onboarding recipes, not model-definition authorities.
The authored v1 shape stores provider-scoped model registrations under
``offering_recipes``.  Each recipe names one exact logical ``model_id`` and
keeps provider wire identities in ``request_ids``.  Applying a recipe derives
the legacy provider ``models`` projection consumed by existing Settings and
dispatch paths.

Legacy templates with a top-level ``models`` array remain readable at this
boundary.  New authored templates must use ``offering_recipes`` so the source
shape cannot imply that each provider independently owns a logical model.
"""

from __future__ import annotations

import copy
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

    ``include_legacy_models`` derives a compatibility alias for older browser
    bundles.  It never changes the authored authority: ``offering_recipes`` is
    always present and is the only field consumers should edit.
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

    Template-only descriptive fields are retained because Settings uses them
    for labels and setup hints; only the recipe vocabulary is projected into
    the legacy ``models`` transport field.  The caller supplies the concrete
    provider identity created for this installation.
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


__all__ = [
    'MAX_TEMPLATE_OFFERINGS',
    'RECIPE_VERSION',
    'ProviderTemplateRecipeError',
    'normalize_provider_template',
    'offering_recipes',
    'provider_from_template',
]
