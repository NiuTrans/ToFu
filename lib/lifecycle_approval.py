"""lib/lifecycle_approval.py — Human-approval gate for server lifecycle actions.

WHY THIS EXISTS (2026-07-28, epic pt_40d00fd526e5479a)
------------------------------------------------------
Incident: an autopilot conversation (ms4206iqwyb7h4) fired
``POST /api/v1/update/restart {"force": true}`` via ``run_command`` TWICE in
three minutes (12:20:40 + 12:23:25), killing 12 + 11 in-flight tasks across
the fleet. The "approval" it acted on came from its own VIRTUAL user — an
LLM role-playing the owner — and the second fire was the crash-resume
blindly re-emitting the same curl. In open-auth mode every loopback caller
gets a synthetic admin context, so any agent shell could unilaterally
re-exec the whole server.

Owner ruling: a restart/shutdown of a LIVE server must be approved by a
HUMAN, through the UI. This module is the single store + decision engine
behind that gate:

  * :func:`create_request`     — a restart/shutdown attempt with NO token
                                 becomes a *pending* approval record (the
                                 endpoint answers 202; nothing is executed).
  * :func:`decide`             — the human approves/denies in the UI. An
                                 approved record is a ONE-TIME token with a
                                 short TTL.
  * :func:`validate`/:func:`consume`
                               — the retried request carries the approval
                                 id; it executes only when the record is
                                 approved + unexpired + unconsumed, and the
                                 first executor consumes it atomically.
  * :func:`restart_cooldown_remaining` / :func:`stamp_restart`
                               — idempotency: a second restart within
                                 ``RESTART_COOLDOWN_SEC`` of the last
                                 accepted one is refused (429), which is
                                 what stops the crash-resume double-fire
                                 even when a first restart was legitimate.
  * :func:`detect_lifecycle_calls`
                               — substring detector for restart-class
                                 side-effecting commands inside a message's
                                 toolRounds; the recovery/regenerate path
                                 uses it to inject a "result unknown — do
                                 not re-fire" caution note.
  * CLI (``python -m lib.lifecycle_approval --script-gate restart``)
                               — the same token check for
                                 ``restart_15000.sh`` when run
                                 non-interactively (an agent shell), so the
                                 shell-script path cannot bypass the HTTP
                                 gate. Interactive terminals confirm with a
                                 typed prompt instead (see the script).

Deliberate boundaries
---------------------
  * ``tofu_guard`` relaunches a DEAD server; that is recovery, not a
    restart of a live instance, and is NOT gated.
  * ``lib/auto_restart.py`` (``TOFU_AUTO_RESTART=1``) re-execs directly when
    HEAD moves — an explicit operator env opt-in, off by default, and NOT
    routed through this gate by design.
  * In open-auth mode there is no real principal, so a *determined* local
    agent could forge the UI approval dance. The gate makes unilateral
    restart impossible BY DEFAULT, loudly audited at every transition, and
    forces any forgery into the open (an agent caught calling the decide
    endpoint is unambiguously malicious, not careless).
"""

from __future__ import annotations

import json
import math
import os
import secrets
import sys
import time

from lib.json_store import (
    JsonStoreReadError,
    read_json,
    update_json_atomic,
    write_json_atomic as write_json_atomic,  # historical test/debug seam
)
from lib.log import audit_log, get_logger
from lib.runtime_paths import data_root

logger = get_logger(__name__)

ACTIONS = ('restart', 'shutdown')

# An APPROVED record is a one-time token; it must be consumed quickly so a
# stale approval cannot be fired long after the human's intent has moved on.
APPROVED_TTL_SEC = int(os.environ.get('TOFU_LIFECYCLE_APPROVED_TTL', '600') or 600)
# A PENDING record the human never answered expires (housekeeping; the UI
# stops offering it).
PENDING_TTL_SEC = int(os.environ.get('TOFU_LIFECYCLE_PENDING_TTL', '1800') or 1800)
# Idempotency window: a second accepted restart within this many seconds of
# the last is refused (429). Survives the re-exec because the state file is
# read fresh by the new process image.
RESTART_COOLDOWN_SEC = int(os.environ.get('TOFU_LIFECYCLE_RESTART_COOLDOWN', '900') or 900)

