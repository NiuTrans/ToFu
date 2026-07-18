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

from flask import Blueprint

import json
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
    from lib.database import DOMAIN_CHAT, async_fetchone
    from routes.common import DEFAULT_USER_ID, _db_safe  # noqa: F401
    row = await async_fetchone(
        'SELECT messages FROM conversations WHERE id=? AND user_id=?',
        (conv_id, DEFAULT_USER_ID), domain=DOMAIN_CHAT)
    if not row:
        return api_not_found('Conversation not found')
    try:
        messages = json.loads(row['messages'] or '[]')
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning('[api_v1.branches] conv=%s parse failed: %s',
                       conv_id[:8], e)
        return api_internal_error('Failed to parse conversation messages')
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

    from lib.database import DOMAIN_CHAT, async_execute, async_fetchone, json_dumps_pg
    from routes.common import DEFAULT_USER_ID

    row = await async_fetchone(
        'SELECT messages, title, settings, created_at '
        'FROM conversations WHERE id=? AND user_id=?',
        (conv_id, DEFAULT_USER_ID), domain=DOMAIN_CHAT)
    if not row:
        return api_not_found('Conversation not found')
    try:
        messages = json.loads(row['messages'] or '[]')
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning('[api_v1.branches] conv=%s parse failed: %s',
                       conv_id[:8], e)
        return api_internal_error('Failed to parse conversation messages')
    # ── Stable-id resolution: msgId (body) is authoritative, index the fallback.
    #    Drift-proof under windowed reads where the client's msg_idx is a tail
    #    window position, NOT the absolute index. ──
    _anchor_msg_id = optional_str(body, 'msg_id', default='', max_len=64) or ''
    if _anchor_msg_id:
        from lib.tasks_pkg.manager import find_message_by_id
        _ridx, _ = find_message_by_id(messages, _anchor_msg_id)
        if _ridx is not None:
            if _ridx != msg_idx:
                logger.info('[api_v1.branches] conv=%s msgId=%s resolved to index %d '
                            '(client sent %d — drift corrected)',
                            conv_id[:8], _anchor_msg_id[:12], _ridx, msg_idx)
            msg_idx = _ridx
    if msg_idx < 0 or msg_idx >= len(messages):
        return api_bad_request(f'msg_idx {msg_idx} out of range')
    msg = messages[msg_idx]
    if not isinstance(msg, dict):
        return api_bad_request(f'message at index {msg_idx} is not an object')

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

    branches = msg.get('branches')
    if not isinstance(branches, list):
        branches = []
        msg['branches'] = branches
    branches.append(branch)
    branch_idx = len(branches) - 1

    # Persist (mirrors the legacy delete_branch impl).
    now_ms = int(time.time() * 1000)
    try:
        settings = json.loads(row['settings'] or '{}')
    except (json.JSONDecodeError, TypeError) as _e_audit:
        logger.debug('[conversations] create_branch caught %s: %s', type(_e_audit).__name__, _e_audit)
        settings = {}
    settings_json = json.dumps(settings, ensure_ascii=False)
    messages_json = json_dumps_pg(messages)
    from routes.conversations import build_search_text  # noqa: E402
    search_text = build_search_text(messages)
    title_db = row['title']
    created_at = row['created_at'] if 'created_at' in row.keys() else now_ms

    try:
        # Build the dialect-correct upsert via Core (same backend-agnostic
        # path as the sync upsert() helper) and run it on the async executor.
        # 8-col insert (search_tsv omitted → PG trigger fills it); on conflict
        # update only messages/settings/updated_at/search_text (NOT title /
        # created_at — a branch must not rewrite those), matching the prior
        # hand-rolled ON CONFLICT clause.
        from lib.database._core_schema import CONVERSATIONS, upsert_sql
        _branch_sql = upsert_sql(
            CONVERSATIONS, conflict_cols=['id', 'user_id'],
            insert_cols=['id', 'user_id', 'title', 'messages', 'settings',
                         'created_at', 'updated_at', 'search_text'],
            update_cols=['messages', 'settings', 'updated_at', 'search_text'])
        await async_execute(
            _branch_sql,
            {'id': conv_id, 'user_id': DEFAULT_USER_ID, 'title': title_db,
             'messages': messages_json, 'settings': settings_json,
             'created_at': created_at, 'updated_at': now_ms,
             'search_text': search_text},
            domain=DOMAIN_CHAT)
    except Exception as e:
        logger.error('[api_v1.branches] persist failed conv=%s: %s',
                     conv_id[:8], e, exc_info=True)
        return api_internal_error(f'Failed to persist branch: {e}')

    # Event-driven cross-device sync: a new branch changes the conversation
    # body, so push the post-write rev → a sibling tab with this conv open
    # refetches without a manual refresh. notify_conv_changed also invalidates
    # the sidebar meta cache, so it replaces the bare _invalidate_meta_cache().
    try:
        from routes.common import _notify_conv_changed
        _rev_row = await async_fetchone(
            'SELECT rev FROM conversations WHERE id=? AND user_id=?',
            (conv_id, DEFAULT_USER_ID), domain=DOMAIN_CHAT)
        _branch_rev = None
        if _rev_row is not None:
            try:
                _branch_rev = _rev_row['rev']
            except (KeyError, TypeError, IndexError):
                _branch_rev = _rev_row[0]
        _notify_conv_changed(conv_id, rev=_branch_rev)
    except Exception as e:
        logger.debug('[api_v1.branches] conv-changed notify: %s', e)

    audit_log('branch_created', conv_id=conv_id, msg_idx=msg_idx,
              branch_idx=branch_idx, branch_id=branch['id'],
              kind=branch['kind'],
              key_id=(current_auth().key_id if current_auth() else ''))
    return api_ok(branch=branch, branch_idx=branch_idx,
                   total_branches=len(branches))


# DELETE /api/v1/conversations/<id>/messages/<i>/branches/<j> is registered
# by routes/conversations.py:delete_branch on this same blueprint.


@api_v1_conversations_bp.route(
    '/api/v1/conversations/<conv_id>/toolset/apply',
    methods=['POST'],
)
@require_scope('conversations')
@api_meta(
    summary='Apply a pending tool-toggle change to an active conversation',
    description=(
        'Clears the per-conversation tool-schema latch so the next chat '
        'round re-assembles the tool list from the CURRENT toggles. The '
        'latch normally freezes the tool array for a conversation\'s '
        'lifetime to keep the prompt cache prefix byte-identical; a '
        'mid-conversation toggle (Swarm/Scheduler/Browser/…) is otherwise '
        'deferred to the next NEW conversation. Call this when the user '
        'explicitly chooses "Apply now" — it accepts a one-time prompt-cache '
        'rebuild (~65k tokens) in exchange for the new tools taking effect '
        'immediately.\n\nResponse: ``{ok: true, conv_id}``.'),
    tags=['conversations'], scope='conversations',
)
async def apply_toolset(conv_id):
    if not conv_id:
        return api_bad_request('conv_id is required', field='conv_id')
    try:
        from lib.tools import clear_tool_list_latch
        clear_tool_list_latch(conv_id)
    except Exception as e:
        logger.error('[api_v1.conv] toolset apply failed conv=%s: %s',
                     conv_id[:8], e, exc_info=True)
        return api_internal_error(f'Failed to apply toolset change: {e}')
    audit_log('toolset_apply', conv_id=conv_id,
              key_id=(current_auth().key_id if current_auth() else ''))
    logger.info('[api_v1.conv] toolset latch cleared (Apply now) conv=%s',
                conv_id[:8])
    return api_ok(conv_id=conv_id)


__all__ = ['api_v1_conversations_bp']
