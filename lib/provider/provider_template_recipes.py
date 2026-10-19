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
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from lib.log import get_logger
from lib.model_registration import normalize_model_entry

logger = get_logger(__name__)


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
            if 'capabilities' not in raw and not raw.get('capabilities_auto'):
                entry.pop('capabilities', None)  # Absence inherits the Model.
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


def _apply_contract_inference(recipes: list[dict], contract: Any) -> None:
    """Import an explicit effort contract only for its exact model identities."""
    if not isinstance(contract, dict) or not isinstance(contract.get('models'), dict):
        return
    from lib.model_routing.inference import (
        profile_from_metadata, resolve_inference_profile, validate_inference_profile,
    )
    for recipe in recipes:
        if recipe['model_id'] in contract['models']:
            model_contract = contract['models'][recipe['model_id']]
            efforts = model_contract.get('reasoning_efforts', contract.get('reasoning_efforts'))
            if efforts is None:
                continue
            reasoning = {
                'support': 'supported', 'wire_format': 'reasoning_effort',
                'supported_efforts': efforts, 'can_disable': 'none' in efforts,
            }
            default = model_contract.get('default_reasoning_effort', contract.get('default_reasoning_effort'))
            if default is not None:
                reasoning['default_effort'] = default
            declared = validate_inference_profile({'reasoning': reasoning, 'source': 'configured'})
            recipe['inference_profile'] = resolve_inference_profile(
                model={'inference_profile': declared},
                offering={'inference_profile': profile_from_metadata(recipe)},
            )


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

    category = raw.get('category', 'relay')
    if category not in ('official', 'relay', 'local'):
        raise ProviderTemplateRecipeError('category must be official, relay, or local')
    catalog_mode = raw.get('catalog_mode', 'curated')
    if catalog_mode not in ('curated', 'discovery'):
        raise ProviderTemplateRecipeError('catalog_mode must be curated or discovery')
    source = raw.get('catalog_source')
    if source is not None:
        if not isinstance(source, dict) or set(source) != {'url', 'checked_at'}:
            raise ProviderTemplateRecipeError('catalog_source requires url and checked_at')
        try:
            url = source['url']
            parsed = urlparse(url) if isinstance(url, str) else None
            if (parsed is None or parsed.scheme != 'https' or not parsed.hostname
                    or parsed.username or parsed.password or len(url) > 2048):
                raise ValueError('invalid source URL')
            checked = source['checked_at']
            if not isinstance(checked, str) or date.fromisoformat(checked).isoformat() != checked:
                raise ValueError('invalid source date')
        except (ValueError, TypeError) as exc:
            raise ProviderTemplateRecipeError('catalog_source requires an HTTPS URL and ISO date') from exc
    if catalog_mode == 'discovery':
        endpoint = urlparse(str(raw.get('base_url') or ''))
        if endpoint.scheme != 'https' or not endpoint.hostname or endpoint.username or endpoint.password:
            raise ProviderTemplateRecipeError('discovery template requires a public HTTPS endpoint')
    recipes = offering_recipes(raw, allow_legacy=allow_legacy)
    out = copy.deepcopy(raw)

    from lib.model_routing.inference import profile_from_metadata
    try:
        _apply_contract_inference(recipes, out.get('contract'))
        for metadata in [out, *(out.get('faces') or {}).values(),
                         *(out.get('endpoints') or [])]:
            if not isinstance(metadata, dict):
                continue
            profile = profile_from_metadata(metadata)
            if profile:
                metadata['inference_profile'] = profile
    except ValueError as exc:
        raise ProviderTemplateRecipeError(str(exc)) from exc
    out['key'] = key
    out['recipe_version'] = RECIPE_VERSION
    out['category'] = category
    out['catalog_mode'] = catalog_mode
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
    """Load normalized onboarding recipes from their file-backed sources.

    The catalog authority is hot-reloaded on every call, merging four layers
    by key, in order (docs/SITE_PACKS.md §4): ``lib/model_info/data/*.json``
    (package catalog), ``static/provider_templates/*.json`` (deployment
    overrides), ``bundles/*/provider_templates/*.json`` (release-tiered
    bundles), then ``<config_path>/provider_templates/*.json`` (user-tier
    drop-ins) — template edits never require a restart.  The bootstrap
    picker merges the same directories in the same order, so both pickers
    offer identical providers.  The Python-side
    ``bootstrap_pkg.providers._BUILTIN_PROVIDER_TEMPLATES`` list is not a
    source here: it only retains bootstrap scaffolding (the protocol-less
    ``anthropic`` stub, which would compile an OpenAI-shaped connection
    against the native Messages API, and the ``custom`` form placeholder
    with an empty model id).
    """
    sources: list[dict] = []
    override_keys: set[str] = set()
    # Layered catalog (docs/SITE_PACKS.md §4): package defaults, deployment
    # overrides, release-tiered bundles, then user-tier drop-ins — later
    # layers win by key.
    template_directories = [
        Path(__file__).parents[1] / 'model_info' / 'data',
        Path(__file__).parents[2] / 'static' / 'provider_templates',
    ]
    bundles_root = Path(__file__).parents[2] / 'bundles'
    if bundles_root.is_dir():
        template_directories.extend(
            bundle / 'provider_templates'
            for bundle in sorted(bundles_root.iterdir())
            if bundle.is_dir() and (bundle / 'provider_templates').is_dir()
        )
    from lib.config_dir import config_path
    template_directories.append(Path(config_path('provider_templates')))
    for layer_index, template_directory in enumerate(template_directories):
        if not template_directory.is_dir():
            continue
        for path in sorted(template_directory.glob('*.json')):
            # A template file being hot-edited (transiently invalid JSON) or
            # a malformed deployment drop-in must not take down the whole
            # picker — skip the offending file and keep serving the rest.
            try:
                raw = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning(
                    'skipping unreadable provider template %s: %s',
                    path.name, exc)
                continue
            if not isinstance(raw, dict):
                logger.warning(
                    'skipping provider template %s: not a JSON object',
                    path.name)
                continue
            try:
                normalized = normalize_provider_template(raw)
                sources.append(normalized)
                if layer_index > 0:
                    override_keys.add(normalized['key'])
            except ProviderTemplateRecipeError as exc:
                logger.warning(
                    'skipping invalid provider template %s: %s',
                    path.name, exc)

    # Deployment-local files intentionally override a package recipe by key.
    by_key: dict[str, dict] = {}
    for normalized in sources:
        if (normalized['offering_recipes']
                or normalized.get('category') == 'local'
                or normalized.get('catalog_mode') == 'discovery'):
            by_key[normalized['key']] = normalized
    templates = sorted(
        by_key.values(),
        key=lambda row: (
            str(row.get('category') or ''),
            str(row.get('name') or row['key']).casefold(),
        ),
    )
    # Dynamic feed overlay (docs/PROVIDER_TEMPLATE_RECIPES.md — Dynamic
    # feed): a provider-owned cache replaces only that provider's seed list
    # at the matching endpoint. Missing/expired cache never blocks the picker
    # and only kicks a one-flight refresh; other providers stay authored-only.
    # The stdlib bootstrap picker never fetches and keeps the authored catalog.
    try:
        from lib.provider.provider_template_feed import (
            load_cached_feed,
            maybe_refresh_async,
            merge_feed_recipes,
        )
        feed = load_cached_feed()
        if feed is not None:
            templates = merge_feed_recipes(templates, feed, excluded_keys=override_keys)
        maybe_refresh_async(feed)
    except Exception as exc:
        logger.warning('provider template feed unavailable: %s', exc)
    return templates


