"""Import the provider authority carried by an internal export.

Owner decision 2026-11-01: an internal export must work out of the box, API
keys included. Those keys live in the model-routing v2 authority, so the
export carries a redacted-document-plus-secrets bundle rather than the frozen
legacy ``providers[]`` list. This module is the only runtime reader of that
bundle; the export side has its own writer, and the two share only the
contract string below.

Semantics
---------
* Import happens only while the destination authority is inactive (revision 0)
  — a fresh install. An already-active owner authority is never overwritten or
  merged: the colleague's own providers and keys are theirs.
* Every secret is re-sealed with the DESTINATION's own installation key under
  its original reference, then the document commits with one CAS. A failure
  leaves the bundle in place so the next boot retries; a successful import
  deletes the plaintext bundle (the authority now holds the encrypted copy).
* Non-``api_key`` credentials (OAuth subscription sessions) are not carried by
  the exporter; the destination signs in for itself.
"""
from __future__ import annotations

import json
import os

from lib.config_dir import config_path
from lib.log import get_logger

from .domain import ModelRoutingError, empty_document, normalize_document
from .repository import ModelRoutingRepository, OwnerBoundary

logger = get_logger(__name__)

CONTRACT_VERSION = 'tofu.carried-model-routing/v1'
_FILENAME = 'carried_model_routing.json'
_MAX_BUNDLE_BYTES = 64 * 1024 * 1024


def carried_path() -> str:
    return config_path(_FILENAME)


def read_carried_bundle(path: str | None = None) -> dict | None:
    """Return the validated bundle, or None when no export carried one."""
    target = path or carried_path()
    try:
        with open(target, 'rb') as handle:
            raw = handle.read(_MAX_BUNDLE_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(raw) > _MAX_BUNDLE_BYTES:
        raise ModelRoutingError('carried provider bundle is too large',
                                kind='carried_model_routing_invalid')
    try:
        payload = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelRoutingError('carried provider bundle is not valid JSON',
                                kind='carried_model_routing_invalid') from exc
    if not isinstance(payload, dict) or payload.get('contract_version') != CONTRACT_VERSION:
        raise ModelRoutingError('carried provider bundle has an unsupported contract',
                                kind='carried_model_routing_invalid')
    document = payload.get('document')
    secrets = payload.get('secrets')
    if not isinstance(document, dict) or not isinstance(secrets, dict):
        raise ModelRoutingError('carried provider bundle is incomplete',
                                kind='carried_model_routing_invalid')
    for credential_id, plaintext in secrets.items():
        if not isinstance(credential_id, str) or not isinstance(plaintext, str) or not plaintext:
            raise ModelRoutingError('carried provider bundle has an invalid secret entry',
                                    kind='carried_model_routing_invalid')
    return payload


def import_carried_model_routing(
    repository: ModelRoutingRepository,
    boundary: OwnerBoundary,
    *,
    path: str | None = None,
) -> dict | None:
    """Import into an inactive authority; return a receipt or None."""
    bundle = read_carried_bundle(path)
    if bundle is None:
        return None
    if repository.get(boundary).revision:
        logger.info('Carried provider bundle left unimported: owner=%s already '
                    'has an active model-routing authority',
                    boundary.owner_user_id)
        return {'status': 'skipped_active_authority',
                'owner_user_id': boundary.owner_user_id}

    document = normalize_document(bundle['document'], revision=0)
    secrets = bundle['secrets']
    known_references = {str(row.get('secret_reference') or '')
                        for row in document['credentials']}
    for credential in document['credentials']:
        credential_id = str(credential.get('credential_id') or '')
        reference = str(credential.get('secret_reference') or '')
        if reference not in known_references or not reference:
            raise ModelRoutingError(
                f'carried credential {credential_id} has no secret reference',
                kind='carried_model_routing_invalid')
        if credential_id not in secrets:
            raise ModelRoutingError(
                f'carried credential {credential_id} has no plaintext in the bundle',
                kind='carried_model_routing_invalid')
        repository.put_secret(boundary, secrets[credential_id], secret_reference=reference)

    committed = repository.compare_and_swap(boundary, document, expected_revision=0)
    target = path or carried_path()
    try:
        os.unlink(target)
    except OSError as exc:
        logger.warning('Could not remove imported provider bundle %s: %s', target, exc)
    logger.info('Imported carried provider authority owner=%s revision=%s '
                'providers=%d credentials=%d', boundary.owner_user_id,
                committed.revision, len(document['providers']), len(secrets))
    return {'status': 'imported_carried_authority',
            'owner_user_id': boundary.owner_user_id,
            'revision': committed.revision,
            'providers': len(document['providers']),
            'credentials': len(secrets)}


__all__ = ['CONTRACT_VERSION', 'carried_path', 'read_carried_bundle',
           'import_carried_model_routing', 'empty_document']
