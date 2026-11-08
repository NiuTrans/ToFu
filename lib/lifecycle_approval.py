"""lib/lifecycle_approval.py — Human-approval gate for server lifecycle actions.

WHY THIS EXISTS (2026-07-28, )
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
    / :func:`restart_pending_on_this_process`
                               — idempotency: a second restart within
                                 ``RESTART_COOLDOWN_SEC`` of the last
                                 accepted one is refused (429).
                                 ``restartPending`` is true only while the
                                 accepting PID is still alive, so a 429
                                 cannot claim a bounce that has not happened.
                                 DISABLED BY DEFAULT (window = 0) since the
                                 2026-09-20 owner ruling: the one-time
                                 consumed token above is what actually stops
                                 the crash-resume double-fire, while a
                                 15-minute window also blocked legitimate
                                 human re-restarts during frontend iteration.
                                 The mechanism stays available as an opt-in
                                 (``TOFU_LIFECYCLE_RESTART_COOLDOWN``).
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

Human provenance and the owner's delegation (owner decision 2026-11-06)
------------------------------------------------------------------------
The 2026-07-28 ruling ("a human approves every live-server restart") is
narrowed to: a human approves every restart, OR the owner has granted their
Global Router a revocable delegation (docs/ROUTER.md R9). Every decision this
gate accepts names its provenance:

  * :func:`decide` requires ``human-ui`` provenance, which only the HTTP
    boundary derives from an owner's browser session
    (``routes/api_v1/auth.human_ui_provenance``). A direct call with a
    free-text ``decided_by`` (11-05: "explicit-user-instruction-via-codex-chat")
    is refused and audited: agents never approve.
  * :func:`set_delegation` grants only with ``human-ui`` provenance; the grant
    names its grantee (the owner's current Global Router conversation and
    binding revision) and covers ``DELEGATION_SCOPES``. Revocation is
    immediate: every delegated action re-checks the grant in the same
    transaction that commits it.
  * :func:`begin_delegated_action` / :func:`accept_delegated_restart` are the
    delegation branch of this gate: the grant check, the cooldown stamp and
    the audit record commit together. There is no token, environment
    variable or pending request on this path, and no grant means a typed
    :class:`DelegationRefused` (audited, nothing pending).
  * ``serverctl.py`` keeps the operator paths (an interactive terminal's
    typed confirmation, and the ``TOFU_LIFECYCLE_GATE_PASSED`` /
    ``TOFU_RESTART_GATE_PASSED`` handoff of a caller that already passed
    this gate), but :func:`record_operator_pass` writes each as a distinct,
    audited ``interactive-terminal`` / ``environment-override`` record.

Residual risks (stated, not solved): open mode has no authentication, so a
local process can forge a loopback request with browser Fetch Metadata; such
provenance is recorded as ``assurance: advisory`` (a UI session cookie is
``authenticated``). An agent driving the owner's real browser acts through the
owner's session and is indistinguishable here; the browser access policy
(page mutation needs the owner's per-domain approval, the Tofu client tab is
never an automation target) and the durable tool rounds of that agent are the
existing controls and evidence, and every decision keeps its user agent and
peer in the record and in audit.log.
"""

from __future__ import annotations

import hashlib
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
from lib.runtime.runtime_paths import data_root

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
# read fresh by the new process image. DEFAULT 0 = OFF (owner ruling
# 2026-09-20): the one-time approval token consumed at acceptance is the real
# anti-double-fire net; a long window mostly blocked humans re-restarting
# during iteration. Set TOFU_LIFECYCLE_RESTART_COOLDOWN to opt back in.
RESTART_COOLDOWN_SEC = int(os.environ.get('TOFU_LIFECYCLE_RESTART_COOLDOWN', '0') or 0)

_KEEP_RECORDS = 50

# Module-level paths (tests monkeypatch these two).
_APPROVALS_FILE = os.path.join(data_root(), 'lifecycle_approvals.json')
_STATE_FILE = os.path.join(data_root(), 'lifecycle_state.json')