def compile_provider_template_bundle(
    template_key: Any,
    *,
    selected_model_ids: list[str] | None = None,
    official_directory: list[dict] | None = None,
    _templates: list[dict] | None = None,
) -> dict:
    """Compile one onboarding recipe into a secret-free v2 access bundle."""
    key = str(template_key or '').strip()
    templates = _templates if _templates is not None else load_provider_templates()
    from lib.model_routing.model_defaults import official_model_directory
    directory = (official_directory if _templates is not None and official_directory is not None
                 else official_model_directory(templates))
    # Owner registry edits take precedence over packaged defaults.
    directory_by_ref = {(r['creator_id'], r['model_id']): r for r in directory}
    directory_by_ref.update({(r['creator_id'], r['model_id']): r
                             for r in official_directory or []})
    template = next((row for row in templates if row['key'] == key), None)
    if template is None:
        raise ProviderTemplateRecipeError(
            f'unknown provider template {key!r}')

    recipes = copy.deepcopy(template['offering_recipes'])
    from lib.model_catalog._creator_identity import strip_routing_decoration
    for recipe in recipes:
        # Public catalog estimates are Model metadata, not provider limits.
        if (template.get('category') == 'relay' and
                recipe.get('context_window_source') in {
                    'family_estimate', 'vendor_official', 'repository_verified'}):
            recipe.pop('context_window', None)
        recipe['inherited_model_fields'] = [key for key in ('capabilities', 'context_window')
                                             if recipe.get(key) is None]
        logical_id = strip_routing_decoration(recipe['model_id'])
        matches = [row for (creator, mid), row in directory_by_ref.items()
                   if mid == logical_id]
        if len(matches) == 1:
            recipe.setdefault('capabilities', copy.deepcopy(matches[0]['capabilities']))
            if not recipe.get('context_window'):
                recipe['context_window'] = matches[0]['context_window']
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
    if (not recipes and template.get('category') != 'local'
            and template.get('catalog_mode') != 'discovery'):
        raise ProviderTemplateRecipeError(
            'at least one template model must be selected')
    # Managed pricing-tier tags (cheap) are owned by the pricing tables, not
    # hand-authored recipe rows: correct the projection from live pricing,
    # but keep authored tags on models the tables do not price yet.
    from lib.llm_dispatch.config._pricing import reevaluate_pricing_tags
    reevaluate_pricing_tags(
        recipes, log_prefix='template:%s' % key, strip_unpriced=False)


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

    # Provider prices belong to Offerings, not official Model list prices.
    # A new creator-recognizable feed ID must not promote a relay quote into
    # developer pricing through the generic legacy migration fallback.
    from lib.model_catalog._creator_identity import brand_family, creator_family
    operator = brand_family(template.get('brand') or template['key'])
    provider_prices = {}
    for recipe in legacy['models']:
        own_model = operator and creator_family(recipe['model_id']) == operator
        if template.get('category') != 'official' or template.get('catalog_feed') or not own_model:
            price = recipe.pop('pricing', None)
            if price is not None:
                provider_prices[recipe['model_id']] = price

    plan = plan_legacy_migration({'providers': [legacy]},
                                 official_directory=list(directory_by_ref.values()))
    if plan.blocking_issues:
        raise ProviderTemplateRecipeError('; '.join(
            issue.message for issue in plan.blocking_issues))
    document = plan.document

    # Legacy imports can preserve a previously used route; a fresh template
    # cannot. Identity recognition is not evidence of account entitlement or
    # successful inference. The access matrix owns the first live verdict.
    for deployment in document['deployments']:
        deployment.pop('enabled', None)
        deployment['probe_status'] = 'unprobed'
    if provider_prices:
        # Migration emits one offering per recipe in order; collisions are
        # blocking issues above. Never fuzzy-match a price to a similar ID.
        for recipe, offering in zip(legacy['models'], document['offerings'], strict=True):
            price = provider_prices.get(recipe['model_id'])
            if price is not None:
                offering['actual_pricing'] = copy.deepcopy(price)

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
        # Static recipe headers are surfaced on each Connection's
        # extra_headers by the browser; only the user-entered API key
        # enters the encrypted credential envelope.
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
