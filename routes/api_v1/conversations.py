"""routes/api_v1/conversations.py — Conversation CRUD over the v1 surface.

Most of the heavy lifting (loading messages from DB, search, branches,
compaction) lives in ``routes/conversations.py`` and its sibling
modules. This blueprint just exposes a stable, scope-gated surface.

Note: the existing routes (``/api/conversations/...``) remain intact for
the UI. New headless callers should use ``/api/v1/conversations/...``.
For now we proxy to the legacy implementations to avoid duplicating
the logic — the migration plan calls for those legacy routes to move
their primary registration here once the JS leak audit is complete.
"""

from __future__ import annotations

from quart import Blueprint

import asyncio
import secrets
import time

from lib.api_response import api_bad_request, api_internal_error, api_not_found, api_ok
from lib.branch_meta import classify_branch_title
from lib.conv_config import resolve_conv_config, resolve_conv_settings
from lib.log import audit_log, get_logger
from lib.openapi import api_meta
from lib.request_parser import BadRequest, async_parse_body, optional_dict, optional_str, require_str

from .auth import current_auth, require_scope

logger = get_logger(__name__)

api_v1_conversations_bp = Blueprint('api_v1_conversations', __name__)


def _load_legacy_module():
    try:
        return __import__('routes.conversations', fromlist=['*'])
    except Exception as e:
        logger.warning('[api_v1.conv] legacy module load failed: %s', e)
        return None


# list / get / delete / search / debug-messages / export / PUT / settings PATCH /
# message DELETE / message PATCH / by-id PATCH / DELETE branch are now
# registered DIRECTLY by routes/conversations.py (and its sibling
# conversations_search/conversations_compaction modules) on this same
# blueprint via the alias `from routes.api_v1 import api_v1_conversations_bp
# as conversations_bp` in routes/conversations.py. The proxy stubs that
# used to live here were deleted on 2026-05-29 once the legacy module's
# routes were re-pointed at /api/v1/*.


@api_v1_conversations_bp.route('/api/v1/conversations/config/resolve',
                                methods=['POST'])
@require_scope('conversations')
@api_meta(
    summary='Resolve a runtime config dict for chat task endpoints',
    description=(
        'Pure-function merge of per-conversation stored settings + '
        'session overrides + server defaults. Returns the canonical '
        '32-field config that goes to `/api/chat/start`, '
        '`/api/chat/regenerate`, `/api/chat/continue`, etc.\n\n'
        'Mirrors the JS `_buildConvConfig` exactly. Centralised so '
        'SDK callers, the UI, and CI scripts all see the same '
        'merge policy. Adding a config field means editing '
        '`lib/conv_config.py` once instead of two JS functions + 8 '
        'callsites.'),
    tags=['conversations'], scope='conversations',
    request_body={'required': True, 'content': {'application/json': {
        'schema': {
            'type': 'object',
            'properties': {
                'conv_settings': {'type': 'object'},
                'overrides': {'type': 'object'},
                'server_defaults': {'type': 'object'},
                'is_active': {'type': 'boolean', 'default': True},
            },
        },
    }}},
)
async def resolve_config_route():
    body = await async_parse_body()
    conv_settings = optional_dict(body, 'conv_settings', default={}) or {}
    overrides = optional_dict(body, 'overrides', default={}) or {}
    server_defaults = optional_dict(body, 'server_defaults', default={}) or {}
    is_active = bool(body.get('is_active', True))
    return api_ok(resolve_conv_config(
        conv_settings=conv_settings,
        overrides=overrides,
        server_defaults=server_defaults,
        is_active=is_active,
    ))


@api_v1_conversations_bp.route('/api/v1/conversations/settings/resolve',
                                methods=['POST'])