_TERMINAL = ('consumed', 'denied', 'expired')

# What the owner's delegation to their Global Router covers. One grant covers
# all of them: the owner turns one authorization on or off (AGENTS.md #5).
DELEGATION_SCOPES = ('restart', 'conversation_control')
PROVENANCE_HUMAN_UI = 'human-ui'
PROVENANCE_TERMINAL = 'interactive-terminal'
PROVENANCE_ENVIRONMENT = 'environment-override'
PROVENANCE_DELEGATION = 'owner-delegation'
_HUMAN_CREDENTIALS = ('session', 'open-mode')
# A same-origin browser request: Fetch Metadata where the browser sends it
# (trustworthy origins), else an Origin header naming this host.
_BROWSER_EVIDENCE = ('fetch-metadata', 'origin')


class DelegationRefused(PermissionError):
    """A typed refusal of an action under the owner's delegation; nothing ran."""

    execution_started = False

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


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

def record_restart_preparation(approval_id: str, progress: dict) -> None:
    """Bounded projection on the existing approval record, never an approval change.

    The admin approval GET owns reconnect/poll recovery. Fifty retained approval
    records bound this diagnostic state; raw compiler logs are never persisted.
    """
    def update(current):
        document = _document(current)
        for record in document['records']:
            if record.get('id') == approval_id and record.get('action') == 'restart':
                record['preparation'] = {**progress, 'updatedAt': _now()}
                return document
        return None
    update_json_atomic(_APPROVALS_FILE, update, default=None, strict=True)


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


def require_human_provenance(provenance) -> dict:
    """Return the validated ``human-ui`` provenance or raise PermissionError.

    Only the HTTP boundary mints it from an owner's same-origin browser
    request (``routes/api_v1/auth.human_ui_provenance``): a user principal
    over the UI session (or the loopback open-mode UI), never an API key.
    In-process callers cannot turn a chat instruction into this shape by
    accident; forging it is deliberate and lands in the audit trail. In open
    mode it is only a claim (see :func:`provenance_assurance`).
    """
    fields = ('kind', 'ownerUserId', 'subject', 'credential', 'browser', 'userAgent',
              'remoteAddr')
    if (not isinstance(provenance, dict) or set(provenance) != set(fields)
            or provenance['kind'] != PROVENANCE_HUMAN_UI
            or type(provenance['ownerUserId']) is not int or provenance['ownerUserId'] < 1
            or provenance['credential'] not in _HUMAN_CREDENTIALS
            or provenance['browser'] not in _BROWSER_EVIDENCE
            or not all(isinstance(provenance[name], str)
                       for name in ('subject', 'userAgent', 'remoteAddr'))):
        raise PermissionError(
            'human_approval_required: only the owner, acting in the Tofu UI, can approve '
            'this; agents never approve lifecycle actions or grant delegations')
    return dict(provenance)


def provenance_assurance(provenance: dict) -> str:
    """How much a ``human-ui`` provenance proves: ``authenticated`` or ``advisory``.

    A UI session cookie is a real credential. Open mode has no
    authentication: any local process can send a loopback request with
    browser Fetch Metadata, so its provenance only records a claim.
    """
    return 'authenticated' if provenance.get('credential') == 'session' else 'advisory'


