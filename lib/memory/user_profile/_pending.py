"""lib/memory/user_profile/_pending.py — propose-then-confirm gate.

Staging area for NEW-preference proposals awaiting user confirmation. A
proposal is staged (deduped by text), then later accepted (written into the
profile via ``._mutate.apply_new_preference``) or dismissed. Persisted as a
small JSON list next to the profile (see ``._paths._pending_path``).
"""

from __future__ import annotations

import copy
import os
import time
from datetime import datetime, timezone

from lib.log import audit_log, get_logger

from lib.memory.user_profile._mutate import _DEFAULT_HEADER, apply_new_preference
from lib.memory.user_profile._paths import _pending_path

logger = get_logger(__name__)


_CLAIM_LEASE_SECONDS = 300.0


def _private_store_path(scope: str) -> str:
    path = _pending_path(scope)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)
        try:
            os.chmod(parent, 0o700)
        except OSError as error:
            logger.warning('[UserProfile] pending directory permissions '
                           'could not be restricted: %s', error)
    if os.path.exists(path):
        try:
            os.chmod(path, 0o600)
        except OSError as error:
            logger.warning('[UserProfile] pending file permissions could not '
                           'be restricted: %s', error)
    return path


def _public(entry: dict) -> dict:
    return {key: copy.deepcopy(value) for key, value in entry.items()
            if not key.startswith('_resolution_')}


def load_pending(scope: str = '') -> list[dict]:
    """Return the list of staged (unconfirmed) preference proposals."""
    from lib.json_store import read_json
    data = read_json(_private_store_path(scope), default=[])
    if not isinstance(data, list):
        return []
    return [_public(entry) for entry in data if isinstance(entry, dict)]


def stage_pending(proposal: dict, scope: str = '') -> dict:
    """Stage a NEW-preference proposal awaiting user confirmation.

    *proposal* must carry at least ``{'text': ...}``. We mint an ``id`` and a
    ``created`` timestamp, dedupe by identical ``text`` (so the same
    preference proposed twice doesn't pile up), and persist. Returns the
    stored proposal dict (with id).
    """
    from lib.ids import short_id
    from lib.json_store import JsonStoreReadError, update_json_atomic

    text = (proposal.get('text') or '').strip()
    if not text:
        return {}
    entry = {
        'id': short_id(n=12),
        'text': text,
        'header': proposal.get('header') or _DEFAULT_HEADER,
        'evidence': (proposal.get('evidence') or '')[:300],
        'created': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    }
    outcome: dict = {}

    def _mutate(pending):
        if not isinstance(pending, list):
            raise JsonStoreReadError('pending profile store is not a list')
        for existing in pending:
            if (isinstance(existing, dict)
                    and (existing.get('text') or '').strip() == text):
                outcome['entry'] = _public(existing)
                return None
        updated = list(pending)
        updated.append(copy.deepcopy(entry))
        outcome['entry'] = _public(entry)
        outcome['created'] = True
        return updated

    update_json_atomic(_private_store_path(scope), _mutate, default=[],
                       strict=True, mode=0o600)
    if outcome.get('created'):
        audit_log('user_profile_pending_staged', pref_id=entry['id'])
    return outcome['entry']


def resolve_pending(pending_id: str, accept: bool,
                    edited_text: str | None = None,
                    scope: str = '') -> dict:
    """Confirm (accept) or dismiss a staged proposal.

    On accept, the (optionally user-edited) text is written into the profile
    via :func:`apply_new_preference`. Either way the proposal is removed from
    the pending list. Returns ``{'resolved': bool, 'accepted': bool,
    'profile': <save result or None>}``.
    """
    from lib.ids import short_id
    from lib.json_store import JsonStoreReadError, update_json_atomic

    path = _private_store_path(scope)
    claim = short_id(n=16)
    claimed: dict = {}
    now = time.time()

    def _claim(pending):
        if not isinstance(pending, list):
            raise JsonStoreReadError('pending profile store is not a list')
        for entry in pending:
            if not isinstance(entry, dict) or entry.get('id') != pending_id:
                continue
            prior_claim = entry.get('_resolution_claim')
            try:
                claim_age = now - float(
                    entry.get('_resolution_claimed_at') or 0.0)
            except (TypeError, ValueError) as error:
                logger.debug('[UserProfile] ignored malformed pending claim '
                             'timestamp for %s: %s', pending_id, error)
                claim_age = _CLAIM_LEASE_SECONDS + 1
            if prior_claim and claim_age < _CLAIM_LEASE_SECONDS:
                claimed['busy'] = True
                return None
            entry['_resolution_claim'] = claim
            entry['_resolution_claimed_at'] = now
            claimed['target'] = _public(entry)
            return pending
        claimed['not_found'] = True
        return None

    update_json_atomic(path, _claim, default=[], strict=True, mode=0o600)
    if claimed.get('not_found'):
        return {'resolved': False, 'accepted': False, 'profile': None,
                'not_found': True}
    if claimed.get('busy'):
        return {'resolved': False, 'accepted': False, 'profile': None,
                'busy': True}

    target = claimed['target']
    save_res = None
    if accept:
        text = (edited_text or target.get('text') or '').strip()
        save_res = apply_new_preference(
            text, header=target.get('header') or _DEFAULT_HEADER,
            scope=scope)
        if not save_res.get('saved'):
            def _release(pending):
                if not isinstance(pending, list):
                    raise JsonStoreReadError(
                        'pending profile store is not a list')
                for entry in pending:
                    if (isinstance(entry, dict)
                            and entry.get('id') == pending_id
                            and entry.get('_resolution_claim') == claim):
                        entry.pop('_resolution_claim', None)
                        entry.pop('_resolution_claimed_at', None)
                        return pending
                return None

            update_json_atomic(path, _release, default=[], strict=True,
                               mode=0o600)
            return {'resolved': False, 'accepted': False,
                    'profile': save_res, 'error': 'profile_save_failed'}

    removed: dict = {}

    def _finalize(pending):
        if not isinstance(pending, list):
            raise JsonStoreReadError('pending profile store is not a list')
        updated = []
        for entry in pending:
            if (isinstance(entry, dict) and entry.get('id') == pending_id
                    and entry.get('_resolution_claim') == claim):
                removed['yes'] = True
                continue
            updated.append(entry)
        return updated if removed else None

    update_json_atomic(path, _finalize, default=[], strict=True, mode=0o600)
    if not removed:
        # The claim changed underneath us (for example an operator repaired
        # the file).  Do not claim successful consumption without ownership.
        return {'resolved': False, 'accepted': False, 'profile': save_res,
                'error': 'resolution_claim_lost'}
    audit_log('user_profile_pending_resolved', pref_id=pending_id,
              accepted=bool(accept))
    return {'resolved': True, 'accepted': bool(accept), 'profile': save_res}
