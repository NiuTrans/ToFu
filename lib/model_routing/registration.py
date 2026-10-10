"""Model registration admission shared by Settings and provider bundle writes.

Entry point: prepare_registration_document. Exact catalog rules fill known
facts; unknown facts return actionable errors for the calling research agent.
No network, model invocation, credential access, or persistence occurs here.
Legacy reads and unchanged Models remain readable while being repaired.
"""
from __future__ import annotations

from datetime import date
import math
import json
from urllib.parse import urlsplit

from .domain import ModelRef, ModelRoutingError, normalize_document, public_projection


def validate_registration_metadata(model: dict) -> None:
    if 'registration_metadata' not in model:
        return
    metadata = model['registration_metadata']
    field = 'registration_metadata'
    try:
        if not isinstance(metadata, dict) or not {'release_date', 'checked_at', 'source_urls'} <= set(metadata) or set(metadata) - {'release_date', 'checked_at', 'source_urls', 'field_sources'}:
            raise ValueError()
        released = metadata['release_date']
        if not isinstance(released, str) or len(released) not in (7, 10):
            raise ValueError()
        date.fromisoformat(released + '-01' if len(released) == 7 else released)
        if len(metadata['checked_at']) != 10:
            raise ValueError()
        date.fromisoformat(metadata['checked_at'])
        urls = metadata['source_urls']
        if not isinstance(urls, list) or not 1 <= len(urls) <= 8:
            raise ValueError()
        for url in urls:
            if not isinstance(url, str) or len(url) > 2048:
                raise ValueError()
            parsed = urlsplit(url)
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError()
        if len(set(urls)) != len(urls):
            raise ValueError()
        sources = metadata.get('field_sources', {})
        if not isinstance(sources, dict) or set(sources) - {'release_date', 'list_pricing', 'inference_profile', 'capabilities', 'context_window'}:
            raise ValueError()
        for values in sources.values():
            if not isinstance(values, list) or not 1 <= len(values) <= 8 or any(value not in urls for value in values):
                raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise ModelRoutingError('registration_metadata requires a valid release date, checked date, and 1–8 HTTPS evidence URLs', field=field) from None


def _valid_price(price) -> bool:
    if not isinstance(price, dict):
        return False
    fields = ('input', 'output') + tuple(
        key for key in ('cache_read', 'cache_write') if key in price)
    return (price.get('currency') in {'USD', 'CNY'}
            and price.get('unit') == 'per_million_tokens'
            and all(isinstance(price.get(key), (int, float))
                    and not isinstance(price[key], bool)
                    and math.isfinite(price[key]) and price[key] >= 0
                    for key in fields))


def registration_missing_fields(model: dict) -> list[str]:
    """Assess a public Model, including rule-derived facts, without guessing."""
    missing = []
    from .model_defaults import has_exact_model_defaults
    from lib.pricing import MODEL_PRICING
    exact_knowledge = has_exact_model_defaults(model) or model['model_id'] in MODEL_PRICING
    if not model.get('release_date') or (not model.get('registration_metadata') and not exact_knowledge):
        missing.append('registration_metadata.release_date + checked_at + source_urls')
    # An explicitly configured zero price is valid; absence is not free. An
    # exact maintained rule in the pricing SSOT is evidence-grade pricing for
    # this identity, the same standing as release-date exact knowledge above.
    price = model.get('list_pricing')
    if price is None:
        price = model.get('official_pricing')
    if not _valid_price(price) and model['model_id'] not in MODEL_PRICING:
        missing.append('list_pricing.input/output/currency/unit')
    if not model.get('capabilities'):
        missing.append('capabilities')
    reasoning = model.get('inference_profile', {}).get('reasoning', {})
    support = reasoning.get('support')
    if support not in {'supported', 'unsupported'}:
        missing.append('inference_profile.reasoning.support')
    elif support == 'supported':
        if not reasoning.get('wire_format') or reasoning.get('wire_format') == 'none':
            missing.append('inference_profile.reasoning.wire_format')
        if not isinstance(reasoning.get('can_disable'), bool):
            missing.append('inference_profile.reasoning.can_disable')
        # Empty ladder explicitly means a toggle/budget-only model.
        if 'supported_efforts' not in reasoning:
            missing.append('inference_profile.reasoning.supported_efforts (use [] for no effort control)')
        elif reasoning['supported_efforts'] and not reasoning.get('default_effort'):
            missing.append('inference_profile.reasoning.default_effort')
    if support == 'unsupported' and 'thinking' in model.get('capabilities', []):
        missing.append('capabilities.thinking contradicts unsupported reasoning')
    if support == 'supported' and not missing:
        from lib.llm.reasoning import ReasoningContractError, compile_reasoning
        intents = [{'version': 1, 'mode': 'enabled', 'strict': True}]
        if reasoning.get('can_disable'):
            intents.append({'version': 1, 'mode': 'off', 'strict': True})
        intents.extend({'version': 1, 'mode': 'exact', 'value': effort, 'strict': True}
                       for effort in reasoning.get('supported_efforts', [])
                       if effort not in {'none', reasoning.get('disabled_effort')})
        try:
            for intent in intents:
                compile_reasoning({'_reasoning_intent': intent}, model['model_id'],
                                  inference_profile=model['inference_profile'])
        except ReasoningContractError:
            missing.append('inference_profile.reasoning controls cannot compile to the declared wire format')
    return missing