@require_scope('conversations')
@api_meta(
    summary='Resolve a per-conversation settings dict for persistence',
    description=(
        'Pure-function port of the JS `_buildConvSettings`. Returns '
        'the 19-field settings payload that goes to PUT '
        '`/api/conversations/{id}/settings` and the chat-send body. '
        'Used by the UI before every chat-action POST and by SDK '
        'callers building branch / regenerate requests headlessly.'),
    tags=['conversations'], scope='conversations',
    request_body={'required': True, 'content': {'application/json': {
        'schema': {
            'type': 'object',
            'properties': {
                'conv_settings': {'type': 'object'},
                'overrides': {'type': 'object'},
            },
        },
    }}},
)
async def resolve_settings_route():
    body = await async_parse_body()
    conv_settings = optional_dict(body, 'conv_settings', default={}) or {}
    overrides = optional_dict(body, 'overrides', default={}) or {}
    return api_ok(resolve_conv_settings(
        conv_settings=conv_settings,
        overrides=overrides,
    ))


@api_v1_conversations_bp.route('/api/v1/conversations/branches/classify',
                                methods=['POST'])
@require_scope('conversations')
@api_meta(
    summary='Classify a branch title — returns icon + semantic kind',
    description=(
        'Pure-function policy lookup that the UI uses to assign an '
        'auto-icon and category to a freshly-created branch. Exposed '
        'so SDK callers (CI scripts auto-creating branches, evaluation '
        'harnesses, the future `POST /api/v1/conversations/{id}/branches` '
        'endpoint) get the same classification the UI shows.\n\n'
        'Response: ``{ok, icon, kind}`` where `kind` is one of '
        '`paper / code / data / math / image / compare / bug / todo / '
        'idea / summary / generic`.'),
    tags=['conversations'], scope='conversations',
    request_body={'required': True, 'content': {'application/json': {
        'schema': {
            'type': 'object',
            'required': ['title'],
            'properties': {'title': {'type': 'string'}},
        },
    }}},
)
async def classify_branch():
    body = await async_parse_body()
    try:
        title = require_str(body, 'title', max_len=200, allow_empty=True)
    except BadRequest as e:
        return api_bad_request(str(e), field=e.field or 'title')
    return api_ok(classify_branch_title(title))


# ── Branch tree mutations ─────────────────────────────────────────────
#
# Server-authoritative branch CRUD. The legacy
# ``/api/conversations/{id}/messages/{i}/branches/{j}`` (DELETE) endpoint
# stays for the UI; new headless callers use the v1 routes below which
# layer scope-gating + structured responses on top of the same logic.

_BRANCH_SCOPE = 'conversations'


def _load_branches_module():
    """Lazy-import the legacy module so we share its DB helpers."""
    try:
        return __import__('routes.conversations', fromlist=['*'])
    except Exception as e:
        logger.warning('[api_v1.branches] legacy load failed: %s', e)
        return None


def _branch_persist_payload(messages):
    """Serialize the full messages blob for persist + build its search text.

    Pure CPU over the whole conversation — run via ``asyncio.to_thread``.
    Returns ``(messages_json, search_text)``.
    """
    from lib.database import json_dumps_pg
    from routes.conversations import build_search_text
    return json_dumps_pg(messages), build_search_text(messages)


def _generate_branch_id() -> str:
    """Stable, URL-safe branch id. Same shape the JS used to mint
    locally — base36 timestamp + random suffix — but server-generated
    so two clients can't race-collide ids on the same message."""
    ts = format(int(time.time() * 1000), 'x')
    return ts + secrets.token_hex(2)