_KEEP_RECORDS = 50

# Module-level paths (tests monkeypatch these two).
_APPROVALS_FILE = os.path.join(data_root(), 'lifecycle_approvals.json')
_STATE_FILE = os.path.join(data_root(), 'lifecycle_state.json')

_TERMINAL = ('consumed', 'denied', 'expired')


# ── internals ─────────────────────────────────────────────────────────

def _now() -> float:
    return time.time()


def _sweep_expired(records: list, now: float) -> bool:
    """Lazily mark timed-out pending/approved records expired. Returns changed."""
    changed = False
    for rec in records:
        if rec.get('status') in ('pending', 'approved'):
            exp = rec.get('expires_at')
            if isinstance(exp, (int, float)) and now > exp:
                rec['status'] = 'expired'
                changed = True
    return changed


def _prune(records: list) -> list:
    """Bound the store: keep newest records (requested_at desc)."""
    if len(records) <= _KEEP_RECORDS:
        return records
    return sorted(records, key=lambda r: r.get('requested_at', 0),
                  reverse=True)[:_KEEP_RECORDS]


def _document(data) -> dict:
    """Validate one store snapshot while preserving future root fields."""
    if data is None:
        return {'records': []}
    if not isinstance(data, dict) or not isinstance(data.get('records'), list):
        raise JsonStoreReadError('lifecycle approval store has invalid shape')
    if any(not isinstance(record, dict) for record in data['records']):
        raise JsonStoreReadError(
            'lifecycle approval store contains an invalid record')
    document = dict(data)
    document['records'] = data['records']
    return document


def _load() -> list:
    try:
        return _document(read_json(
            _APPROVALS_FILE, default=None, strict=True))['records']
    except JsonStoreReadError as error:
        # Reads fail closed: a broken approval store never manufactures a
        # usable token, and remains untouched for operator recovery.
        logger.warning('[Lifecycle] approval store read failed: %s', error)
        return []


def _load_swept(now: float) -> list:
    """Return a swept snapshot without stale read-modify-write overwrite."""
    outcome: dict = {}

    def _mut(cur):
        document = _document(cur)
        records = document['records']
        changed = _sweep_expired(records, now)
        outcome['records'] = records
        return document if changed else None

    try:
        update_json_atomic(
            _APPROVALS_FILE, _mut, default=None, strict=True)
    except JsonStoreReadError as error:
        logger.warning('[Lifecycle] approval expiry sweep failed: %s', error)
        return []
    return outcome.get('records') or []


def _public(rec: dict) -> dict:
    """The API-facing shape (all fields — the record carries no secret)."""
    return dict(rec)


# ── request lifecycle ─────────────────────────────────────────────────

def create_request(action: str, origin: dict | None = None) -> dict:
    """Register a pending approval request. Returns the record.

    Every pending creation is LOUD: audit log + app log with the full
    request origin (UA / peer / conversation / force), so attribution of a
    restart attempt never again needs a 30-minute log dig.
    """
    if action not in ACTIONS:
        raise ValueError(f'unknown lifecycle action: {action!r}')
    now = _now()
    rec = {
        'id': secrets.token_urlsafe(16),
        'action': action,
        'status': 'pending',
        'requested_at': now,
        'decided_at': None,
        'expires_at': now + PENDING_TTL_SEC,
        'decided_by': None,
        'origin': dict(origin or {}),
    }

    def _mut(cur):
        document = _document(cur)
        records = document['records']
        _sweep_expired(records, now)
        records.append(rec)
        document['records'] = _prune(records)
        return document

    update_json_atomic(
        _APPROVALS_FILE, _mut, default=None, strict=True)
    origin = rec['origin']
    logger.warning('[Lifecycle] %s PENDING human approval (id=%s, ua=%.80s, '
                   'peer=%s, conv=%s, force=%s, running=%s)',
                   action, rec['id'][:8], origin.get('ua') or '-',
                   origin.get('remote_addr') or '-', origin.get('conv_id') or '-',
                   origin.get('force'), origin.get('running_tasks'))
    audit_log('lifecycle_approval_pending', approval_id=rec['id'], action=action,
              ua=origin.get('ua') or '', remote=origin.get('remote_addr') or '',
              conv_id=origin.get('conv_id') or '', force=bool(origin.get('force')),
              running_tasks=origin.get('running_tasks'))
    return _public(rec)