def decide(approval_id: str, approved: bool, *, provenance) -> dict | None:
    """Human decision. Approve → one-time token (short TTL); deny → terminal.

    ``provenance`` must be the ``human-ui`` provenance of the owner's browser
    request; anything else (an agent acting on a chat instruction) raises
    PermissionError before the store changes. The decider's user-agent stays
    on the record (``decide_ua``), so a forged approval dance in open-auth
    mode remains a smoking gun in the audit trail.

    Returns the updated record, or None when the id is unknown / already
    terminal / expired (fail-closed — a stale pending cannot be approved).
    """
    try:
        human = require_human_provenance(provenance)
    except PermissionError:
        audit_log('lifecycle_approval_decide_refused', approval_id=approval_id,
                  approved=approved, provenance=str(provenance)[:300])
        logger.warning('[Lifecycle] decide(%s) REFUSED: no human UI provenance',
                       str(approval_id)[:8])
        raise
    decided_by, decide_ua = 'ui', human['userAgent']
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
            rec['decided_via'] = human
            rec['decided_assurance'] = provenance_assurance(human)
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
              decide_ua=decide_ua, credential=human['credential'],
              remote=human['remoteAddr'])
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


def _read_last_restart_pid():
    try:
        document = _document(read_json(
            _APPROVALS_FILE, default=None, strict=True))
    except JsonStoreReadError as error:
        logger.warning('[Lifecycle] restart pid read failed: %s', error)
        return None
    raw = document.get('last_restart_pid')
    try:
        pid = int(raw)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def restart_pending_on_this_process(*, now: float | None = None) -> bool:
    """True while cooldown is active and this PID is still the accepter.

    After a real replacement the new image has a different PID, so a 429
    can distinguish "already bounced" from "accepted, still waiting".
    Stamps written before pid recording cannot claim either way.
    """
    if restart_cooldown_remaining(now=now) <= 0:
        return False
    pid = _read_last_restart_pid()
    return pid is not None and pid == os.getpid()


def _apply_restart_stamp(document, now: float) -> None:
    previous = document.get('last_restart_at')
    if not _valid_timestamp(previous) or previous < now:
        document['last_restart_at'] = now
        document['last_restart_pid'] = os.getpid()


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
        _apply_restart_stamp(document, now)
        return document

    try:
        update_json_atomic(
            _APPROVALS_FILE, _mut, default=None, strict=True)
    except (JsonStoreReadError, OSError, ValueError) as error:
        # Preserve the public helper's historical best-effort contract. The
        # compatibility mirror still provides a cooldown when possible.
        logger.warning('[Lifecycle] cooldown authority stamp failed: %s', error)
    _mirror_restart_stamp(now)


def rollback_restart_stamp(*, pid: int) -> bool:
    """Undo the acceptance stamp when NO restart bridge could execute.

    Called only from the pre-shutdown failure path: the manager refused and
    the in-place re-exec bridge was unavailable, so the accepting process is
    guaranteed to keep serving. Clearing the stamp lets the next
    human-approved retry be a real retry instead of a cooldown/``restartPending``
    refusal against a process that will never replace itself.

    The rollback is PID-guarded: it refuses to touch a stamp recorded by a
    different process, so a bridge failure AFTER a replacement was already
    committed elsewhere can never masquerade as a safe rollback. The consumed
    one-time token is deliberately NOT resurrected — the human approves once
    more on retry, which keeps the approval trail honest.
    """
    outcome: dict = {}

    def _mut(cur):
        document = _document(cur)
        stamped_pid = document.get('last_restart_pid')
        if stamped_pid is None or int(stamped_pid or 0) != int(pid):
            outcome['rolled_back'] = False
            return document
        document.pop('last_restart_at', None)
        document.pop('last_restart_pid', None)
        outcome['rolled_back'] = True
        return document

    try:
        update_json_atomic(
            _APPROVALS_FILE, _mut, default=None, strict=True)
    except (JsonStoreReadError, OSError, ValueError) as error:
        logger.warning('[Lifecycle] restart stamp rollback failed: %s', error)
        return False
    rolled_back = bool(outcome.get('rolled_back'))
    if rolled_back:
        _mirror_restart_rollback()
        audit_log('lifecycle_restart_stamp_rolled_back', pid=int(pid))
    return rolled_back