@api_v1_conversations_bp.route(
    '/api/v1/conversations/<conv_id>/messages/<int:msg_idx>/branches',
    methods=['GET'],
)
@require_scope(_BRANCH_SCOPE)
@api_meta(
    summary='List branches under a message',
    tags=['conversations'], scope=_BRANCH_SCOPE,
)
async def list_branches(conv_id, msg_idx):
    legacy = _load_branches_module()
    if legacy is None:
        return api_internal_error('Branches module unavailable')
    from routes.common import DEFAULT_USER_ID, _db_safe  # noqa: F401
    def _load():
        from lib.database import DOMAIN_CHAT, get_thread_db
        from lib.database.conversation_repository import load_conversation
        return load_conversation(
            get_thread_db(DOMAIN_CHAT), conv_id, user_id=DEFAULT_USER_ID)
    snapshot = await asyncio.to_thread(_load)
    if snapshot is None:
        return api_not_found('Conversation not found')
    messages = snapshot.messages
    # Stable-id resolution (query msgId authoritative, index fallback) so a
    # windowed-read client lists the correct message's branches.
    from quart import request as _request
    _anchor_msg_id = _request.args.get('msgId') or None
    if _anchor_msg_id:
        from lib.tasks_pkg.manager import find_message_by_id
        _ridx, _ = find_message_by_id(messages, _anchor_msg_id)
        if _ridx is not None:
            msg_idx = _ridx
    if msg_idx < 0 or msg_idx >= len(messages):
        return api_bad_request(f'msg_idx {msg_idx} out of range')
    msg = messages[msg_idx]
    branches = (msg.get('branches')
                if isinstance(msg, dict) else None) or []
    return api_ok(branches=branches, count=len(branches))


@api_v1_conversations_bp.route(
    '/api/v1/conversations/<conv_id>/messages/<int:msg_idx>/branches',
    methods=['POST'],
)
@require_scope(_BRANCH_SCOPE)
@api_meta(
    summary='Create a branch under a message',
    description=(
        'Server generates the branch ID, classifies the title (icon + '
        'kind via `lib/branch_meta.py`), validates `msg_idx`, persists '
        'to DB, and returns the new branch dict + its position.\n\n'
        'Replaces the JS pattern of locally minting an ID, pushing to '
        '`msg.branches`, then PUT-syncing the whole conversation. Two '
        'clients can no longer race-collide on branch IDs.'),
    tags=['conversations'], scope=_BRANCH_SCOPE,
    request_body={'required': True, 'content': {'application/json': {
        'schema': {
            'type': 'object',
            'required': ['title'],
            'properties': {
                'title': {'type': 'string'},
                'anchor_text': {'type': 'string'},
                'parent_selection': {'type': 'string'},
            },
        },
    }}},
)
async def create_branch(conv_id, msg_idx):
    body = await async_parse_body()
    try:
        title = require_str(body, 'title', max_len=200).strip()
    except BadRequest as e:
        return api_bad_request(str(e), field=e.field or 'title')
    if not title:
        return api_bad_request('title is empty', field='title')
    anchor_text = optional_str(body, 'anchor_text',
                                 default='', max_len=200) or ''
    parent_selection = optional_str(body, 'parent_selection',
                                      default='', max_len=4000) or ''

    from routes.common import DEFAULT_USER_ID
    _anchor_msg_id = optional_str(body, 'msg_id', default='', max_len=64) or ''

    classified = classify_branch_title(title)
    branch = {
        'id': _generate_branch_id(),
        'title': title,
        'icon': classified.get('icon', '') or '',
        'kind': classified.get('kind', 'generic'),
        'messages': [],
    }
    if anchor_text:
        branch['anchorText'] = anchor_text
    if parent_selection:
        branch['parentSelection'] = parent_selection

    def _persist_branch():
        import copy
        from lib.database import DOMAIN_CHAT, get_thread_db
        from lib.database.conversation_repository import (
            ConversationMutation,
            mutate_conversation,
        )

        def _mutate(messages, _snapshot):
            resolved_idx = msg_idx
            if _anchor_msg_id:
                from lib.tasks_pkg.manager import find_message_by_id
                found, _ = find_message_by_id(messages, _anchor_msg_id)
                if found is not None:
                    resolved_idx = found
            if resolved_idx < 0 or resolved_idx >= len(messages):
                return ConversationMutation(
                    changed=False,
                    value={'error': 'out_of_range', 'index': resolved_idx})
            msg = messages[resolved_idx]
            if not isinstance(msg, dict):
                return ConversationMutation(
                    changed=False,
                    value={'error': 'not_object', 'index': resolved_idx})
            branches = msg.get('branches')
            if not isinstance(branches, list):
                branches = []
                msg['branches'] = branches
            branches.append(copy.deepcopy(branch))
            return ConversationMutation(
                value={'index': resolved_idx,
                       'branch_idx': len(branches) - 1,
                       'total': len(branches)},
                changed_seqs=[resolved_idx])

        return mutate_conversation(
            get_thread_db(DOMAIN_CHAT), conv_id, _mutate,
            user_id=DEFAULT_USER_ID, max_attempts=5)

    try:
        mutation = await asyncio.to_thread(_persist_branch)
    except Exception as e:
        logger.error('[api_v1.branches] persist failed conv=%s: %s',
                     conv_id[:8], e, exc_info=True)
        return api_internal_error(f'Failed to persist branch: {e}')
    if mutation.missing:
        return api_not_found('Conversation not found')
    if not mutation.applied:
        error = (mutation.value or {}).get('error') if mutation.value else None
        if error == 'out_of_range':
            return api_bad_request(
                f'msg_idx {(mutation.value or {}).get("index")} out of range')
        if error == 'not_object':
            return api_bad_request('target message is not an object')
        return api_internal_error('Conversation remained busy; retry')
    resolved = mutation.value
    branch_idx = resolved['branch_idx']
    msg_idx = resolved['index']

    # Event-driven cross-device sync: a new branch changes the conversation
    # body, so push the post-write rev → a sibling tab with this conv open
    # refetches without a manual refresh. notify_conv_changed also invalidates
    # the sidebar meta cache, so it replaces the bare _invalidate_meta_cache().
    try:
        from routes.common import _notify_conv_changed, _request_user_id
        _notify_conv_changed(
            conv_id, rev=mutation.rev, user_id=_request_user_id())
    except Exception as e:
        logger.debug('[api_v1.branches] conv-changed notify: %s', e)

    audit_log('branch_created', conv_id=conv_id, msg_idx=msg_idx,
              branch_idx=branch_idx, branch_id=branch['id'],
              kind=branch['kind'],
              key_id=(current_auth().key_id if current_auth() else ''))
    return api_ok(branch=branch, branch_idx=branch_idx,
                   total_branches=resolved['total'])


