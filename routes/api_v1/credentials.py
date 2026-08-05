"""routes/api_v1/credentials.py — Credential vault management.

The operator-facing surface of :mod:`lib.credentials_vault`: one sanctioned,
encrypted-at-rest store for machine/release credentials (GitHub PAT, PyPI
token, …) so they never live in committable files again.

Routes:
  GET    /api/v1/credentials               — list entries (REDACTED: hint only)
  POST   /api/v1/credentials               — create/update {name, value, note?}
  POST   /api/v1/credentials/<name>/reveal — return the plaintext (audited)
  DELETE /api/v1/credentials/<name>        — remove

The reveal endpoint is the ONLY path that returns a value, and it is
audit-logged; the list endpoint can never echo a secret, so the Settings UI
can render the vault without a value ever crossing the wire.
"""

from __future__ import annotations

from flask import Blueprint

from lib.api_response import api_bad_request, api_error, api_ok
from lib.log import audit_log, get_logger
from lib.openapi import api_meta
from lib.request_parser import parse_body

from .auth import require_auth

logger = get_logger(__name__)

api_v1_credentials_bp = Blueprint('api_v1_credentials', __name__)


def _bootstrap():
    """Best-effort one-shot import of legacy .secrets/ files. Idempotent
    (existing entries win), so calling it on every list is cheap and keeps a
    fresh checkout zero-config when the operator already has .secrets/."""
    try:
        from pathlib import Path

        from lib.credentials_vault import bootstrap_from_legacy
        root = Path(__file__).resolve().parent.parent.parent
        bootstrap_from_legacy(root.parent / '.secrets')
    except Exception as e:
        logger.debug('[Vault] legacy bootstrap skipped: %s', e)


@api_v1_credentials_bp.route('/api/v1/credentials', methods=['GET'])
@require_auth
@api_meta(
    summary='List vault entries (redacted)',
    description=(
        'Returns ``{credentials: [...]}`` with ``name`` / ``hint`` / ``note`` / '
        'timestamps. Values are NEVER included — use the reveal endpoint for '
        'a deliberate plaintext read.'
    ),
    tags=['capabilities'],
)
def list_credentials():
    from lib.credentials_vault import list_entries
    _bootstrap()
    return api_ok({'credentials': list_entries()})


@api_v1_credentials_bp.route('/api/v1/credentials', methods=['POST'])
@require_auth
@api_meta(
    summary='Create or update a vault entry',
    description=(
        'Body: ``{name, value, note?}``. ``name`` is normalized to a lowercase '
        'identifier (``github_token``, ``pypi_token``…). Stored Fernet-encrypted; '
        'the response echoes only redacted metadata.'
    ),
    tags=['capabilities'],
)
def upsert_credential():
    from lib.credentials_vault import set_entry

    data = parse_body()
    name = (data.get('name') or '').strip()
    value = (data.get('value') or '').strip()
    if not name:
        return api_bad_request('name is required', field='name')
    if not value:
        return api_bad_request('value is required', field='value')
    try:
        meta = set_entry(name, value, note=data.get('note'))
    except ValueError as e:
        return api_bad_request(str(e), field='name')
    return api_ok({'credential': meta})


@api_v1_credentials_bp.route('/api/v1/credentials/<name>/reveal', methods=['POST'])
@require_auth
@api_meta(
    summary='Reveal a vault entry (plaintext, audited)',
    description=(
        'The ONLY endpoint that returns a credential value. POST (not GET) so '
        'it is a deliberate act; every call is audit-logged.'
    ),
    tags=['capabilities'],
)
def reveal_credential(name):
    from lib.credentials_vault import get_entry, normalize_name

    try:
        n = normalize_name(name)
    except ValueError as e:
        return api_bad_request(str(e), field='name')
    value = get_entry(n)
    if value is None:
        return api_error(f'Unknown credential: {n}', status=404)
    audit_log('credential_vault_reveal', name=n)
    logger.info('[Vault] revealed credential name=%s', n)
    return api_ok({'name': n, 'value': value})


@api_v1_credentials_bp.route('/api/v1/credentials/<name>', methods=['DELETE'])
@require_auth
@api_meta(summary='Remove a vault entry', tags=['capabilities'])
def delete_credential(name):
    from lib.credentials_vault import delete_entry
    if not delete_entry(name):
        return api_error(f'Unknown credential: {name}', status=404)
    return api_ok({'name': name})