def _mirror_restart_rollback() -> None:
    """Best-effort legacy mirror clear; never the acceptance authority."""
    def _mut(cur):
        if cur is not None and not isinstance(cur, dict):
            raise JsonStoreReadError(
                'legacy lifecycle cooldown state has invalid shape')
        document = dict(cur or {})
        document.pop('last_restart_at', None)
        return document

    try:
        update_json_atomic(_STATE_FILE, _mut, default=None, strict=True)
    except (JsonStoreReadError, OSError, ValueError) as error:
        logger.warning('[Lifecycle] cooldown mirror rollback failed: %s', error)


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
                _apply_restart_stamp(document, now)
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


# ── operator passes recorded by serverctl (distinct, audited provenance) ──

def record_operator_pass(action: str, provenance: dict) -> dict:
    """Record a lifecycle action an operator path let through, before it runs.

    ``serverctl.py`` keeps two operator paths: the typed confirmation of an
    interactive terminal, and the handoff variable of a caller that already
    passed this gate (``restart_15000.sh``, the TofuDB cutover). Each pass is
    a consumed record with its own provenance kind, so a restart through them
    is never again invisible in the approval history.
    """
    if action not in ACTIONS:
        raise ValueError(f'unknown lifecycle action: {action!r}')
    kind = provenance.get('kind') if isinstance(provenance, dict) else None
    if kind not in (PROVENANCE_TERMINAL, PROVENANCE_ENVIRONMENT):
        raise ValueError('operator pass provenance must be interactive-terminal or environment-override')
    now = _now()
    rec = {
        'id': secrets.token_urlsafe(16), 'action': action, 'status': 'consumed',
        'requested_at': now, 'decided_at': now, 'consumed_at': now, 'expires_at': None,
        'decided_by': kind, 'origin': dict(provenance),
    }

    def _mut(cur):
        document = _document(cur)
        records = document['records']
        _sweep_expired(records, now)
        records.append(rec)
        document['records'] = _prune(records)
        return document

    update_json_atomic(_APPROVALS_FILE, _mut, default=None, strict=True)
    audit_log('lifecycle_operator_pass', approval_id=rec['id'], action=action, provenance=kind,
              **{key: provenance.get(key) for key in ('variable', 'pid', 'ppid', 'user', 'tty')
                 if provenance.get(key) is not None})
    return _public(rec)


# ── the owner's delegation to their Global Router (owner decision 2026-11-06) ──

_GRANTEE_FIELDS = frozenset({'kind', 'conversationId', 'bindingRevision', 'tenant'})
_REQUESTER_FIELDS = _GRANTEE_FIELDS | {'turnId', 'attemptId'}
_ACTION_STATUSES = ('accepted', 'restarting', 'completed', 'refused', 'failed')


def _owner_key(owner) -> str:
    from lib.identity import require_user_id
    return str(require_user_id(owner, context='owner delegation'))


def _revoked(owner: int) -> dict:
    return {'version': 1, 'owner': owner, 'state': 'revoked', 'revision': 0,
            'scopes': list(DELEGATION_SCOPES), 'grantee': None, 'grantedAt': None,
            'grantedBy': None, 'assurance': None, 'revokedAt': None, 'revokedBy': None,
            'updatedAt': None}


def _router_identity(value, fields: frozenset, what: str) -> dict:
    if (not isinstance(value, dict) or set(value) != fields or value['kind'] != 'global_router'
            or not isinstance(value['conversationId'], str) or not value['conversationId'].strip()
            or type(value['bindingRevision']) is not int or value['bindingRevision'] < 1
            or value['tenant'] is not None and not isinstance(value['tenant'], str)
            or any(value.get(name) is not None and not isinstance(value[name], str)
                   for name in ('turnId', 'attemptId'))):
        raise ValueError(f'The delegation {what} must be the owner\'s Global Router conversation')
    return dict(value)


def _delegation_of(document: dict, owner_key: str) -> dict | None:
    delegations = document.get('delegations') or {}
    if not isinstance(delegations, dict):
        raise JsonStoreReadError('lifecycle delegation store has invalid shape')
    record = delegations.get(owner_key)
    if record is not None and not isinstance(record, dict):
        raise JsonStoreReadError('lifecycle delegation record has invalid shape')
    return record