def decide(approval_id: str, approved: bool, *, decided_by: str = 'ui',
           decide_ua: str = '') -> dict | None:
    """Human decision. Approve → one-time token (short TTL); deny → terminal.

    ``decide_ua`` records the decider's user-agent: in open-auth mode a
    forged approval dance is possible in principle, but a curl-flavoured
    decider UA is then a smoking gun sitting in the audit trail —
    deliberateness is forced into the open (pt_40d00fd526e5479a).

    Returns the updated record, or None when the id is unknown / already
    terminal / expired (fail-closed — a stale pending cannot be approved).
    """
    now = _now()
    outcome: dict = {}

    def _mut(cur):
        document = _document(cur)
        records = document['records']
        _sweep_expired(records, now)
        for rec in records:
            if rec.get('id') != approval_id:
                continue
            if rec.get('status') != 'pending':
                outcome['record'] = None
                return document
            rec['status'] = 'approved' if approved else 'denied'
            rec['decided_at'] = now
            rec['decided_by'] = decided_by
            rec['decide_ua'] = decide_ua
            if approved:
                rec['expires_at'] = now + APPROVED_TTL_SEC
            outcome['record'] = dict(rec)
            return document
        outcome['record'] = None
        return document

    update_json_atomic(
        _APPROVALS_FILE, _mut, default=None, strict=True)
    rec = outcome.get('record')
    if rec is None:
        logger.warning('[Lifecycle] decide(%s, approved=%s) REJECTED — unknown/'
                       'terminal/expired id', approval_id[:8], approved)
        audit_log('lifecycle_approval_decide_rejected',
                  approval_id=approval_id, approved=approved)
        return None
    logger.warning('[Lifecycle] %s %s by %s (id=%s, ua=%.80s)', rec['action'],
                   'APPROVED' if approved else 'DENIED', decided_by,
                   approval_id[:8], decide_ua or '-')
    audit_log('lifecycle_approval_decided', approval_id=approval_id,
              action=rec['action'], approved=approved, decided_by=decided_by,
              decide_ua=decide_ua)
    return _public(rec)


def get(approval_id: str) -> dict | None:
    """Read one record, atomically persisting any expiry transition."""
    now = _now()
    records = _load_swept(now)
    for rec in records:
        if rec.get('id') == approval_id:
            return _public(rec)
    return None


def list_records(*, status: str | None = None, action: str | None = None,
                 limit: int = 50) -> list:
    """List records newest-first, optionally filtered."""
    now = _now()
    records = _load_swept(now)
    out = [r for r in records
           if (status is None or r.get('status') == status)
           and (action is None or r.get('action') == action)]
    out.sort(key=lambda r: r.get('requested_at', 0), reverse=True)
    return [_public(r) for r in out[:limit]]


def validate(approval_id: str, action: str) -> tuple:
    """(ok, why) — approved + right action + unexpired + unconsumed."""
    now = _now()
    records = _load()
    _sweep_expired(records, now)
    for rec in records:
        if rec.get('id') != approval_id:
            continue
        if rec.get('action') != action:
            return False, f'action-mismatch:{rec.get("action")}'
        status = rec.get('status')
        if status == 'approved':
            exp = rec.get('expires_at')
            if isinstance(exp, (int, float)) and now <= exp:
                return True, ''
            return False, 'expired'
        return False, f'not-approved:{status}'
    return False, 'unknown-id'


