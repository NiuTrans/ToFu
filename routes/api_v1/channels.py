"""routes/api_v1/channels.py — channel accounts, threads and outbox (docs/CHANNELS.md).

Routes (projected onto ``/api/v4`` with the v4 envelope):
  GET  /api/v1/channels/dx                        — 大象 account config and status
  PUT  /api/v1/channels/dx                        — enable/disable, login MIS, choose command chat
  POST /api/v1/channels/dx/command-chat           — create the single-member 'Tofu Router' group
  GET  /api/v1/channels/dx/threads                — 大象 thread page with mode and reply policy
  PUT  /api/v1/channels/dx/threads/<thread_key>   — set a 大象 thread's mode / reply policy
  GET  /api/v1/channels/github                    — GitHub account config, status, poll schedule
  PUT  /api/v1/channels/github                    — enable/disable, token or vault entry, API base
  GET  /api/v1/channels/github/threads            — GitHub issue/PR thread page
  PUT  /api/v1/channels/github/threads/<path:key> — set a GitHub thread's mode / reply policy
  GET  /api/v1/channels/{wecom,qq,wechat}         — bot account (C12): status, owner, pairing code, 微信 login QR
  PUT  /api/v1/channels/{wecom,qq,wechat}         — enable/disable, app ID and secret, pairing, 微信 QR login
  GET  /api/v1/channels/{wecom,qq,wechat}/threads — bot thread page with mode and reply policy
  PUT  /api/v1/channels/{wecom,qq,wechat}/threads/<thread_key> — set a bot thread's mode / reply policy
  GET  /api/v1/channels/outbox[?channel=<channel>] — active outbox items (drafts, queue, failures)
  GET  /api/v1/channels/outbox/<item_id>/attachments/<index> — an attached original image (thumbnail)
  POST /api/v1/channels/outbox/<item_id>/approve  — approve a draft
  POST /api/v1/channels/outbox/<item_id>/discard  — discard a draft/queued/failed item
  GET  /api/v1/channels/audit?channel=dx|github[&before=] — everything recorded for other people

Wire schemas: ``contracts/channels_v1.schema.json``. Handlers are stateless:
the owner comes from the authenticated principal and every operation goes
through ``lib.channels.service``.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from quart import Blueprint, request

from lib.api_response import (
    api_bad_request, api_conflict, api_not_found, api_ok, api_service_unavailable,
)
from lib.api_v4 import raw_success_response
from lib.log import get_logger
from lib.openapi import api_meta, media_responses, schema_definition
from lib.request_parser import async_parse_body

from .auth import request_user_id, require_scope

logger = get_logger(__name__)

api_v1_channels_bp = Blueprint('api_v1_channels', __name__)

# A Global Router answer to a command-chat message is enqueued from the
# existing terminal-listener seam (lib/channels/autoreply.py).
from lib.channels import autoreply  # noqa: E402

autoreply.install()

_BAD_REQUEST_CODES = frozenset({'invalid_update', 'invalid_thread', 'invalid_mode', 'invalid_note',
                                'invalid_reply_policy', 'invalid_decision', 'invalid_event_id',
                                'invalid_cursor', 'invalid_channel', 'auth_failed',
                                'token_unsupported', 'credential_missing'})
_UNAVAILABLE_CODES = frozenset({'skill_missing', 'bridge_failed', 'engine_error',
                                'identity_unavailable', 'group_create_failed',
                                'github_unavailable', 'rate_limited', 'qq_unavailable',
                                'wechat_unavailable'})


@lru_cache(maxsize=1)
def _contract() -> dict:
    root = Path(__file__).resolve().parents[2]
    return json.loads((root / 'contracts/channels_v1.schema.json').read_text(encoding='utf-8'))


def _responses(name: str, description: str) -> dict:
    return {'200': {'description': description, 'content': {
        'application/json': {'schema': schema_definition(_contract(), name)}}}}


def _body(name: str) -> dict:
    return {'required': True, 'content': {'application/json': {
        'schema': schema_definition(_contract(), name)}}}


def _fields(name: str) -> tuple[str, ...]:
    """A strict request body admits exactly the properties of its contract DTO."""
    return tuple(_contract()['$defs'][name]['properties'])


def _channel_error(error):
    code = getattr(error, 'code', 'channel_error')
    if code == 'not_found':
        return api_not_found('not_found', code=code, error_code=code)
    if code in _BAD_REQUEST_CODES:
        return api_bad_request(str(error), code=code, error_code=code)
    if code in _UNAVAILABLE_CODES:
        return api_service_unavailable(str(error), code=code, error_code=code)
    return api_conflict(str(error), code=code, error_code=code)


async def _call(function, *args, **kwargs):
    from quart.utils import run_sync
    from lib.channels.model import ChannelError
    try:
        return api_ok(await run_sync(function)(*args, **kwargs))
    except ChannelError as error:
        return _channel_error(error)


@api_v1_channels_bp.route('/api/v1/channels/dx', methods=['GET'])
@require_scope('conversations')
@api_meta(summary='Read the 大象 channel account configuration and status', tags=['channels'],
          scope='conversations', responses=_responses('ChannelAccountView', '大象 account'))
async def get_dx_account_v1():
    from lib.channels import service
    return await _call(service.account_view, request_user_id())


@api_v1_channels_bp.route('/api/v1/channels/dx', methods=['PUT'])
@require_scope('admin')
@api_meta(summary='Enable/disable the 大象 account, choose its login MIS or its command chat', tags=['channels'],
          scope='admin', request_body=_body('ChannelAccountUpdate'),
          responses=_responses('ChannelAccountView', 'Updated 大象 account'))
async def update_dx_account_v1():
    from lib.channels import service
    body = await async_parse_body(strict=True, exact=_fields('ChannelAccountUpdate'))
    return await _call(service.update_account, request_user_id(), body)


@api_v1_channels_bp.route('/api/v1/channels/dx/command-chat', methods=['POST'])
@require_scope('admin')
@api_meta(summary="Create the owner's single-member 'Tofu Router' command chat group",
          tags=['channels'], scope='admin', request_body=_body('CommandChatCreateRequest'),
          responses=_responses('ChannelAccountView', 'Account with its command chat'))
async def create_dx_command_chat_v1():
    from lib.channels import service
    await async_parse_body(strict=True, exact=_fields('CommandChatCreateRequest'))
    return await _call(service.create_command_chat, request_user_id())


@api_v1_channels_bp.route('/api/v1/channels/dx/threads', methods=['GET'])
@require_scope('conversations')
@api_meta(summary='List 大象 threads with their ingress mode and reply policy', tags=['channels'],
          scope='conversations', responses=_responses('ChannelThreadPage', 'Thread page'),
          parameters=[{'name': 'cursor', 'in': 'query', 'required': False,
                       'schema': {'type': 'string', 'maxLength': 512}}])
async def list_dx_threads_v1():
    return await _thread_page('dx')


async def _thread_page(channel: str):
    from lib.channels import service
    cursor = str(request.args.get('cursor') or '')
    if len(cursor) > 512:
        return api_bad_request('cursor is too long', code='invalid_cursor')
    return await _call(service.thread_page, request_user_id(), cursor=cursor, channel=channel)


async def _configure_thread(channel: str, thread_key: str):
    from lib.channels import service
    from lib.channels.model import ChannelError, ThreadKey
    body = await async_parse_body(strict=True, exact=_fields('ChannelThreadUpdate'))
    try:
        if ThreadKey.parse(thread_key).channel != channel:
            raise ChannelError('invalid_thread', f'Not a {channel} thread key')
    except ChannelError as error:
        return _channel_error(error)
    kwargs = {}
    if 'mode' in body:
        kwargs['mode'] = body['mode']
    if 'replyPolicy' in body:
        kwargs['reply_policy'] = body['replyPolicy']
    if 'note' in body:
        kwargs['note'] = body['note']
    return await _call(service.configure_thread, request_user_id(), thread_key, **kwargs)


@api_v1_channels_bp.route('/api/v1/channels/dx/threads/<thread_key>', methods=['PUT'])
@require_scope('conversations')
@api_meta(summary="Set a 大象 thread's ingress mode, reply policy (null restores the default) or owner note",
          tags=['channels'], scope='conversations', request_body=_body('ChannelThreadUpdate'),
          responses=_responses('ChannelThreadView', 'Updated thread'))
async def update_dx_thread_v1(thread_key):
    return await _configure_thread('dx', thread_key)


@api_v1_channels_bp.route('/api/v1/channels/github', methods=['GET'])
@require_scope('conversations')
@api_meta(summary='Read the GitHub channel account configuration, status and poll schedule',
          tags=['channels'], scope='conversations',
          responses=_responses('GitHubAccountView', 'GitHub account'))
async def get_github_account_v1():
    from lib.channels.github import account
    return await _call(account.account_view, request_user_id())


@api_v1_channels_bp.route('/api/v1/channels/github', methods=['PUT'])
@require_scope('admin')
@api_meta(summary='Enable/disable the GitHub account, set its token or credential vault entry, '
                  'or its API base URL', tags=['channels'], scope='admin',
          request_body=_body('GitHubAccountUpdate'),
          responses=_responses('GitHubAccountView', 'Updated GitHub account'))
async def update_github_account_v1():
    from lib.channels.github import account
    body = await async_parse_body(strict=True, exact=_fields('GitHubAccountUpdate'))
    return await _call(account.update_account, request_user_id(), body)


@api_v1_channels_bp.route('/api/v1/channels/github/threads', methods=['GET'])
@require_scope('conversations')
@api_meta(summary='List GitHub issue and pull request threads with their mode and reply policy',
          tags=['channels'], scope='conversations', responses=_responses('ChannelThreadPage', 'Thread page'),
          parameters=[{'name': 'cursor', 'in': 'query', 'required': False,
                       'schema': {'type': 'string', 'maxLength': 512}}])
async def list_github_threads_v1():
    return await _thread_page('github')


@api_v1_channels_bp.route('/api/v1/channels/github/threads/<path:thread_key>', methods=['PUT'])
@require_scope('conversations')
@api_meta(summary="Set a GitHub thread's ingress mode, reply policy (null restores the default) or owner note",
          tags=['channels'], scope='conversations', request_body=_body('ChannelThreadUpdate'),
          responses=_responses('ChannelThreadView', 'Updated thread'))
async def update_github_thread_v1(thread_key):
    return await _configure_thread('github', thread_key)


def _register_bot_routes(channel: str, label: str) -> None:
    """The account and thread routes of one bot platform (docs/CHANNELS.md C12)."""
    async def get_account():
        from lib.channels.bots import account
        return await _call(account.account_view, request_user_id(), channel)

    async def update_account():
        from lib.channels.bots import account
        body = await async_parse_body(strict=True, exact=_fields('BotAccountUpdate'))
        return await _call(account.update_account, request_user_id(), channel, body)

    async def list_threads():
        return await _thread_page(channel)

    async def update_thread(thread_key):
        return await _configure_thread(channel, thread_key)

    cursor = [{'name': 'cursor', 'in': 'query', 'required': False, 'schema': {'type': 'string', 'maxLength': 512}}]
    routes = (
        (get_account, 'account', '', 'GET', 'conversations',
         dict(summary=f'Read the {label} bot account: status, owner, pairing code or login code',
              responses=_responses('BotAccountView', f'{label} account'))),
        (update_account, 'update_account', '', 'PUT', 'admin',
         dict(summary=f'Enable/disable the {label} bot account, set its app ID and secret, pair its owner '
                      f'or start a login', request_body=_body('BotAccountUpdate'),
              responses=_responses('BotAccountView', f'Updated {label} account'))),
        (list_threads, 'threads', '/threads', 'GET', 'conversations',
         dict(summary=f'List {label} threads with their ingress mode and reply policy',
              responses=_responses('ChannelThreadPage', 'Thread page'), parameters=cursor)),
        (update_thread, 'update_thread', '/threads/<thread_key>', 'PUT', 'conversations',
         dict(summary=f"Set a {label} thread's ingress mode or reply policy (null restores the default)",
              request_body=_body('ChannelThreadUpdate'), responses=_responses('ChannelThreadView', 'Updated thread'))),
    )
    for function, name, suffix, method, scope, meta in routes:
        function.__name__ = function.__qualname__ = f'{name}_{channel}_v1'
        handler = require_scope(scope)(api_meta(tags=['channels'], scope=scope, **meta)(function))
        api_v1_channels_bp.route(f'/api/v1/channels/{channel}{suffix}', methods=[method])(handler)


for _channel, _label in (('wecom', '企业微信'), ('qq', 'QQ'), ('wechat', '微信')):
    _register_bot_routes(_channel, _label)


@api_v1_channels_bp.route('/api/v1/channels/outbox', methods=['GET'])
@require_scope('conversations')
@api_meta(summary='List active channel outbox items (drafts, queue, unknown, failed)',
          tags=['channels'], scope='conversations', responses=_responses('OutboxPage', 'Outbox'),
          parameters=[{'name': 'channel', 'in': 'query', 'required': False,
                       'schema': schema_definition(_contract(), 'Channel')}])
async def list_channel_outbox_v1():
    from lib.channels import service
    channel = request.args.get('channel') or None
    return await _call(service.outbox_page, request_user_id(), channel=channel)


@api_v1_channels_bp.route('/api/v1/channels/audit', methods=['GET'])
@require_scope('conversations')
@api_meta(summary="List what Tofu recorded for other people on the owner's behalf, newest first",
          tags=['channels'], scope='conversations', responses=_responses('AuditPage', 'Audit page'),
          parameters=[{'name': 'channel', 'in': 'query', 'required': True,
                       'schema': schema_definition(_contract(), 'Channel')},
                      {'name': 'before', 'in': 'query', 'required': False,
                       'schema': {'type': 'string', 'maxLength': 512}}])
async def list_channel_audit_v1():
    from lib.channels import service
    return await _call(service.audit_page, request_user_id(), channel=str(request.args.get('channel') or ''),
                       before=str(request.args.get('before') or ''))


@api_v1_channels_bp.route('/api/v1/channels/outbox/<item_id>/attachments/<int:index>', methods=['GET'])
@require_scope('conversations')
@api_meta(summary="Read an original image attached to an outbox item (a draft's thumbnail)",
          tags=['channels'], scope='conversations',
          responses=media_responses('image/png', 'image/jpeg', 'image/gif', 'image/bmp'))
@raw_success_response
async def channel_outbox_attachment_v1(item_id, index):
    from quart import Response
    from quart.utils import run_sync
    from lib.channels import service
    from lib.channels.model import ChannelError
    try:
        data, mime = await run_sync(service.outbox_attachment)(request_user_id(), item_id, index)
    except ChannelError as error:
        return _channel_error(error)
    response = Response(data, content_type=mime)
    response.headers.update({'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'private, no-store',
                             'Content-Disposition': 'inline', 'Referrer-Policy': 'no-referrer'})
    return response


@api_v1_channels_bp.route('/api/v1/channels/outbox/<item_id>/approve', methods=['POST'])
@require_scope('conversations')
@api_meta(summary='Approve a draft outbox item (owner decision)', tags=['channels'],
          scope='conversations', responses=_responses('OutboxItemView', 'Approved item'))
async def approve_channel_outbox_v1(item_id):
    from lib.channels import service
    return await _call(service.decide_outbox, request_user_id(), item_id, 'approve')


@api_v1_channels_bp.route('/api/v1/channels/outbox/<item_id>/discard', methods=['POST'])
@require_scope('conversations')
@api_meta(summary='Discard a draft, queued or failed outbox item', tags=['channels'],
          scope='conversations', responses=_responses('OutboxItemView', 'Discarded item'))
async def discard_channel_outbox_v1(item_id):
    from lib.channels import service
    return await _call(service.decide_outbox, request_user_id(), item_id, 'discard')


__all__ = ['api_v1_channels_bp']