# DELETE /api/v1/conversations/<id>/messages/<i>/branches/<j> is registered
# by routes/conversations.py:delete_branch on this same blueprint.


# ── pt_conv_state_ssot P5: sync-drift probe ────────────────────────────
#
# The client reports a compact digest of what IT believes (per conv: the
# authoritative busy set + the rev it last converged to); the server compares
# it against the two server-side SSOTs — the in-memory task registry and the
# conversations.rev column — and WARN-logs + returns every divergence.
# Owner constraint #4: the digest covers BOTH activeTaskIds AND conv rev; the
# rev half closes the "notify frame dropped, _serverRev never converges" hole.
# Probe only: this endpoint NEVER mutates either side's state.

_SYNC_DIGEST_MAX = 500


def _log_divergence(conv_id: str, kind: str, client, server,
                    observer_id: str = '') -> dict | None:
    """Log a divergence at a severity that reflects whether it is a FAULT.

    P5 logged every inequality at WARNING. On a streaming conversation the
    client's 60s-old digest can never match a live server read, so the warning
    fired constantly and buried the real signal. The tracker distinguishes a
    client that is merely sampling late (moving) from one that remains frozen
    at an unequal value — only the latter is the "notify frame dropped, never
    converges" hole.  The server may already be static by the first probe (for
    example after a lost terminal frame), so repeated server movement is not a
    prerequisite.

    Returns the tracker's verdict dict (or None when the tracker itself
    failed) so the caller can act on a SUSTAINED stall instead of only
    logging it — the observation/repair split of pt_cadaa70ffa6b468d. Logging
    remains the only side effect HERE; this function still never mutates
    either side's state.
    """
    try:
        from lib.conversations.drift_tracker import observe_divergence
        v = observe_divergence(
            conv_id, kind, client, server, observer_id=observer_id)
    except Exception as e:
        # Never let the tracker suppress the underlying signal.
        logger.debug('[SyncDrift] tracker failed conv=%s kind=%s: %s',
                     conv_id[:8], kind, e)
        logger.warning('[SyncDrift] conv=%s kind=%s client=%s server=%s',
                       conv_id[:8], kind, client, server)
        return None

    if v['severity'] == 'warning':
        logger.warning(
            '[SyncDrift] STALLED conv=%s kind=%s client=%s server=%s '
            'frozen_age=%.0fs observations=%d direction=%s — client value has NOT '
            'moved while the values remained unequal; this conversation is not '
            'converging on its own',
            conv_id[:8], kind, client, server, v['frozen_age'], v['observations'],
            v['direction'])
    else:
        logger.debug(
            '[SyncDrift] conv=%s kind=%s client=%s server=%s age=%.0fs '
            'observations=%d stalled=%s direction=%s',
            conv_id[:8], kind, client, server, v['age'], v['observations'],
            v['stalled'], v['direction'])
    return v