def consume(approval_id: str, action: str) -> tuple:
    """(ok, why) — atomically flip approved → consumed (first consumer wins).

    Called ONLY at the acceptance point (the action is really being
    executed). A validation that does not lead to execution must NOT
    consume — e.g. a restart refused on running tasks leaves the token
    usable for the force retry.
    """
    now = _now()
    outcome: dict = {}

    def _mut(cur):
        document = _document(cur)
        records = document['records']
        _sweep_expired(records, now)
        for rec in records:
            if rec.get('id') != approval_id:
                continue
            if (rec.get('action') == action and rec.get('status') == 'approved'
                    and isinstance(rec.get('expires_at'), (int, float))
                    and now <= rec['expires_at']):
                rec['status'] = 'consumed'
                rec['consumed_at'] = now
                outcome['ok'] = True
                return document
            outcome['ok'] = False
            outcome['why'] = (f'action-mismatch:{rec.get("action")}'
                              if rec.get('action') != action
                              else f'not-approved:{rec.get("status")}')
            return document
        outcome['ok'] = False
        outcome['why'] = 'unknown-id'
        return document

    update_json_atomic(
        _APPROVALS_FILE, _mut, default=None, strict=True)
    ok = bool(outcome.get('ok'))
    why = outcome.get('why') or ''
    if ok:
        audit_log('lifecycle_approval_consumed', approval_id=approval_id,
                  action=action)
    else:
        audit_log('lifecycle_approval_consume_rejected',
                  approval_id=approval_id, action=action, reason=why)
    return ok, why


def consume_any(action: str) -> tuple:
    """(ok, why, approval_id) — atomically consume the NEWEST fireable token
    for ``action``.

    The shell-script gate path: the human approved SOME pending request in
    the UI; the script does not know the id — it claims the newest
    approved + unexpired + unconsumed record for the action. First consumer
    wins; a second run finds nothing and blocks.
    """
    now = _now()
    outcome: dict = {}

    def _mut(cur):
        document = _document(cur)
        records = document['records']
        _sweep_expired(records, now)
        # newest approved first
        cands = [r for r in records
                 if r.get('action') == action and r.get('status') == 'approved'
                 and isinstance(r.get('expires_at'), (int, float))
                 and now <= r['expires_at']]
        cands.sort(key=lambda r: r.get('decided_at') or 0, reverse=True)
        if not cands:
            outcome['ok'] = False
            outcome['why'] = 'no-approved-token'
            return document
        rec = cands[0]
        rec['status'] = 'consumed'
        rec['consumed_at'] = now
        outcome['ok'] = True
        outcome['id'] = rec.get('id')
        return document

    update_json_atomic(
        _APPROVALS_FILE, _mut, default=None, strict=True)
    ok = bool(outcome.get('ok'))
    if ok:
        audit_log('lifecycle_approval_consumed', approval_id=outcome.get('id'),
                  action=action, via='script-gate')
    else:
        audit_log('lifecycle_approval_consume_rejected', action=action,
                  reason=outcome.get('why'), via='script-gate')
    return ok, outcome.get('why') or '', outcome.get('id')


# ── restart cooldown (idempotency) ────────────────────────────────────

def _valid_timestamp(value) -> bool:
    return (isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value)))


def _cooldown_remaining(last, now: float) -> int:
    if not _valid_timestamp(last):
        return 0
    remaining = int(RESTART_COOLDOWN_SEC - (now - last))
    return max(0, remaining)


def _read_legacy_restart_stamp():
    try:
        data = read_json(_STATE_FILE, default=None, strict=True)
    except JsonStoreReadError as error:
        logger.warning('[Lifecycle] legacy cooldown state read failed: %s', error)
        return None
    return (data or {}).get('last_restart_at') if isinstance(data, dict) else None