def _actions(document: dict) -> list:
    actions = document.setdefault('delegatedActions', [])
    if not isinstance(actions, list) or any(not isinstance(item, dict) for item in actions):
        raise JsonStoreReadError('lifecycle delegated action store has invalid shape')
    return actions


def _prune_actions(actions: list) -> list:
    """The approval records' retention bound; audit.log keeps every event."""
    if len(actions) <= _KEEP_RECORDS:
        return actions
    return sorted(actions, key=lambda item: item.get('requestedAt', 0), reverse=True)[:_KEEP_RECORDS]


def _check_grant(document: dict, owner_key: str, requester: dict, scope: str) -> dict:
    record = _delegation_of(document, owner_key)
    if not record or record.get('state') != 'granted':
        raise DelegationRefused(
            'owner_delegation_not_granted',
            'The owner has not granted the main coordinator their delegation. Ask the owner to turn on '
            '"Act for me" in the main coordinator\'s header in Tofu, or to do this themselves (a restart: '
            'Settings → Update). No approval request was created.')
    if scope not in (record.get('scopes') or ()):
        raise DelegationRefused('owner_delegation_scope', f'The owner\'s delegation does not cover {scope}')
    try:
        require_human_provenance(record.get('grantedBy'))
    except PermissionError as error:
        raise DelegationRefused('owner_delegation_not_granted',
                                'The delegation record carries no human grant; it authorizes nothing') from error
    grantee = record.get('grantee') or {}
    from lib.identity import tenant_scope
    if (grantee.get('conversationId') != requester['conversationId']
            or grantee.get('bindingRevision') != requester['bindingRevision']
            or tenant_scope(grantee.get('tenant')) != tenant_scope(requester['tenant'])):
        raise DelegationRefused(
            'owner_delegation_router_changed',
            'The owner granted their delegation to an earlier main coordinator; it does not carry over. '
            'Ask the owner to grant it again.')
    return record


def _read_document() -> dict:
    try:
        return _document(read_json(_APPROVALS_FILE, default=None, strict=True))
    except JsonStoreReadError as error:
        logger.warning('[Lifecycle] delegation store read failed: %s', error)
        raise DelegationRefused('owner_delegation_unavailable',
                                'The approval store could not be read; nothing ran') from error


def delegation(owner) -> dict:
    """The owner's delegation record; a revoked default when it was never granted."""
    key = _owner_key(owner)
    record = _delegation_of(_read_document(), key)
    return dict(record) if record else _revoked(int(key))