def _log_agreement(conv_id: str, kind: str, observer_id: str = '') -> None:
    """Record that a previously-diverged pair converged.

    This is the positive evidence P6 needs: proof the channel self-heals
    without the fallback branch. Silent in steady state — only a divergence
    that actually closes produces a line.
    """
    try:
        from lib.conversations.drift_tracker import observe_agreement
        res = observe_agreement(conv_id, kind, observer_id=observer_id)
    except Exception as e:
        logger.debug('[SyncDrift] agreement record failed conv=%s: %s',
                     conv_id[:8], e)
        return
    if res is None:
        return
    logger.info(
        '[SyncDrift] CONVERGED conv=%s kind=%s after %.0fs '
        '(%d diverged observations, was_stalled=%s)',
        conv_id[:8], kind, res['age'], res['observations'], res['was_stalled'])


@api_v1_conversations_bp.route('/api/v1/conversations/sync-digest',
                                methods=['POST'])
@require_scope('conversations')
@api_meta(
    summary='Compare client conv-state digests against the server SSOTs',
    description=(
        'pt_conv_state_ssot P5 (drift probe). Body: ``{probeId, digests: [{convId, '
        'taskIds: [...], rev: <number|null>}]}``. For each entry the server '
        'compares the client busy set against the task-registry snapshot and '
        'the client rev against ``conversations.rev``; divergences are '
        'WARN-logged and returned. Read-only probe — no state is mutated.\n\n'
        'Optional top-level ``identityGateDegraded: true`` reports that the '
        'client\'s multi-user identity gate fell back to accept-all (a JS '
        'build-order regression); it is WARN-logged and may arrive with an '
        'empty ``digests`` list.'),
    tags=['conversations'], scope='conversations',
    request_body={'required': True, 'content': {'application/json': {
        'schema': {
            'type': 'object',
            'required': ['digests'],
            'properties': {
                'identityGateDegraded': {'type': 'boolean'},
                'probeId': {'type': 'string'},
                'pushRid': {'type': 'string'},
                'digests': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'convId': {'type': 'string'},
                            'taskIds': {'type': 'array',
                                        'items': {'type': 'string'}},
                            'rev': {'type': ['number', 'null']},
                        },
                    },
                },
            },
        },
    }}},
)
async def sync_digest():
    body = await async_parse_body()

    # A drift belongs to the reporting PAGE, not globally to a conversation.
    # Otherwise an up-to-date sibling tab clears the stale tab's evidence on
    # every alternating probe, making the sustained threshold unreachable.
    # New clients send the page-stable probeId.  pushRid keeps old clients
    # isolated too; the empty legacy bucket preserves wire compatibility.
    _raw_observer = body.get('probeId') or body.get('pushRid') or ''
    observer_id = (_raw_observer[:64]
                   if isinstance(_raw_observer, str) else '')

    # ── Identity-gate fail-open telemetry ──────────────────────────────
    # The client's multi-user identity gate (conv_state_reducer::_frameIsOurs)
    # fails OPEN when the predicate is missing — a build-order regression makes
    # every notify frame accepted UNSCOPED. That degrade is security-relevant
    # and was previously visible ONLY in a browser console. It rides this
    # existing probe so it lands in logs/app.log next to every other drift
    # signal. Read BEFORE the digests validation: a broken-bundle page can
    # legitimately have zero digests, and rejecting the body first would
    # suppress the signal on exactly the page it exists to catch.
    if body.get('identityGateDegraded'):
        _auth_ig = current_auth()
        logger.warning(
            '[SyncDrift] IDENTITY GATE DEGRADED — client reports '
            'window._frameIsOurs was unavailable, so notify frames are being '
            'accepted UNSCOPED (multi-user isolation is off on that page). '
            'Cause is a JS build-order regression: core/conv_state_reducer.js '
            'must initialize before its consumers in the Vite module graph. '
            'key_id=%s user_id=%s digests=%s',
            (_auth_ig.key_id if _auth_ig else ''),
            (getattr(_auth_ig, 'user_id', '') if _auth_ig else ''),
            (len(body.get('digests')) if isinstance(body.get('digests'), list)
             else 0))

    digests = body.get('digests')
    if not isinstance(digests, list):
        return api_bad_request('digests must be a list', field='digests')
    if len(digests) > _SYNC_DIGEST_MAX:
        return api_bad_request(
            f'too many digests (max {_SYNC_DIGEST_MAX})', field='digests')

    # Registry snapshot — the ONLY physical SSOT for "who is running".
    # Scope by the caller's tenant when auth carries a real user_id;
    # single-user default stays unscoped (byte-identical to P1's notify).
    #
    # ``_scope`` is resolved OUTSIDE the try: the repair block at the end of
    # this function needs it to build a correctly-scoped snapshot, and a
    # failure of the registry import must not leave it unbound (that would
    # turn "registry unavailable" into a silent loss of the repair path — an
    # exception swallowed by the repair block's own guard, i.e. exactly the
    # kind of invisible degrade this whole workstream is about).
    _auth = current_auth()
    _uid = getattr(_auth, 'user_id', None) if _auth else None
    _scope = '' if _uid in (None, '', 1, '1') else str(_uid)
    try:
        from lib.tasks_pkg.manager._registry import snapshot_running_by_conv
        snap = snapshot_running_by_conv(user_id=_scope)
    except Exception as e:
        logger.warning('[SyncDrift] registry snapshot failed: %s', e)
        snap = {}

    from lib.database import DOMAIN_CHAT, async_fetchall
    from routes.common import DEFAULT_USER_ID

    # Resolve every idle numeric-rev digest in ONE indexed query. The former
    # per-item ``async_fetchone`` loop allowed a valid 500-item probe to create
    # 500 serial database round trips. On a FUSE-backed local PostgreSQL data
    # directory even a small storage hiccup then stretched a tiny sync request
    # into seconds. Busy conversations deliberately remain excluded because
    # their client rev is frozen by design until the stream settles.
    rev_conv_ids = []
    rev_conv_seen = set()
    for d in digests:
        if not isinstance(d, dict):
            continue
        conv_id = str(d.get('convId') or '')[:64]
        client_rev = d.get('rev')
        if (not conv_id or conv_id in rev_conv_seen
                or snap.get(conv_id)
                or not isinstance(client_rev, (int, float))
                or isinstance(client_rev, bool)):
            continue
        rev_conv_seen.add(conv_id)
        rev_conv_ids.append(conv_id)

    server_revs = {}
    if rev_conv_ids:
        placeholders = ','.join('?' for _ in rev_conv_ids)
        rows = await async_fetchall(
            'SELECT id, rev FROM conversations '
            f'WHERE user_id=? AND id IN ({placeholders})',
            (DEFAULT_USER_ID, *rev_conv_ids), domain=DOMAIN_CHAT)
        for row in rows:
            try:
                row_id, row_rev = row['id'], row['rev']
            except (KeyError, TypeError, IndexError) as e:
                logger.debug('[api_v1.conv] positional sync-digest row '
                             'fallback: %s', e)
                row_id, row_rev = row[0], row[1]
            server_revs[str(row_id)] = row_rev

    divergences = []
    verdicts = []
    checked = 0
    for d in digests:
        if not isinstance(d, dict):
            continue
        conv_id = str(d.get('convId') or '')[:64]
        if not conv_id:
            continue
        checked += 1

        client_tids = d.get('taskIds')
        client_tids = sorted({str(t) for t in client_tids
                              if t}) if isinstance(client_tids, list) else []
        server_tids = sorted(snap.get(conv_id, []))
        if client_tids != server_tids:
            divergences.append({'convId': conv_id, 'kind': 'task_ids',
                                'client': client_tids, 'server': server_tids})
            _v = _log_divergence(
                conv_id, 'task_ids', client_tids, server_tids, observer_id)
            if _v:
                _v.update(convId=conv_id, kind='task_ids')
                verdicts.append(_v)
        else:
            _log_agreement(conv_id, 'task_ids', observer_id)

        client_rev = d.get('rev')
        if isinstance(client_rev, (int, float)) and not isinstance(client_rev, bool):
            if server_tids:
                # Busy-lag is BY DESIGN (pt_a182d5bd): the client does not
                # advance _serverRev mid-stream — the live SSE owns the conv
                # and the sync PUT at stream end is what converges rev. A
                # frozen client rev against a checkpoint-climbing server rev
                # is exactly what a HEALTHY generating conversation looks
                # like (measured ~716 STALLED/day 2026-07-26, all on convs
                # with running tasks — not dropped notify frames). The
                # task_ids comparison above already covers the busy channel;
                # the rev dimension only carries signal while the conv is
                # IDLE. Skipping also spares the per-digest SELECT.
                logger.debug(
                    '[SyncDrift] conv=%s kind=rev compare skipped while busy '
                    '(client=%s, %d running task(s))',
                    conv_id[:8], client_rev, len(server_tids))
                continue
            if conv_id not in server_revs:
                divergences.append({'convId': conv_id, 'kind': 'unknown_conv',
                                    'client': client_rev, 'server': None})
                logger.warning('[SyncDrift] conv=%s kind=unknown_conv client_rev=%s',
                               conv_id[:8], client_rev)
                continue
            server_rev = server_revs[conv_id]
            if server_rev != client_rev:
                divergences.append({'convId': conv_id, 'kind': 'rev',
                                    'client': client_rev, 'server': server_rev})
                _v = _log_divergence(
                    conv_id, 'rev', client_rev, server_rev, observer_id)
                if _v:
                    _v.update(convId=conv_id, kind='rev')
                    verdicts.append(_v)
            else:
                _log_agreement(conv_id, 'rev', observer_id)

    # ── REPAIR: a detected stall must produce a correction, not just a log ──
    # pt_cadaa70ffa6b468d. Everything above is observation; without this block
    # the server knew exactly which socket was frozen and left the user to
    # discover it by pressing F5.
    #
    # The frame sent is the ORDINARY conv_state_snapshot — the same one a fresh
    # connection receives, built from the same registry projection, carrying
    # the same server-minted frame-level rev. Reusing it (rather than inventing
    # a "repair" frame type) means the client needs NO new handling: the reducer
    # already applies it, already rev-gates each conv, and already treats an
    # absent conv as CLEAR. A repair is therefore indistinguishable from a
    # reconnect, which is exactly the property that makes it safe to send.
    #
    # Scoped to the reporting tenant so a repair can never leak sibling tasks,
    # and delivered to THAT socket alone (see PushHub.deliver_to_socket).
    # Best-effort throughout: a failed repair must never turn the probe — a
    # read-only diagnostic — into a 500.
    # ── REPAIR: a detected stall must produce a correction, not just a log ──
    # pt_cadaa70ffa6b468d / pt_b8dcd3b96f684296. Everything above is
    # observation; without this block the server knew exactly which client was
    # frozen and left the user to discover it by pressing F5.
    #
    # ★ THE CORRECTION RIDES THE HTTP RESPONSE, NOT THE PUSH SOCKET.
    # The first version pushed it down the socket, which is circular: a client
    # is judged stalled largely BECAUSE notify frames stopped arriving, so the
    # socket is the very thing that is broken. Measured, three ways:
    #
    #   * HALF-OPEN SOCKET — the client stays registered and ``send`` never
    #     throws; the queue just fills (bounded at 1000) and the peer gets
    #     nothing. ``deliver_to_socket`` still returns True, which the caller
    #     read as DELIVERED and used to arm a 300s cooldown. The client most in
    #     need of repair got a false success plus five minutes of silence.
    #   * NO SOCKET AT ALL — WebSocket blocked by a corporate proxy/tunnel:
    #     ``deliver_to_socket`` returns False forever, so that population was
    #     permanently unrepairable — and it is the population with no push
    #     channel to self-heal through, i.e. the one that needs this most.
    #   * HTTP IS PROVEN ALIVE — this stall was detected FROM a digest POST
    #     that is, right now, returning 200. The detection channel has just
    #     demonstrated it works, so it is the honest channel for the answer.
    #
    # Rule: the correction goes back on the channel the detection arrived on.
    #
    # The payload is the ORDINARY conv_state_snapshot — same projection, same
    # server-minted frame-level rev — so the client applies it with the reducer
    # it already has and a repair stays indistinguishable from a reconnect.
    # Scoped to the reporting tenant so it can never leak sibling tasks.
    #
    # Best-effort: a failed repair must never turn this read-only diagnostic
    # into a 500.
    # A rev inequality needs a BODY refresh, not a running-task snapshot.
    # Return this directive immediately on the first observation: the digest
    # POST itself proves HTTP is alive, active tabs can verify non-destructively,
    # and background tabs merely mark their shell stale for the next open.
    reload_conv_ids = sorted({
        d['convId'] for d in divergences if d.get('kind') == 'rev'
    })

    repaired = False
    snapshot = None
    try:
        _raw_socket_id = body.get('pushRid') or ''
        socket_id = (_raw_socket_id[:64]
                     if isinstance(_raw_socket_id, str) else '')
        repair_client_id = socket_id or observer_id
        from lib.conversations.drift_repair import (note_repair_attempt,
                                                    note_repair_outcome,
                                                    should_repair)

        # ── Effectiveness feedback (closes the loop) ──
        # A repair whose outcome is never checked is indistinguishable from the
        # mechanism silently not firing. This probe IS the observation: if the
        # socket we repaired last round now reports no sustained divergence, the
        # correction landed. Evaluated BEFORE this round's decision so a fresh
        # attempt does not overwrite the pending verdict.
        if repair_client_id:
            _still_stalled = any(v.get('sustained') for v in verdicts)
            note_repair_outcome(
                repair_client_id, converged=not _still_stalled)

        # ``should_repair`` is keyed on the socket id only as an identity for
        # rate-limiting; it does NOT require that socket to be live, because the
        # correction no longer travels that way.
        if should_repair(repair_client_id, verdicts):
            from lib.agent_core.push import build_conv_state_snapshot, hub
            snapshot = build_conv_state_snapshot(user_id=_scope)
            # Delivery is the HTTP response itself: returning 200 with this body
            # IS the delivery, so the cooldown may be armed honestly here.
            note_repair_attempt(repair_client_id)
            repaired = True
            logger.warning(
                '[SyncDrift] REPAIR returned in-band to socket=%s (%d sustained '
                'divergence(s)) — corrective conv_state_snapshot covering %d '
                'running conv(s)',
                repair_client_id[:8],
                sum(1 for v in verdicts if v.get('sustained')),
                len(snapshot.get('convs') or {}))
            # OPTIONAL ACCELERATOR, never the delivery proof: if the socket
            # does happen to be live and healthy, the same frame arriving over
            # push lands a beat sooner. Its return value is deliberately
            # IGNORED — treating an enqueue as delivery is the bug above.
            try:
                if socket_id:
                    hub.deliver_to_socket(socket_id, snapshot)
            except Exception as _pe:
                logger.debug('[SyncDrift] optional push accelerator failed '
                             '(the in-band copy is authoritative): %s', _pe)
    except Exception as e:
        logger.warning('[SyncDrift] repair attempt failed: %s', e)

    return api_ok(checked=checked, divergences=divergences,
                  repaired=repaired, snapshot=snapshot,
                  reloadConvIds=reload_conv_ids)


__all__ = ['api_v1_conversations_bp']