def restart_cooldown_remaining(*, now: float | None = None) -> int:
    """Seconds left in the restart cooldown; 0 when a restart may proceed.

    The approval document is the atomic authority because restart-token
    consumption and the timestamp must commit together. ``_STATE_FILE`` is
    still read as a compatibility mirror for stamps written by older builds.
    """
    now = _now() if now is None else now
    approval_last = None
    try:
        document = _document(read_json(
            _APPROVALS_FILE, default=None, strict=True))
        approval_last = document.get('last_restart_at')
    except JsonStoreReadError as error:
        logger.warning('[Lifecycle] approval cooldown read failed: %s', error)
    return max(
        _cooldown_remaining(approval_last, now),
        _cooldown_remaining(_read_legacy_restart_stamp(), now),
    )


def _mirror_restart_stamp(now: float) -> None:
    """Best-effort compatibility mirror; never the acceptance authority."""
    def _mut(cur):
        if cur is not None and not isinstance(cur, dict):
            raise JsonStoreReadError(
                'legacy lifecycle cooldown state has invalid shape')
        document = dict(cur or {})
        previous = document.get('last_restart_at')
        if not _valid_timestamp(previous) or previous < now:
            document['last_restart_at'] = now
        return document

    try:
        update_json_atomic(_STATE_FILE, _mut, default=None, strict=True)
    except (JsonStoreReadError, OSError, ValueError) as error:
        logger.warning('[Lifecycle] cooldown mirror stamp failed: %s', error)


def stamp_restart(*, now: float | None = None) -> None:
    """Record an accepted restart outside the token-acceptance workflow.

    Timestamps advance monotonically, so a delayed older writer cannot shorten
    a newer cooldown. HTTP acceptance uses :func:`consume_restart`, which
    commits the token transition and this authority timestamp together.
    """
    now = _now() if now is None else now

    def _mut(cur):
        document = _document(cur)
        previous = document.get('last_restart_at')
        if not _valid_timestamp(previous) or previous < now:
            document['last_restart_at'] = now
        return document

    try:
        update_json_atomic(
            _APPROVALS_FILE, _mut, default=None, strict=True)
    except (JsonStoreReadError, OSError, ValueError) as error:
        # Preserve the public helper's historical best-effort contract. The
        # compatibility mirror still provides a cooldown when possible.
        logger.warning('[Lifecycle] cooldown authority stamp failed: %s', error)
    _mirror_restart_stamp(now)


def consume_restart(approval_id: str, *, now: float | None = None) -> tuple:
    """Atomically accept one restart token and start the global cooldown.

    Returns ``(ok, why, cooldown_remaining)``. Exactly one transaction owns
    both decisions, so two concurrently approved tokens cannot both pass a
    stale zero-cooldown read and schedule two process re-execs.
    """
    now = _now() if now is None else now
    legacy_last = _read_legacy_restart_stamp()
    outcome: dict = {}

    def _mut(cur):
        document = _document(cur)
        records = document['records']
        _sweep_expired(records, now)
        remaining = max(
            _cooldown_remaining(document.get('last_restart_at'), now),
            _cooldown_remaining(legacy_last, now),
        )
        if remaining > 0:
            outcome.update(ok=False, why='cooldown', remaining=remaining)
            return document
        for rec in records:
            if rec.get('id') != approval_id:
                continue
            if (rec.get('action') == 'restart'
                    and rec.get('status') == 'approved'
                    and isinstance(rec.get('expires_at'), (int, float))
                    and now <= rec['expires_at']):
                rec['status'] = 'consumed'
                rec['consumed_at'] = now
                document['last_restart_at'] = now
                outcome.update(ok=True, why='', remaining=0)
                return document
            outcome.update(
                ok=False,
                why=(f'action-mismatch:{rec.get("action")}'
                     if rec.get('action') != 'restart'
                     else f'not-approved:{rec.get("status")}'),
                remaining=0,
            )
            return document
        outcome.update(ok=False, why='unknown-id', remaining=0)
        return document

    update_json_atomic(
        _APPROVALS_FILE, _mut, default=None, strict=True)
    ok = bool(outcome.get('ok'))
    why = outcome.get('why') or ''
    remaining = int(outcome.get('remaining') or 0)
    if ok:
        _mirror_restart_stamp(now)
        audit_log('lifecycle_approval_consumed', approval_id=approval_id,
                  action='restart', cooldown_started=True)
    else:
        audit_log('lifecycle_approval_consume_rejected',
                  approval_id=approval_id, action='restart', reason=why,
                  cooldown_remaining=remaining)
    return ok, why, remaining