def set_delegation(owner, granted: bool, *, grantee: dict | None, provenance) -> dict:
    """Grant (``human-ui`` provenance only) or revoke the owner's Global Router delegation.

    A grant names its grantee, the owner's current Global Router conversation
    and binding revision, so it never carries over to a replaced router.
    Revocation needs only an authenticated owner request and is recorded with
    whatever provenance it had. Setting the current state again is a no-op.
    """
    key = _owner_key(owner)
    if granted:
        try:
            actor = require_human_provenance(provenance)
            if actor['ownerUserId'] != int(key):
                raise PermissionError('human_approval_required: only the owner can grant their delegation')
        except PermissionError:
            audit_log('owner_delegation_grant_refused', owner=int(key), provenance=str(provenance)[:300])
            raise
        grantee = _router_identity(grantee, _GRANTEE_FIELDS, 'grantee')
    else:
        if not isinstance(provenance, dict) or not isinstance(provenance.get('kind'), str):
            raise ValueError('Revocation records who revoked it')
        actor, grantee = dict(provenance), None
    now = _now()
    outcome: dict = {}

    def _mut(cur):
        document = _document(cur)
        current = _delegation_of(document, key) or _revoked(int(key))
        if (current['state'] == ('granted' if granted else 'revoked')
                and (not granted or current.get('grantee') == grantee)):
            outcome['record'], outcome['changed'] = dict(current), False
            return None
        record = {**current, 'state': 'granted' if granted else 'revoked',
                  'revision': int(current.get('revision') or 0) + 1,
                  'scopes': list(DELEGATION_SCOPES), 'updatedAt': now}
        if granted:
            record.update(grantee=grantee, grantedAt=now, grantedBy=actor,
                          assurance=provenance_assurance(actor), revokedAt=None, revokedBy=None)
        else:
            record.update(revokedAt=now, revokedBy=actor)
        document['delegations'] = {**(document.get('delegations') or {}), key: record}
        outcome['record'], outcome['changed'] = dict(record), True
        return document

    update_json_atomic(_APPROVALS_FILE, _mut, default=None, strict=True)
    record = outcome['record']
    if outcome['changed']:
        audit_log('owner_delegation_granted' if granted else 'owner_delegation_revoked',
                  owner=int(key), revision=record['revision'],
                  router=(record.get('grantee') or {}).get('conversationId') or '',
                  provenance=actor.get('kind'), credential=actor.get('credential') or '',
                  assurance=record.get('assurance') or '',
                  ua=actor.get('userAgent') or '', remote=actor.get('remoteAddr') or '')
        logger.warning('[Lifecycle] owner delegation %s (owner=%s, revision=%s)',
                       'GRANTED' if granted else 'REVOKED', key, record['revision'])
    return record


def check_delegation(owner, requester: dict, scope: str) -> dict:
    """The live grant covering ``scope`` for this Global Router, or DelegationRefused."""
    key = _owner_key(owner)
    requester = _router_identity(requester, _REQUESTER_FIELDS, 'requester')
    try:
        return dict(_check_grant(_read_document(), key, requester, scope))
    except DelegationRefused as refusal:
        audit_log('owner_delegation_refused', owner=int(key), scope=scope, code=refusal.code,
                  router=requester['conversationId'], turn=requester.get('turnId') or '')
        raise


