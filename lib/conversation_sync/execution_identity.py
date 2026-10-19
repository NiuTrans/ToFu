"""Accepted conversation identity, not autonomous execution authorization.

The machine contract owns envelope keys and reserved public fields. Command
acceptance stamps a least-privilege user identity; executor recovery validates
it before context construction. Nonhuman dispatch remains denied until the
project grant/revocation/cost authority exists. No repository or worker lives
here; the existing attempt config remains the only durable carrier.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from lib.identity import PrincipalContext, require_user_id

_CONTRACT = json.loads((Path(__file__).resolve().parents[2] / 'contracts' /
                        'conversation_execution_identity_v1.json').read_text())
EXECUTION_IDENTITY_CONFIG_KEY = _CONTRACT['configKey']
RESERVED_EXECUTION_FIELDS = frozenset(_CONTRACT['reservedConfigFields'])
REVIEW_SOURCE_CONFIG_KEY = _CONTRACT['reviewSource']['configKey']
REVIEW_SOURCE_SCHEMA = _CONTRACT['reviewSource']['schema']
_ENVELOPE_FIELDS = frozenset(_CONTRACT['fields'])
_PRINCIPAL_FIELDS = frozenset(_CONTRACT['principalFields'])
_SUPPORTED_KINDS = frozenset(_CONTRACT['supportedPrincipalKinds'])


def public_execution_fields(value: Mapping[str, Any]) -> dict[str, Any]:
    """Drop private execution authority without inspecting untrusted values."""
    return {key: item for key, item in value.items() if key not in RESERVED_EXECUTION_FIELDS}


def capture_execution_identity(
    config: Mapping[str, Any], user_id: Any,
    principal: PrincipalContext | None,
) -> dict[str, Any]:
    """Discard client authority and freeze the authenticated minimal identity.

    Explicit numeric-owner internal callers retain their historical user path.
    A system principal is *not* a human authorization just because it has an
    owner. Reject it before accepting a durable executable command.
    """
    owner = require_user_id(user_id, context='conversation task admission')
    if principal is None:
        principal = PrincipalContext.user(subject_id=f'user:{owner}', owner_user_id=owner)
    if not isinstance(principal, PrincipalContext) or principal.require_owner() != owner:
        raise PermissionError('Conversation task principal does not match its owner')
    if principal.kind not in _SUPPORTED_KINDS:
        raise PermissionError('Nonhuman conversation execution requires trusted grant admission')
    # Request API scopes are not durable tool grants; do not regrant them on a
    # recovered task. Preserve subject/tenant for audit, with the existing
    # conversation executor's empty task scopes.
    frozen = PrincipalContext.user(subject_id=principal.subject_id,
        owner_user_id=owner, tenant_id=principal.tenant_id,
        scopes=_CONTRACT['executionScopes'])
    accepted = public_execution_fields(config)
    accepted['_turnPrincipalTenantId'] = frozen.tenant_id or ''
    accepted[EXECUTION_IDENTITY_CONFIG_KEY] = {
        'version': _CONTRACT['version'], 'principal': frozen.to_payload(), 'attended': True,
    }
    return accepted


def restore_execution_identity(config: Mapping[str, Any]) -> tuple[PrincipalContext, bool]:
    """Validate a durable accepted envelope; malformed presence never defaults.

    Legacy absent envelopes preserve the explicit-owner user executor only.
    Explicit nonhuman/false-attendance markers cannot take that compatibility
    path. Live and recovered executors use the same validation port.
    """
    owner = require_user_id(config.get('_turnOwnerUserId'), context='conversation executor')

    if '_projectDispatchAdmission' in config:
        from lib.project_dispatch.admission import restore_dispatch_identity
        return restore_dispatch_identity(config)
    if EXECUTION_IDENTITY_CONFIG_KEY not in config:
        if (config.get('_turnPrincipalKind', 'user') != 'user'
                or config.get('_attended', True) is not True
                or config.get('_unattended', False) is not False
                or '_principal' in config or '_principalContext' in config
                or '_conversationExecutionAdmission' in config):
            raise PermissionError('Legacy conversation identity cannot admit nonhuman execution')
        tenant = config.get('_turnPrincipalTenantId', '')
        if not isinstance(tenant, str) or len(tenant) > 256:
            raise PermissionError('Invalid legacy conversation tenant identity')
        return PrincipalContext.user(subject_id=f'user:{owner}', owner_user_id=owner,
                                     tenant_id=tenant or None), True
    envelope = config[EXECUTION_IDENTITY_CONFIG_KEY]
    if (not isinstance(envelope, Mapping) or set(envelope) != _ENVELOPE_FIELDS
            or type(envelope.get('version')) is not int
            or envelope['version'] != _CONTRACT['version']
            or envelope.get('attended') is not True):
        raise PermissionError('Invalid accepted conversation execution identity')
    payload = envelope.get('principal')
    if (not isinstance(payload, Mapping) or set(payload) != _PRINCIPAL_FIELDS
            or not isinstance(payload.get('kind'), str)
            or payload.get('kind') not in _SUPPORTED_KINDS
            or type(payload.get('owner_user_id')) is not int
            or payload['owner_user_id'] != owner
            or not isinstance(payload.get('subject_id'), str)
            or not payload['subject_id'].strip()
            or len(payload['subject_id']) > 256
            or (payload.get('tenant_id') is not None and
                (not isinstance(payload['tenant_id'], str) or
                 not payload['tenant_id'].strip() or len(payload['tenant_id']) > 256))
            or payload.get('scopes') != _CONTRACT['executionScopes']):
        raise PermissionError('Invalid accepted conversation principal identity')
    principal = PrincipalContext.from_payload(payload)
    if config.get('_turnPrincipalTenantId', '') != (principal.tenant_id or ''):
        raise PermissionError('Accepted conversation tenant identity does not match')
    return principal, True