def prepare_registration_document(document: dict, *, previous: dict) -> dict:
    """Single admission path used by both repositories and every caller.

    Incomplete new Models are durable drafts, never eligible routes. An invalid
    update cannot replace a ready model unless explicitly staged as a draft.
    Unchanged unmarked legacy rows survive until the bounded catalog migration.
    """
    candidate = normalize_document(document)
    previous_normalized = normalize_document(previous)
    prior = {ModelRef.from_value(m): m for m in previous_normalized['models']}
    public = public_projection(candidate)
    for index, (model, projected) in enumerate(zip(candidate['models'], public['models'])):
        if model.get('list_pricing') is not None and not _valid_price(model['list_pricing']):
            raise ModelRoutingError('list_pricing must contain finite nonnegative input/output prices with currency and unit', field=f'models[{index}].list_pricing')
        old = prior.get(ModelRef.from_value(model))
        if old == model and 'registration_state' not in model:
            continue
        missing = registration_missing_fields(projected)
        requested = model.get('registration_state')
        if requested == 'disabled':
            model['registration_issues'] = missing
            continue
        if missing and (requested == 'ready' or (old and old.get('registration_state') == 'ready'
                and requested not in {'pending_metadata', 'pending_validation'})):
            raise ModelRoutingError(
                f"Cannot activate incomplete model {model['creator_id']}/{model['model_id']}: "
                + '; '.join(missing) + '. Search official documentation and resubmit verified metadata.',
                kind='model_registration_incomplete', field=f'models[{index}]')
        invalid_wire = any('cannot compile' in field for field in missing)
        model['registration_state'] = ('pending_validation' if invalid_wire else 'pending_metadata') if missing else 'ready'
        model['registration_issues'] = missing
    # Validate the actual provider/protocol override, not only the Model baseline.
    # These bounded diagnostics are recomputed server-side at every write.
    from .inference import resolve_inference_profile
    models = {ModelRef.from_value(row): row for row in public_projection(candidate)['models']}
    offerings = {row['offering_id']: row for row in candidate['offerings']}
    connections = {row['connection_id']: row for row in candidate['connections']}
    prior_deployments = {row['deployment_id']: row for row in previous_normalized['deployments']}
    profile_checks = {}  # request-local; at most 128 bounded profile keys
    for deployment in candidate['deployments']:
        offering = offerings[deployment['offering_id']]
        model = models.get(ModelRef.from_value(offering['model'])) if offering.get('model') else None
        deployment.pop('registration_issues', None)
        if not model or model.get('registration_state') != 'ready':
            continue
        profile = resolve_inference_profile(model=model, offering=offering,
            connection=connections[deployment['connection_id']], deployment=deployment)
        scoped = dict(model, inference_profile=profile)
        if profile.get('reasoning', {}).get('support') == 'unsupported':
            scoped['capabilities'] = [cap for cap in scoped['capabilities'] if cap != 'thinking']
        cache_key = (ModelRef.from_value(model), json.dumps(profile, sort_keys=True, separators=(',', ':')))
        issues = profile_checks.get(cache_key)
        if issues is None:
            issues = registration_missing_fields(scoped)
            if len(profile_checks) < 128 and len(cache_key[1]) <= 16384:
                profile_checks[cache_key] = issues
        if issues:
            old_deployment = prior_deployments.get(deployment['deployment_id'])
            old_model = prior.get(ModelRef.from_value(model), {})
            if (old_deployment and not old_deployment.get('registration_issues')
                    and old_model.get('registration_state') == 'ready' and offering.get('enabled')):
                raise ModelRoutingError('Provider inference metadata is incomplete; disable the Offering to stage this change: '
                    + '; '.join(issues), kind='model_registration_incomplete', field='deployments')
            deployment['registration_issues'] = issues
    return candidate


def model_registration_ready(model: dict) -> bool:
    """Legacy unmarked rows remain a migration seam; marked rows fail closed."""
    return model.get('registration_state', 'ready') == 'ready'
