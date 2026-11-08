"""World model catalogue sync into the owner model-routing authority.

The Model catalog tab (模型配置) is the registry of models that EXIST, not
models some configured provider currently supplies: a newly shipped model
must appear there even before any provider bundle carries it. This sync
compiles every package provider template (the same recipes the onboarding
picker serves) and inserts the missing Creator/Model rows — idempotent:
existing registry fields (including owner edits) are never overwritten.
Missing declared inference defaults are filled and exact pending catalog
identities are bound without changing probe verdicts or credential exclusions.

One authored exception to insert-only: a template's ``retired_model_ids``
is a kill-switch (the dynamic template feed already never re-adds them),
and rows still carrying a retired identity are pruned here — but only when
the freshly compiled catalogue no longer supplies that identity.

One provenance exception to never-overwrite: a Model context the first
legacy cutover generated (proven by replaying its redacted receipt, see
``metadata_migration.cutover_generated_model_contexts``) and that still holds
that default takes the official directory fact.

Retirement is global: ``tests/model_routing/test_world_catalog_sync.py`` pins that no
shipped template authors an offering whose identity any template retires
(deepseek-chat flipped in and out of the registry while deepseek.json
retired it and meituan.json still authored it — the UI's retired chip
advised removal, a full-document PUT wiped the row, and the next boot sync
re-inserted it).  The catalogue guard below remains as defence for
deployment-local override templates the shipped-template invariant cannot
see.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


def _compile_world_catalog(templates: list[dict]) -> tuple[list[dict], list[dict], dict]:
    """Compile every non-local template once: creators, Models and supply.

    ``supply`` maps a Model identity to each template offering naming it:
    template key, wire ids and that template's context/capabilities/price.
    """
    from lib.provider.provider_template_recipes import (
        ProviderTemplateRecipeError, compile_provider_template_bundle)

    from .model_defaults import official_model_directory
    directory = official_model_directory(templates)
    creators: dict[str, dict] = {}
    models: dict[tuple, dict] = {(r['creator_id'], r['model_id']): r for r in directory}
    supply: dict[tuple, list[dict]] = {}
    for template in templates:
        if template.get('category') == 'local':
            continue
        try:
            bundle = compile_provider_template_bundle(template['key'],
                official_directory=directory, _templates=templates)
        except ProviderTemplateRecipeError as exc:
            logger.warning('world catalogue: skipping template %s: %s',
                           template['key'], exc)
            continue
        for row in bundle['creators']:
            creators.setdefault(row['creator_id'], row)
        for row in bundle['models']:
            models.setdefault((row['creator_id'], row['model_id']), row)
        wires: dict[str, list[str]] = {}
        for deployment in bundle['deployments']:
            wires.setdefault(deployment['offering_id'], []).append(deployment['wire_model_id'])
        for offering in bundle['offerings']:
            ref = offering.get('model')
            if not ref:
                continue
            supply.setdefault((ref['creator_id'], ref['model_id']), []).append({
                'template': template['key'],
                'wire_model_ids': wires.get(offering['offering_id'], []),
                'context_window': offering.get('context_window'),
                'capabilities': offering.get('capabilities', []),
                'actual_pricing': offering.get('actual_pricing'),
            })
    return list(creators.values()), list(models.values()), supply


def _world_catalog_rows(templates: list[dict]) -> tuple[list[dict], list[dict]]:
    """Union of (creators, models) rows compiled from all known templates."""
    creators, models, _supply = _compile_world_catalog(templates)
    return creators, models


def _registration_gaps(creators: list[dict], models: list[dict]) -> dict[tuple, list[str]]:
    """Missing admission facts per compiled Model identity ([] = publishable).

    Templates and provider feeds also contain unverified supply estimates.
    A world registry is not a queue of silently admitted Model drafts: this
    projects the same rule-derived fields the shared admission checks.
    """
    from .domain import empty_document, public_projection
    from .registration import registration_missing_fields
    catalog = empty_document()
    catalog['creators'], catalog['models'] = creators, models
    return {(row['creator_id'], row['model_id']): registration_missing_fields(row)
            for row in public_projection(catalog)['models']}


def withheld_catalog_models(templates: list[dict]) -> list[dict]:
    """Compiled catalogue Models the sync withholds, with missing facts and supply.

    Exactly the identities ``sync_world_model_catalog`` refuses to publish for
    the same templates; the model release watch reports from this list.
    """
    creators, models, supply = _compile_world_catalog(templates)
    return [
        {'creator_id': creator_id, 'model_id': model_id, 'missing': missing,
         'supply': supply.get((creator_id, model_id), [])}
        for (creator_id, model_id), missing in _registration_gaps(creators, models).items()
        if missing
    ]


def _official_directory(templates: list[dict]) -> list[dict]:
    """Developer Model facts only; relay supply never repairs a Model fact."""
    from .model_defaults import official_model_directory
    return official_model_directory(templates)


def _adopt_declared_name(model: dict, template_row: dict | None) -> bool:
    """Give an id-echoing registry row the Model's authored display name.

    A row whose ``display_name`` merely repeats its ``model_id`` carries no
    naming decision — it is what migration wrote when the template declared
    none. Adopting the template's name is therefore not an overwrite of an
    owner edit (unlike ``apply_model_defaults``, any other value wins), and it
    is what lets a gateway spelling such as ``deepseek-v4.1-flash`` stop
    surfacing as a raw id.
    """
    declared = (template_row or {}).get('display_name')
    if not isinstance(declared, str) or not declared.strip():
        return False
    if declared == model.get('model_id'):
        return False
    if model.get('display_name') not in (None, '', model.get('model_id')):
        return False
    model['display_name'] = declared
    return True


def _retired_model_keys(templates: list[dict]) -> set:
    """Canonical keys of every template-authored ``retired_model_ids`` row."""
    from lib.model_catalog._creator_identity import canonical_key

    retired = set()
    for template in templates:
        for model_id in (template.get('retired_model_ids') or []):
            key = canonical_key(model_id)
            if key:
                retired.add(key)
    return retired


def sync_world_model_catalog(*, boundary=None, repository=None) -> dict:
    """Insert missing Models and repair absent metadata/exact pending identities.

    Also prunes registry rows whose identity is retired by any template —
    the feed never re-adds them, so they would otherwise linger forever.
    The hot-reloaded template catalogue is read once: every step of one sync
    sees the same template revision.
    """
    if boundary is None or repository is None:
        from lib.llm_dispatch.autodiscover_local import _runtime_context
        context = _runtime_context()
        if context is None:
            return {'added': 0, 'creators_added': 0, 'skipped': 'no_owner'}
        boundary, repository = context

    from lib.model_catalog._creator_identity import merge_keys
    from lib.provider.provider_template_recipes import load_provider_templates

    templates = load_provider_templates()
    creators, models = _world_catalog_rows(templates)
    # Publish only identities whose full Model contract is satisfied.
    gaps = _registration_gaps(creators, models)
    models = [row for row in models
              if gaps.get((row['creator_id'], row['model_id'])) == []]
    retired_keys = _retired_model_keys(templates)
    catalog_keys = set()
    for row in models:
        catalog_keys.update(merge_keys(row.get('model_id'),
                                       row.get('display_name')))

    def _prunable(row: dict) -> bool:
        keys = merge_keys(row.get('model_id'), row.get('display_name'))
        return bool(retired_keys & keys) and not (catalog_keys & keys)

    current = repository.get(boundary)
    from copy import deepcopy
    from .model_defaults import apply_model_defaults
    from .catalog_identity import reconcile_catalog_identities
    from .metadata_migration import (
        cutover_generated_model_contexts, reconcile_generated_model_context,
        reconcile_offering_metadata)
    from .migration import _DEFAULT_CONTEXT_WINDOW
    unmarked_offerings = any(
        'inherited_model_fields' not in row for row in current.document['offerings'])
    # The cutover-context proof is evaluated once per cutover receipt; the
    # marker makes every later 128,000 value an owner value, never re-derived
    # from the immutable receipt (which also stops replaying the legacy plan).
    cutover_receipt_id = (current.document.get('migration') or {}).get('receipt_id') or ''
    context_repair_pending = bool(cutover_receipt_id) and (
        current.document['migration'].get('context_repair_receipt_id') != cutover_receipt_id)
    defaulted_models = context_repair_pending and any(
        row['context_window'] == _DEFAULT_CONTEXT_WINDOW and not row.get('registration_metadata')
        for row in current.document['models'])
    receipt = (repository.migration_receipt(boundary)
               if unmarked_offerings or defaulted_models else None)
    legacy_receipt = receipt if unmarked_offerings else None
    generated_contexts = (cutover_generated_model_contexts(current.document, receipt)
                          if defaulted_models else set())
    official_directory = _official_directory(templates) if generated_contexts else []
    baseline = {(r['creator_id'], r['model_id']): r for r in models}

    def repair_metadata(document):
        filled = sum(apply_model_defaults(row, baseline=baseline.get(
            (row['creator_id'], row['model_id']))) for row in document['models'])
        named = sum(_adopt_declared_name(row, baseline.get(
            (row['creator_id'], row['model_id']))) for row in document['models'])
        bound = reconcile_catalog_identities(document)
        migrated = len(reconcile_offering_metadata(document,
            provider_templates=templates, legacy_receipt=legacy_receipt))
        contexts = reconcile_generated_model_context(document,
            generated=generated_contexts, official_directory=official_directory)
        return filled, named, bound, migrated, contexts

    repairs = repair_metadata(deepcopy(current.document))
    known_creators = {row['creator_id'] for row in current.document['creators']}
    known_keys = set()
    for row in current.document['models']:
        known_keys.update(merge_keys(row.get('model_id'),
                                     row.get('display_name')))
    new_creators = [row for row in creators
                    if row['creator_id'] not in known_creators]
    new_models = [row for row in models
                  if not (merge_keys(row.get('model_id'),
                                     row.get('display_name')) & known_keys)]
    prunable = [row for row in current.document['models'] if _prunable(row)]
    if (not new_creators and not new_models and not prunable and not any(repairs)
            and not context_repair_pending):
        return {'added': 0, 'creators_added': 0, 'pruned': 0,
                'revision': current.revision}

    pruned_count = 0
    repaired_counts = (0, 0, 0, 0, [])

    def _mutate(working: dict):
        nonlocal pruned_count, repaired_counts
        pruned_count = 0  # reset per attempt: CAS retries re-run this closure
        changed = False
        if retired_keys:
            kept = []
            for row in working['models']:
                if _prunable(row):
                    pruned_count += 1
                    changed = True
                    continue
                kept.append(row)
            if changed:
                working['models'] = kept
        present_creators = {
            row['creator_id'] for row in working['creators']}
        present_keys = set()
        for row in working['models']:
            present_keys.update(merge_keys(row.get('model_id'),
                                           row.get('display_name')))
        appended = False
        for row in new_creators:
            if row['creator_id'] not in present_creators:
                working['creators'].append(dict(row))
                present_creators.add(row['creator_id'])
                appended = True
        for row in new_models:
            keys = merge_keys(row.get('model_id'), row.get('display_name'))
            if present_keys & keys:
                continue
            working['models'].append(dict(row))
            present_keys.update(keys)
            appended = True
        repaired_counts = repair_metadata(working)
        marked = False
        migration = working.get('migration')
        if (context_repair_pending and isinstance(migration, dict)
                and migration.get('receipt_id') == cutover_receipt_id
                and migration.get('context_repair_receipt_id') != cutover_receipt_id):
            migration['context_repair_receipt_id'] = cutover_receipt_id
            marked = True
        return working if (appended or changed or marked or any(repaired_counts)) else None

    authority = repository.mutate(boundary, _mutate)
    summary = {
        'added': len(new_models),
        'creators_added': len(new_creators),
        'pruned': pruned_count,
        'profiles_filled': repaired_counts[0],
        'display_names_filled': repaired_counts[1],
        'identities_bound': repaired_counts[2],
        'metadata_migrated': repaired_counts[3],
        'model_contexts_repaired': len(repaired_counts[4]),
        'revision': authority.revision,
    }
    logger.info('world catalogue sync: %s', summary)
    if repaired_counts[4]:
        logger.info('world catalogue sync: cutover-generated Model contexts repaired: %s',
                    repaired_counts[4])
    return summary


__all__ = ['sync_world_model_catalog', 'withheld_catalog_models']
