"""lib/memory/user_profile/_pending.py — propose-then-confirm gate.

Staging area for NEW-preference proposals awaiting user confirmation. A
proposal is staged (deduped by text), then later accepted (written into the
profile via ``._mutate.apply_new_preference``) or dismissed. Persisted as a
small JSON list next to the profile (see ``._paths._pending_path``).
"""

from __future__ import annotations

from datetime import datetime, timezone

from lib.log import audit_log, get_logger

from lib.memory.user_profile._mutate import _DEFAULT_HEADER, apply_new_preference
from lib.memory.user_profile._paths import _pending_path

logger = get_logger(__name__)


def load_pending() -> list[dict]:
    """Return the list of staged (unconfirmed) preference proposals."""
    from lib.json_store import read_json
    data = read_json(_pending_path(), default=[])
    return data if isinstance(data, list) else []


def stage_pending(proposal: dict) -> dict:
    """Stage a NEW-preference proposal awaiting user confirmation.

    *proposal* must carry at least ``{'text': ...}``. We mint an ``id`` and a
    ``created`` timestamp, dedupe by identical ``text`` (so the same
    preference proposed twice doesn't pile up), and persist. Returns the
    stored proposal dict (with id).
    """
    from lib.ids import short_id
    from lib.json_store import write_json_atomic

    text = (proposal.get('text') or '').strip()
    if not text:
        return {}
    pending = load_pending()
    for p in pending:
        if (p.get('text') or '').strip() == text:
            return p  # already staged — idempotent
    entry = {
        'id': short_id(n=12),
        'text': text,
        'header': proposal.get('header') or _DEFAULT_HEADER,
        'evidence': (proposal.get('evidence') or '')[:300],
        'created': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    }
    pending.append(entry)
    write_json_atomic(_pending_path(), pending)
    audit_log('user_profile_pending_staged', pref_id=entry['id'])
    return entry


def resolve_pending(pending_id: str, accept: bool,
                    edited_text: str | None = None) -> dict:
    """Confirm (accept) or dismiss a staged proposal.

    On accept, the (optionally user-edited) text is written into the profile
    via :func:`apply_new_preference`. Either way the proposal is removed from
    the pending list. Returns ``{'resolved': bool, 'accepted': bool,
    'profile': <save result or None>}``.
    """
    from lib.json_store import write_json_atomic

    pending = load_pending()
    target = next((p for p in pending if p.get('id') == pending_id), None)
    if target is None:
        return {'resolved': False, 'accepted': False, 'profile': None}
    pending = [p for p in pending if p.get('id') != pending_id]
    write_json_atomic(_pending_path(), pending)

    save_res = None
    if accept:
        text = (edited_text or target.get('text') or '').strip()
        save_res = apply_new_preference(text, header=target.get('header')
                                        or _DEFAULT_HEADER)
    audit_log('user_profile_pending_resolved', pref_id=pending_id,
              accepted=bool(accept))
    return {'resolved': True, 'accepted': bool(accept), 'profile': save_res}
