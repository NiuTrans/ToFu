"""One-way startup cutover from legacy configuration to the v2 authority.

Personal mode has one known owner today.  The owner-taking entry point keeps
the repository seam explicit; distributed deployments must enumerate owners
at their authentication/storage boundary instead of assuming a global user.
"""

from __future__ import annotations

from collections.abc import Mapping
import copy
import json
import os

from lib.config_dir import config_path
from lib.identity import PERSONAL_USER_ID
from lib.log import get_logger
from lib.provider_headers import split_header_secrecy

from .credential_envelope import (
    ENVELOPE_FORMAT,
    credential_secret_display_hint,
    decode_credential_envelope,
)
from .discovered_provider import (
    build_discovered_provider_bundle,
    discovered_provider_id,
)
from .domain import ModelRoutingError, empty_document, normalize_document
from .managed_provider import replace_managed_provider
from .migration import MigrationPlan, execute_migration, plan_legacy_migration
from .repository import ModelRoutingRepository, OwnerBoundary


logger = get_logger(__name__)
_PENDING_PROVIDER_CONTRACT = 'tofu.bootstrap-provider-stage/v1'
_MAX_PENDING_PROVIDER_BYTES = 4 * 1024 * 1024


def _pending_provider_path() -> str:
    return config_path('.bootstrap-provider-pending.json')