# ── restart-class call detector (recovery no-refire note) ─────────────

# Substrings that mark a tool call as a server-lifecycle side effect. Kept
# tight on purpose: these are the concrete restart/shutdown entry points
# (HTTP endpoint, shell script, supervisor, internal re-exec helper).
_LIFECYCLE_PATTERNS = (
    'update/restart',
    'update/shutdown',
    'restart_15000.sh',
    'serverctl.py restart',
    'supervisorctl restart',
    '_perform_server_reexec',
)


def detect_lifecycle_calls(toolrounds: list | None) -> list:
    """Return the matched lifecycle patterns inside a message's toolRounds.

    ``toolrounds`` is the persisted per-round list on an assistant message;
    each round's JSON is substring-scanned. Pure (no I/O) — unit-tested.
    """
    if not isinstance(toolrounds, list) or not toolrounds:
        return []
    matched: set = set()
    for rnd in toolrounds:
        try:
            hay = json.dumps(rnd, ensure_ascii=False)
        except (TypeError, ValueError) as e:
            logger.debug('[Lifecycle] toolround not JSON-serializable (%s) — skipped', e)
            continue
        for pat in _LIFECYCLE_PATTERNS:
            if pat in hay:
                matched.add(pat)
    return sorted(matched)


# ── CLI: shell-script gate ─────────────────────────────────────────────

def _script_gate(action: str) -> int:
    """Consume an approved token for ``action``; exit code 0 ok / 3 blocked.

    Used by ``restart_15000.sh`` when run non-interactively. When there is
    no approved token the message tells the operator exactly how to mint
    one (approve in the UI), so the block is self-explanatory in logs.
    """
    ok, why, _aid = consume_any(action)
    if ok:
        print(f'[lifecycle-gate] approved {action} token consumed — proceeding.')
        return 0
    print('════════════════════════════════════════════════════════════════')
    print(f'[lifecycle-gate] REFUSING: no valid human-approved {action} token ({why}).')
    print('       Restarting/shutting down a LIVE server requires HUMAN approval:')
    print('       open the Tofu UI → Settings → 更新 (Update) → approve the pending')
    print(f'       {action} request, then re-run this script.')
    print('       (Interactive terminals confirm by typing instead; recovery with')
    print('        no live server on the port is never gated.)')
    print('════════════════════════════════════════════════════════════════')
    return 3


def main(argv: list) -> int:
    args = list(argv[1:])
    if args and args[0] == '--script-gate':
        action = args[1] if len(args) > 1 else 'restart'
        if action not in ACTIONS:
            print(f'[lifecycle-gate] unknown action {action!r} (want one of {ACTIONS})')
            return 3
        return _script_gate(action)
    print('usage: python -m lib.lifecycle_approval --script-gate [restart|shutdown]')
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv))


__all__ = [
    'ACTIONS', 'APPROVED_TTL_SEC', 'PENDING_TTL_SEC', 'RESTART_COOLDOWN_SEC',
    'create_request', 'decide', 'get', 'list_records', 'validate', 'consume',
    'restart_cooldown_remaining', 'stamp_restart', 'consume_restart',
    'detect_lifecycle_calls', 'consume_any',
]