def delegated_action_id(owner, requester: dict, scope: str, key: str) -> str:
    """One action per (owner, Global Router, scope, idempotency key)."""
    return 'od-' + hashlib.sha256(json.dumps(
        [int(_owner_key(owner)), requester['conversationId'], scope, key],
        ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()[:32]


def _observed(record: dict) -> dict:
    """A restart handed to the server manager reads as completed once a newer server answers.

    The manager reports no outcome back. Every accepted restart replaces the
    serving worker, so a process that booted after the handover proves the
    restart; until then the record stays ``restarting`` (handed over). A
    refusal inside the manager (logs/server-manager.log) leaves it there.
    """
    from lib import boot_identity
    accepted = record.get('acceptedAt')
    if (record.get('status') != 'restarting' or not _valid_timestamp(accepted)
            or boot_identity.BOOT_TS <= accepted):
        return record
    return {**record, 'status': 'completed',
            'detail': f'The server restarted: generation {boot_identity.BOOT_ID} booted after the handover.'}


def delegated_action(owner, action_id: str) -> dict | None:
    key = int(_owner_key(owner))
    for record in _actions(_read_document()):
        if record.get('id') == action_id and record.get('owner') == key:
            return _observed(dict(record))
    return None


def delegated_actions(owner) -> list:
    """The owner's retained delegated actions, newest first."""
    key = int(_owner_key(owner))
    records = [_observed(dict(record)) for record in _actions(_read_document()) if record.get('owner') == key]
    return sorted(records, key=lambda item: item.get('requestedAt', 0), reverse=True)


def _action_record(action_id, key, requester, scope, *, reason, trigger, target, grant, now, status,
                   code=None, detail=None) -> dict:
    return {'id': action_id, 'owner': int(key), 'scope': scope, 'requester': requester,
            'reason': reason, 'trigger': trigger, 'target': target,
            'grantRevision': grant['revision'] if grant else None,
            'grantedBy': (grant or {}).get('grantedBy'), 'requestedAt': now, 'status': status,
            'code': code, 'detail': detail, 'updatedAt': now}


def begin_delegated_action(owner, requester: dict, *, scope: str, key: str, reason: str,
                           trigger: dict, target: dict) -> tuple[dict, bool]:
    """Check the grant and record the accepted action in one transaction.

    Returns ``(record, replayed)``; a replay of the same key returns the
    original record and changes nothing, so retrying never acts twice.
    """
    owner_key = _owner_key(owner)
    requester = _router_identity(requester, _REQUESTER_FIELDS, 'requester')
    action_id = delegated_action_id(owner, requester, scope, key)
    now = _now()
    outcome: dict = {}

    def _mut(cur):
        document = _document(cur)
        actions = _actions(document)
        for record in actions:
            if record.get('id') == action_id:
                outcome.update(record=dict(record), replayed=True)
                return None
        try:
            grant = _check_grant(document, owner_key, requester, scope)
        except DelegationRefused as refusal:
            outcome['refusal'] = refusal
            return None
        record = _action_record(action_id, owner_key, requester, scope, reason=reason, trigger=trigger,
                                target=target, grant=grant, now=now, status='accepted')
        actions.append(record)
        document['delegatedActions'] = _prune_actions(actions)
        outcome.update(record=dict(record), replayed=False)
        return document

    update_json_atomic(_APPROVALS_FILE, _mut, default=None, strict=True)
    if 'refusal' in outcome:
        audit_log('owner_delegation_refused', owner=int(owner_key), scope=scope,
                  code=outcome['refusal'].code, router=requester['conversationId'],
                  turn=requester.get('turnId') or '')
        raise outcome['refusal']
    record = outcome['record']
    if not outcome['replayed']:
        audit_log('owner_delegation_action', action_id=action_id, owner=int(owner_key), scope=scope,
                  router=requester['conversationId'], turn=requester.get('turnId') or '',
                  trigger=(trigger or {}).get('kind') or '', target=json.dumps(target)[:300],
                  reason=reason[:300], grant_revision=record['grantRevision'])
    return record, outcome['replayed']


def record_delegated_refusal(owner, requester: dict, *, scope: str, reason: str, trigger: dict | None,
                             target: dict, code: str, detail: str) -> dict | None:
    """Record an attempt refused while a grant covered it; without one, audit.log only."""
    owner_key = _owner_key(owner)
    requester = _router_identity(requester, _REQUESTER_FIELDS, 'requester')
    now = _now()
    outcome: dict = {}

    def _mut(cur):
        document = _document(cur)
        try:
            grant = _check_grant(document, owner_key, requester, scope)
        except DelegationRefused:
            return None
        record = _action_record('od-' + secrets.token_hex(16), owner_key, requester, scope,
                                reason=reason, trigger=trigger, target=target, grant=grant, now=now,
                                status='refused', code=code, detail=detail[:2000])
        actions = _actions(document)
        actions.append(record)
        document['delegatedActions'] = _prune_actions(actions)
        outcome['record'] = dict(record)
        return document

    update_json_atomic(_APPROVALS_FILE, _mut, default=None, strict=True)
    audit_log('owner_delegation_refused', owner=int(owner_key), scope=scope, code=code,
              router=requester['conversationId'], turn=requester.get('turnId') or '',
              detail=detail[:300])
    return outcome.get('record')


def _update_action(owner, action_id: str, change) -> dict | None:
    key = int(_owner_key(owner))
    outcome: dict = {}

    def _mut(cur):
        document = _document(cur)
        for record in _actions(document):
            if record.get('id') == action_id and record.get('owner') == key:
                result = change(document, record)
                outcome['record'] = dict(record)
                return document if result is not False else None
        return None

    update_json_atomic(_APPROVALS_FILE, _mut, default=None, strict=True)
    return outcome.get('record')


def settle_delegated_action(owner, action_id: str, *, status: str, code: str | None = None,
                            detail: str | None = None) -> dict | None:
    """Record the outcome of an accepted delegated action."""
    if status not in _ACTION_STATUSES:
        raise ValueError(f'unknown delegated action status: {status!r}')
    now = _now()

    def change(_document, record):
        record.update(status=status, code=code, detail=(detail or None) and detail[:2000], updatedAt=now)

    record = _update_action(owner, action_id, change)
    audit_log('owner_delegation_action_settled', action_id=action_id, status=status, code=code or '',
              detail=(detail or '')[:300])
    return record


def accept_delegated_restart(owner, action_id: str, requester: dict, *,
                             interrupted: list | tuple = ()) -> dict:
    """The delegation branch of restart acceptance: the grant, cooldown and stamp commit together.

    Called after preparation succeeded and immediately before the old server
    is handed to its lifecycle owner. A grant revoked meanwhile, or another
    accepted restart inside the cooldown, refuses here. ``interrupted`` names
    the conversations the owner accepted interrupting. Returns the action
    record; ``status == 'restarting'`` is the only acceptance.
    """
    owner_key = _owner_key(owner)
    requester = _router_identity(requester, _REQUESTER_FIELDS, 'requester')
    legacy_last = _read_legacy_restart_stamp()
    now = _now()
    accepted: dict = {}

    def change(document, record):
        if record.get('status') != 'accepted' or record.get('scope') != 'restart':
            return False
        try:
            _check_grant(document, owner_key, requester, 'restart')
        except DelegationRefused as refusal:
            record.update(status='refused', code=refusal.code, detail=str(refusal), updatedAt=now)
            return True
        remaining = max(_cooldown_remaining(document.get('last_restart_at'), now),
                        _cooldown_remaining(legacy_last, now))
        if remaining > 0:
            record.update(status='refused', code='restart_cooldown',
                          detail=f'Another restart was accepted; retry in {remaining}s', updatedAt=now)
            return True
        _apply_restart_stamp(document, now)
        record.update(status='restarting', acceptedAt=now, updatedAt=now,
                      interrupted=[str(item) for item in interrupted])
        accepted['ok'] = True
        return True

    record = _update_action(owner, action_id, change)
    if record is None:
        raise DelegationRefused('owner_delegation_action_unknown', 'Unknown delegated restart')
    if accepted:
        _mirror_restart_stamp(now)
        audit_log('owner_delegation_restart_accepted', action_id=action_id, owner=int(owner_key),
                  router=requester['conversationId'], turn=requester.get('turnId') or '',
                  grant_revision=record.get('grantRevision'), cooldown_started=True,
                  interrupted=len(record.get('interrupted') or ()))
    else:
        audit_log('owner_delegation_restart_refused', action_id=action_id, owner=int(owner_key),
                  code=record.get('code') or '', status=record.get('status'))
    return record


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
    'perform_server_reexec',
    'restart_server',
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
    'DELEGATION_SCOPES', 'DelegationRefused', 'PROVENANCE_DELEGATION', 'PROVENANCE_ENVIRONMENT',
    'PROVENANCE_HUMAN_UI', 'PROVENANCE_TERMINAL',
    'create_request', 'decide', 'get', 'list_records', 'validate', 'consume',
    'restart_cooldown_remaining', 'restart_pending_on_this_process',
    'stamp_restart', 'rollback_restart_stamp', 'consume_restart',
    'detect_lifecycle_calls', 'consume_any', 'record_operator_pass', 'require_human_provenance',
    'provenance_assurance',
    'delegation', 'set_delegation', 'check_delegation', 'delegated_action_id', 'delegated_action',
    'delegated_actions', 'begin_delegated_action', 'record_delegated_refusal',
    'settle_delegated_action', 'accept_delegated_restart',
]