def _read_pending_provider() -> dict | None:
    path = _pending_provider_path()
    try:
        with open(path, 'rb') as pending_file:
            encoded = pending_file.read(_MAX_PENDING_PROVIDER_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(encoded) > _MAX_PENDING_PROVIDER_BYTES:
        raise ModelRoutingError(
            'bootstrap provider draft exceeds 4 MiB',
            kind='bootstrap_provider_invalid',
        )
    try:
        payload = json.loads(encoded.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelRoutingError(
            'bootstrap provider draft is not valid JSON',
            kind='bootstrap_provider_invalid',
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get('contract_version') != _PENDING_PROVIDER_CONTRACT
    ):
        raise ModelRoutingError(
            'bootstrap provider draft has an unsupported contract',
            kind='bootstrap_provider_invalid',
        )
    return payload


def _import_pending_provider(
    repository: ModelRoutingRepository,
    boundary: OwnerBoundary,
) -> dict | None:
    """Import and consume the repair launcher's secret-free v2 draft."""

    pending = _read_pending_provider()
    if pending is None:
        return None
    credential_env = str(pending.get('credential_env') or '')
    if credential_env not in {'LLM_API_KEYS', 'LLM_API_KEY'}:
        raise ModelRoutingError(
            'bootstrap provider credential_env is not allowed',
            kind='bootstrap_provider_invalid',
        )
    raw_secret = os.environ.get(credential_env, '')
    api_key = next((part.strip() for part in raw_secret.split(',')
                    if part.strip()), '')
    if not api_key:
        raise ModelRoutingError(
            f'bootstrap provider awaits {credential_env}',
            kind='bootstrap_provider_secret_missing',
        )

    base_url = str(pending.get('base_url') or '').strip()
    brand = str(pending.get('brand') or 'generic').strip()
    provider_id = discovered_provider_id(brand, base_url)
    bundle = build_discovered_provider_bundle(
        provider_id=provider_id,
        display_name=str(pending.get('name') or 'Bootstrap Provider'),
        brand=brand,
        base_url=base_url,
        models=pending.get('models') or [],
        protocol=str(pending.get('protocol') or 'openai'),
    )
    credential_id = bundle['credentials'][0]['credential_id']
    fragment = {
        'providers': [bundle['provider']],
        'provider_accesses': [bundle['provider_access']],
        'connections': bundle['connections'],
        'credentials': bundle['credentials'],
        'offerings': bundle['offerings'],
        'deployments': bundle['deployments'],
    }
    mutation = replace_managed_provider(
        repository,
        boundary,
        provider_id=provider_id,
        bundle=fragment,
        credential_plaintexts={credential_id: api_key},
    )
    try:
        os.unlink(_pending_provider_path())
    except OSError as exc:
        # The aggregate commit is authoritative. A leftover stage is safe to
        # replay (replace_managed_provider is idempotent) and preferable to
        # reporting the committed credential as absent.
        logger.warning(
            'Could not remove imported bootstrap provider draft: %s', exc)
    logger.info(
        'Imported bootstrap provider into model-routing v2 owner=%s '
        'provider=%s revision=%s',
        boundary.owner_user_id,
        provider_id,
        mutation.authority.revision,
    )
    return {
        'status': 'imported',
        'provider_id': provider_id,
        'revision': mutation.authority.revision,
        'changed': mutation.changed,
    }


def _legacy_sources(boundary: OwnerBoundary) -> tuple[dict, list[dict]]:
    from lib import _load_server_config
    from lib.byo_providers import get_provider, list_providers

    rows = list_providers(
        boundary.owner_user_id, tenant_id=boundary.tenant_id)
    private_rows = [
        provider
        for row in rows
        if (provider := get_provider(
            row['id'],
            boundary.owner_user_id,
            tenant_id=boundary.tenant_id,
        )) is not None
    ]
    return _load_server_config(), private_rows


def _merge_missing_legacy_plan(
    current_document: dict,
    plan: MigrationPlan,
) -> MigrationPlan:
    """Compose a failed-cutover repair without replacing v2-only providers.

    A migration may fail before authority activation and OAuth reconciliation
    may then create a small v2 aggregate.  Retrying the original plan wholesale
    would erase that OAuth ProviderAccess.  This merge preserves the current
    authority and adds only the legacy providers absent from it.
    """
    candidate = copy.deepcopy(current_document)
    from .catalog_merge import merge_missing_catalog_entities
    merge_missing_catalog_entities(candidate, plan.document)

    for collection in (
        'providers', 'provider_accesses', 'connections', 'credentials',
        'offerings', 'deployments',
    ):
        candidate[collection].extend(copy.deepcopy(plan.document[collection]))

    return MigrationPlan(
        document=normalize_document(candidate),
        issues=list(plan.issues),
        source_digest=plan.source_digest,
        redacted_backup=copy.deepcopy(plan.redacted_backup),
        _secrets=list(plan._secrets),
    )


def _recover_missing_legacy_providers(
    repository: ModelRoutingRepository,
    boundary: OwnerBoundary,
    current,
) -> dict | None:
    """Retry an incomplete first cutover while preserving later v2 writes."""
    if current.document.get('migration'):
        return None
    server_config, private_rows = _legacy_sources(boundary)
    existing_provider_ids = {
        row['provider_id'] for row in current.document['providers']
    }

    def missing(row: dict) -> bool:
        legacy_id = str(row.get('id') or row.get('key') or '').strip()
        return not legacy_id or legacy_id not in existing_provider_ids

    legacy_providers = server_config.get('providers')
    missing_public = [
        copy.deepcopy(row) for row in (legacy_providers or [])
        if isinstance(row, dict) and missing(row)
    ] if isinstance(legacy_providers, list) else []
    missing_private = [
        copy.deepcopy(row) for row in private_rows
        if isinstance(row, dict) and missing(row)
    ]
    if not missing_public and not missing_private:
        return None

    repair_config = copy.deepcopy(server_config)
    repair_config['providers'] = missing_public
    plan = plan_legacy_migration(
        repair_config,
        byo_providers=missing_private,
        official_directory=current.document['models'],
    )
    merged = _merge_missing_legacy_plan(current.document, plan)
    migration = execute_migration(repository, boundary, merged)
    if not migration.enabled or migration.authority is None:
        logger.error(
            'Model-routing legacy recovery not activated owner=%s receipt=%s',
            boundary.owner_user_id,
            migration.receipt.get('receipt_id', ''),
        )
        return {
            'status': 'migration_failed',
            'owner_user_id': boundary.owner_user_id,
            'revision': current.revision,
            'receipt': migration.receipt,
        }
    logger.info(
        'Recovered %d legacy ProviderAccess resources into v2 owner=%s '
        'revision=%s',
        len(missing_public) + len(missing_private),
        boundary.owner_user_id,
        migration.authority.revision,
    )
    return {
        'status': 'recovered_legacy',
        'owner_user_id': boundary.owner_user_id,
        'revision': migration.authority.revision,
        'receipt': migration.receipt,
    }


def _repair_credential_secrets(
    repository: ModelRoutingRepository,
    boundary: OwnerBoundary,
) -> int:
    """Relocate inspectable headers out of credential envelopes, fix hints.

    Early v2 writers folded every legacy provider header into the encrypted
    credential-secret envelope and stored a ``secret_hint`` of that whole
    envelope as the credential ``key_hint``.  Masked key cards then rendered
    the JSON chrome (``{"ap****…``) instead of the key's own digits, the
    reveal eye showed raw envelope JSON, and routing headers vanished from
    the 自定义请求头 editor.  This pass moves plain routing headers back onto
    their authorized connections, re-seals each envelope with only credential
    material, and re-derives hints from the usable key.  It is idempotent
    and never weakens secrecy: credential-named and transport-reserved
    headers stay encrypted, and a header only moves when no authorized
    connection already carries a conflicting value (dispatch merges
    connection headers under envelope headers, so partial moves would change
    wire behavior).
    """
    authority = repository.get(boundary)
    connections_by_id = {
        str(row.get('connection_id') or ''): row
        for row in authority.document['connections']
    }
    planned_headers: dict[str, dict[str, str]] = {
        connection_id: dict(row.get('extra_headers') or {})
        for connection_id, row in connections_by_id.items()
    }
    hint_updates: dict[str, str] = {}
    reseal: list[tuple[str, str]] = []
    moved_headers = 0
    for credential in authority.document['credentials']:
        reference = str(credential.get('secret_reference') or '')
        if not reference:
            continue
        try:
            plaintext = repository.resolve_secret(boundary, reference)
        except Exception as exc:
            logger.debug('credential secret resolution failed: %s',
                         type(exc).__name__)
            continue
        new_plaintext = plaintext
        envelope = decode_credential_envelope(plaintext)
        if envelope is not None:
            raw_headers = envelope.get('extra_headers')
            headers = (
                {str(name): str(value) for name, value in raw_headers.items()}
                if isinstance(raw_headers, Mapping) else {}
            )
            envelope_named, plain = split_header_secrecy(headers)
            authorized = [
                connection_id for connection_id in (
                    (credential.get('authorization') or {}).get('connection_ids')
                    or [])
                if str(connection_id) in planned_headers
            ]
            movable = (
                {
                    name: value for name, value in plain.items()
                    if all(
                        planned_headers[cid].get(name) in (None, value)
                        for cid in authorized)
                }
                if authorized else {}
            )
            if movable:
                kept = dict(envelope_named)
                for name, value in plain.items():
                    if name not in movable:
                        # Conflicted plain header stays encrypted to preserve
                        # the envelope-over-connection dispatch precedence.
                        kept[name] = value
                for cid in authorized:
                    planned_headers[cid].update(movable)
                moved_headers += len(movable)
                new_plaintext = json.dumps({
                    'format': ENVELOPE_FORMAT,
                    'api_key': str(envelope.get('api_key') or ''),
                    'oauth': str(envelope.get('oauth') or ''),
                    'extra_headers': kept,
                }, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
                reseal.append((reference, new_plaintext))
        hint = credential_secret_display_hint(new_plaintext)
        if hint != str(credential.get('key_hint') or ''):
            hint_updates[str(credential['credential_id'])] = hint
    header_delta = {
        connection_id: headers
        for connection_id, headers in planned_headers.items()
        if headers != dict(
            connections_by_id[connection_id].get('extra_headers') or {})
    }
    if not hint_updates and not header_delta:
        return 0

    def apply(document: dict) -> None:
        for connection in document['connections']:
            delta = header_delta.get(str(connection.get('connection_id') or ''))
            if delta is not None:
                connection['extra_headers'] = dict(delta)
        for credential in document['credentials']:
            hint = hint_updates.get(str(credential.get('credential_id') or ''))
            if hint is not None:
                credential['key_hint'] = hint

    repository.mutate(boundary, apply)
    # Document first, secrets second: a crash between the two leaves headers
    # duplicated on both surfaces, which dispatch merges to identical values;
    # the next boot converges.
    for reference, secret_plaintext in reseal:
        repository.put_secret(
            boundary, secret_plaintext, secret_reference=reference)
    logger.info(
        'Repaired credential secrets owner=%s revision=%s '
        '(headers relocated=%d, hints=%d)',
        boundary.owner_user_id,
        repository.get(boundary).revision,
        moved_headers,
        len(hint_updates),
    )
    return moved_headers + len(hint_updates)


def bootstrap_owner_model_routing(
    boundary: OwnerBoundary,
    *,
    repository: ModelRoutingRepository | None = None,
) -> dict:
    """Activate v2 once, then reconcile any staged first-run provider."""
    authority_repository = repository or ModelRoutingRepository()
    current = authority_repository.get(boundary)
    if current.revision:
        try:
            _repair_credential_secrets(authority_repository, boundary)
            current = authority_repository.get(boundary)
        except Exception as exc:
            logger.warning(
                'Credential secret repair deferred owner=%s: %s',
                boundary.owner_user_id,
                str(exc)[:300],
            )
        result = _recover_missing_legacy_providers(
            authority_repository, boundary, current)
        if result is None:
            result = {
                'status': 'already_active',
                'revision': current.revision,
                'owner_user_id': boundary.owner_user_id,
            }
    else:
        server_config, private_rows = _legacy_sources(boundary)
        legacy_providers = server_config.get('providers')
        has_legacy_providers = (
            isinstance(legacy_providers, list) and bool(legacy_providers)
        ) or bool(private_rows)
        if not has_legacy_providers:
            activated = authority_repository.compare_and_swap(
                boundary,
                empty_document(),
                expected_revision=0,
            )
            result = {
                'status': 'initialized_empty',
                'revision': activated.revision,
                'owner_user_id': boundary.owner_user_id,
            }
        else:
            plan = plan_legacy_migration(
                server_config, byo_providers=private_rows)
            migration = execute_migration(
                authority_repository, boundary, plan)
            if not migration.enabled or migration.authority is None:
                logger.error(
                    'Model-routing v2 migration not activated owner=%s '
                    'receipt=%s',
                    boundary.owner_user_id,
                    migration.receipt.get('receipt_id', ''),
                )
                return {
                    'status': 'migration_failed',
                    'owner_user_id': boundary.owner_user_id,
                    'receipt': migration.receipt,
                }
            logger.info(
                'Model-routing v2 authority active owner=%s revision=%s',
                boundary.owner_user_id,
                migration.authority.revision,
            )
            result = {
                'status': 'migrated',
                'revision': migration.authority.revision,
                'owner_user_id': boundary.owner_user_id,
                'receipt': migration.receipt,
            }

    try:
        pending = _import_pending_provider(authority_repository, boundary)
    except ModelRoutingError as exc:
        logger.error(
            'Bootstrap provider import deferred owner=%s kind=%s: %s',
            boundary.owner_user_id,
            exc.kind,
            exc,
        )
        result['pending_provider'] = {
            'status': 'deferred',
            'error_kind': exc.kind,
        }
        return result
    if pending is not None:
        result['pending_provider'] = pending
        result['revision'] = pending['revision']
    return result


def bootstrap_personal_model_routing() -> dict:
    """Cut over the personal owner; fail closed in distributed mode."""
    from runtime_guards import load_deployment_configuration

    deployment = load_deployment_configuration()
    if deployment.mode != 'personal':
        logger.info(
            'Model-routing bootstrap awaits owner enumeration in %s mode',
            deployment.mode,
        )
        return {'status': 'owner_enumeration_required', 'mode': deployment.mode}
    return bootstrap_owner_model_routing(
        OwnerBoundary.create(PERSONAL_USER_ID))


__all__ = [
    'bootstrap_owner_model_routing',
    'bootstrap_personal_model_routing',
]
