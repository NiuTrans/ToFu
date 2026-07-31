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
import os
import secrets
import sys
import time

from lib.json_store import read_json, update_json_atomic, write_json_atomic
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


def _load() -> list:
    data = read_json(_APPROVALS_FILE, default=None)
    if isinstance(data, dict) and isinstance(data.get('records'), list):
        return data['records']
    return []


def _save(records: list) -> None:
    write_json_atomic(_APPROVALS_FILE, {'records': records})


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
        records = (cur or {}).get('records') or []
        _sweep_expired(records, now)
        records.append(rec)
        return {'records': _prune(records)}

    update_json_atomic(_APPROVALS_FILE, _mut, default={'records': []})
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
        records = (cur or {}).get('records') or []
        _sweep_expired(records, now)
        for rec in records:
            if rec.get('id') != approval_id:
                continue
            if rec.get('status') != 'pending':
                outcome['record'] = None
                return {'records': records}
            rec['status'] = 'approved' if approved else 'denied'
            rec['decided_at'] = now
            rec['decided_by'] = decided_by
            rec['decide_ua'] = decide_ua
            if approved:
                rec['expires_at'] = now + APPROVED_TTL_SEC
            outcome['record'] = dict(rec)
            return {'records': records}
        outcome['record'] = None
        return {'records': records}

    update_json_atomic(_APPROVALS_FILE, _mut, default={'records': []})
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
    """Read one record (sweeping expiry first, best-effort persist)."""
    now = _now()
    records = _load()
    if _sweep_expired(records, now):
        try:
            _save(records)
        except Exception as e:
            logger.debug('[Lifecycle] expiry sweep persist failed: %s', e)
    for rec in records:
        if rec.get('id') == approval_id:
            return _public(rec)
    return None


def list_records(*, status: str | None = None, action: str | None = None,
                 limit: int = 50) -> list:
    """List records newest-first, optionally filtered."""
    now = _now()
    records = _load()
    if _sweep_expired(records, now):
        try:
            _save(records)
        except Exception as e:
            logger.debug('[Lifecycle] expiry sweep persist failed: %s', e)
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
        records = (cur or {}).get('records') or []
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
                return {'records': records}
            outcome['ok'] = False
            outcome['why'] = (f'action-mismatch:{rec.get("action")}'
                              if rec.get('action') != action
                              else f'not-approved:{rec.get("status")}')
            return {'records': records}
        outcome['ok'] = False
        outcome['why'] = 'unknown-id'
        return {'records': records}

    update_json_atomic(_APPROVALS_FILE, _mut, default={'records': []})
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
        records = (cur or {}).get('records') or []
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
            return {'records': records}
        rec = cands[0]
        rec['status'] = 'consumed'
        rec['consumed_at'] = now
        outcome['ok'] = True
        outcome['id'] = rec.get('id')
        return {'records': records}

    update_json_atomic(_APPROVALS_FILE, _mut, default={'records': []})
    ok = bool(outcome.get('ok'))
    if ok:
        audit_log('lifecycle_approval_consumed', approval_id=outcome.get('id'),
                  action=action, via='script-gate')
    else:
        audit_log('lifecycle_approval_consume_rejected', action=action,
                  reason=outcome.get('why'), via='script-gate')
    return ok, outcome.get('why') or '', outcome.get('id')


# ── restart cooldown (idempotency) ────────────────────────────────────

def restart_cooldown_remaining(*, now: float | None = None) -> int:
    """Seconds left in the restart cooldown; 0 when a restart may proceed."""
    now = _now() if now is None else now
    data = read_json(_STATE_FILE, default=None)
    last = (data or {}).get('last_restart_at') if isinstance(data, dict) else None
    if not isinstance(last, (int, float)):
        return 0
    remaining = int(RESTART_COOLDOWN_SEC - (now - last))
    return max(0, remaining)


def stamp_restart(*, now: float | None = None) -> None:
    """Record an accepted restart (called just before the re-exec)."""
    now = _now() if now is None else now
    try:
        write_json_atomic(_STATE_FILE, {'last_restart_at': now})
    except Exception as e:
        # Best-effort: never blocks a restart, but a lost stamp means the
        # cooldown net is open — be loud.
        logger.warning('[Lifecycle] cooldown stamp failed: %s', e)


# ── restart-class call detector (recovery no-refire note) ─────────────

# Substrings that mark a tool call as a server-lifecycle side effect. Kept
# tight on purpose: these are the concrete restart/shutdown entry points
# (HTTP endpoint, shell script, supervisor, internal re-exec helper).
_LIFECYCLE_PATTERNS = (
    'update/restart',
    'update/shutdown',
    'restart_15000.sh',
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
        except (TypeError, ValueError):
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
    'restart_cooldown_remaining', 'stamp_restart', 'detect_lifecycle_calls',
    'consume_any',
]
