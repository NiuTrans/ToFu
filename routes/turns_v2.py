"""Breaking v2 Turn / Attempt API.

The routes expose only conversation/turn/attempt identities.  ``task_id`` is
kept behind this adapter as executor plumbing and never becomes message
identity on the wire.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re

from quart import Blueprint, Response, request

from lib.api_response import (
    api_bad_request,
    api_conflict,
    api_internal_error,
    api_not_found,
    api_ok,
    sse_response,
)
from lib.log import get_logger
from lib.request_parser import parse_body
from lib.storage.errors import StorageError
from lib.turn_lifecycle import (
    LifecycleConflict,
    LifecycleNotFound,
    abort_attempt,
    attempt_is_terminal,
    bind_task,
    claim_attempt_start,
    create_branch_lane,
    create_attempt,
    create_turn_pair,
    delete_branch_lane,
    delete_turns,
    fail_start,
    get_attempt,
    get_conversation_revision,
    get_turn,
    list_turns,
    read_events,
    update_turn_projection,
)
from routes.api_v1.auth import current_auth, require_scope
from routes.common import _notify_conv_changed

logger = get_logger(__name__)
turns_v2_bp = Blueprint('turns_v2', __name__)


def _user_id():
    auth = current_auth()
    return getattr(auth, 'user_id', None) or 1 if auth else 1


def _conflict(exc: LifecycleConflict):
    return api_conflict({
        'kind': exc.code,
        'message': exc.message,
    }, latestTurn=exc.turn)


def _response_payload(result: dict) -> dict:
    public = dict(result)
    public.pop('_needsStart', None)
    return public


def _turns_etag(conversation_id: str, revision: int, lane_id: str | None,
                after_ordinal: int | None, limit: int, light: bool) -> str:
    """Weak validator for a turns-list projection.

    ``conversationRevision`` is the authoritative monotonic version, but the
    list body also depends on the request's filter/window, so the validator
    folds those in as well.  A repeat repair/snapshot fetch for an unchanged
    conversation therefore returns 304 (empty body) instead of re-shipping a
    multi-MB turns payload.
    """
    fingerprint = '|'.join([
        conversation_id,
        str(int(revision or 0)),
        str(lane_id or ''),
        str(after_ordinal if after_ordinal is not None else ''),
        str(int(limit or 0)),
        'light' if light else 'full',
    ])
    return hashlib.sha1(fingerprint.encode('utf-8')).hexdigest()


def _start_attempt(result: dict, request_config: dict, request_data: dict):
    if not result.get('_needsStart'):
        return None
    turn = result['turn']
    attempt = result['attempt']
    if not claim_attempt_start(attempt['attemptId']):
        # Another request/process already owns dispatch for this exact durable
        # command. The caller attaches through the returned attempt stream.
        return None
    operation = attempt['operation']
    config = dict(request_config or {})
    config.update({
        '_turnProtocolV2': True,
        '_turnId': turn['turnId'],
        '_attemptId': attempt['attemptId'],
    })
    # The allocated output turn is an identity/projection target, never an
    # input message for its own generation.  Context construction therefore
    # always excludes that final row (generate as well as every resume mode).
    config['excludeLast'] = True
    projection = turn.get('projection') or {}
    if operation == 'continue':
        prefix = projection.get('content') or ''
        config.update({
            'resumePrefill': prefix,
            'contentPrefix': prefix,
        })
    elif operation == 'checkpoint_resume':
        config.update({
            'contentPrefix': projection.get('content') or '',
            'checkpointToolRounds': projection.get('toolRounds') or [],
        })

    # Resolve the shared entry dynamically so existing tests/integrators that
    # steer routes.chat._start_task_for_conv keep controlling this adapter.
    from routes import chat as chat_routes
    task_id, error_response = chat_routes._start_task_for_conv(
        turn['conversationId'], config, request_data)
    if error_response is not None:
        fail_start(attempt['attemptId'], {
            'kind': 'task_start_failed',
            'message': 'The generation executor could not be started.',
        })
        return api_internal_error(
            {'kind': 'task_start_failed',
             'message': 'The generation executor could not be started.'},
            latestTurn=get_turn(
                turn['conversationId'], turn['turnId'], user_id=_user_id()),
        )
    bind_task(attempt['attemptId'], task_id)
    return None


@turns_v2_bp.route('/api/v2/conversations/<conversation_id>/turns',
                   methods=['POST'])
@require_scope('chat')
def create_conversation_turn(conversation_id):
    data = parse_body()
    command_id = str(data.get('commandId') or '')
    if not command_id:
        return api_bad_request('commandId required')
    config = data.get('config') or {}
    input_projection = data.get('inputTurn')
    raw_message = data.get('message')
    if input_projection is None and isinstance(raw_message, dict):
        # Keep translation, references, attachments and turn-context building
        # on the server exactly as the retired send route did. Identity is
        # deliberately stripped by the lifecycle normalizer; commandId is the
        # only client retry key and turnId remains server-issued.
        from lib.chat import build_user_msg_from_payload
        input_projection = build_user_msg_from_payload(
            raw_message, config, conv_id=conversation_id)
    if input_projection is None:
        input_projection = data.get('input', raw_message or '')
    conversation_defaults = data.get('conversation')
    if isinstance(conversation_defaults, dict):
        conversation_defaults = dict(conversation_defaults)
        if not conversation_defaults.get('title') and isinstance(raw_message, dict):
            title_text = str(raw_message.get('text') or '')
            title_text = re.sub(
                r'</?(?:notranslate|nt)>', '', title_text,
                flags=re.IGNORECASE)
            if title_text:
                conversation_defaults['title'] = (
                    title_text[:60] + ('...' if len(title_text) > 60 else ''))
    try:
        result = create_turn_pair(
            conversation_id,
            command_id=command_id,
            input_projection=input_projection,
            config=config,
            lane_id=str(data.get('laneId') or 'main'),
            parent_turn_id=data.get('parentTurnId'),
            kind=str(data.get('kind') or 'reply'),
            output_actor=str(data.get('actor') or 'assistant'),
            run_id=str(data.get('runId') or ''),
            user_id=_user_id(),
            conversation_defaults=conversation_defaults,
        )
        start_error = _start_attempt(result, config, data)
        if start_error is not None:
            return start_error
        # Re-read after bind so the ACK carries running (or an already-fast
        # terminal state), never the pre-dispatch pending snapshot.
        result['turn'] = get_turn(
            conversation_id, result['turn']['turnId'], user_id=_user_id())
        result['attempt'] = get_attempt(
            result['attempt']['attemptId'], user_id=_user_id())
        result['conversationRevision'] = get_conversation_revision(
            conversation_id, user_id=_user_id())
        _notify_conv_changed(
            conversation_id, rev=result['conversationRevision'], user_id=_user_id())
        return api_ok(_response_payload(result))
    except LifecycleConflict as exc:
        logger.debug('[turns-v2] create conflict conv=%s kind=%s',
                     conversation_id[:8], exc.code)
        return _conflict(exc)
    except LifecycleNotFound as exc:
        return api_not_found(str(exc))
    except ValueError as exc:
        return api_bad_request(str(exc))
    except Exception as exc:
        logger.error('[turns-v2] create failed conv=%s: %s',
                     conversation_id[:8], exc, exc_info=True)
        return api_internal_error('internal_error')


@turns_v2_bp.route('/api/v2/conversations/<conversation_id>/turns/<turn_id>',
                   methods=['PATCH'])
@require_scope('chat')
def patch_conversation_turn(conversation_id, turn_id):
    data = parse_body()
    expected = data.get('expectedProjectionRevision')
    projection = data.get('projection')
    if expected is None:
        return api_bad_request('expectedProjectionRevision required')
    if not isinstance(projection, dict):
        return api_bad_request('projection object required')
    try:
        result = update_turn_projection(
            conversation_id, turn_id, projection=projection,
            expected_projection_revision=int(expected), user_id=_user_id())
        _notify_conv_changed(
            conversation_id, rev=result['conversationRevision'], user_id=_user_id())
        return api_ok(result)
    except LifecycleConflict as exc:
        logger.debug('[turns-v2] patch conflict conv=%s turn=%s kind=%s',
                     conversation_id[:8], turn_id[:8], exc.code)
        return _conflict(exc)
    except LifecycleNotFound as exc:
        return api_not_found(str(exc))
    except (TypeError, ValueError) as exc:
        return api_bad_request(str(exc))


@turns_v2_bp.route('/api/v2/conversations/<conversation_id>/turns/<turn_id>/lanes',
                   methods=['POST'])
@require_scope('chat')
def create_turn_branch_lane(conversation_id, turn_id):
    data = parse_body()
    expected = data.get('expectedProjectionRevision')
    if expected is None:
        return api_bad_request('expectedProjectionRevision required')
    try:
        result = create_branch_lane(
            conversation_id, turn_id,
            title=str(data.get('title') or 'Branch'),
            anchor_text=str(data.get('anchorText') or ''),
            parent_selection=str(data.get('parentSelection') or ''),
            kind=str(data.get('kind') or 'branch'),
            expected_projection_revision=int(expected), user_id=_user_id())
        _notify_conv_changed(
            conversation_id, rev=result['conversationRevision'], user_id=_user_id())
        return api_ok(result)
    except LifecycleConflict as exc:
        logger.debug('[turns-v2] branch create conflict conv=%s turn=%s kind=%s',
                     conversation_id[:8], turn_id[:8], exc.code)
        return _conflict(exc)
    except LifecycleNotFound as exc:
        return api_not_found(str(exc))
    except (TypeError, ValueError) as exc:
        return api_bad_request(str(exc))


@turns_v2_bp.route(
    '/api/v2/conversations/<conversation_id>/turns/<turn_id>/lanes/<lane_id>',
    methods=['DELETE'])
@require_scope('chat')
def delete_turn_branch_lane(conversation_id, turn_id, lane_id):
    try:
        result = delete_branch_lane(
            conversation_id, turn_id, lane_id, user_id=_user_id())
        _notify_conv_changed(
            conversation_id, rev=result['conversationRevision'], user_id=_user_id())
        return api_ok(result)
    except LifecycleConflict as exc:
        logger.debug('[turns-v2] branch delete conflict conv=%s turn=%s kind=%s',
                     conversation_id[:8], turn_id[:8], exc.code)
        return _conflict(exc)
    except LifecycleNotFound as exc:
        return api_not_found(str(exc))


@turns_v2_bp.route('/api/v2/conversations/<conversation_id>/turns/delete',
                   methods=['POST'])
@require_scope('chat')
def delete_conversation_turns(conversation_id):
    data = parse_body()
    try:
        result = delete_turns(
            conversation_id, data.get('turnIds') or [], user_id=_user_id())
        _notify_conv_changed(
            conversation_id, rev=result['conversationRevision'], user_id=_user_id())
        return api_ok(result)
    except LifecycleConflict as exc:
        logger.debug('[turns-v2] turn delete conflict conv=%s kind=%s',
                     conversation_id[:8], exc.code)
        return _conflict(exc)
    except LifecycleNotFound as exc:
        return api_not_found(str(exc))
    except ValueError as exc:
        return api_bad_request(str(exc))


@turns_v2_bp.route('/api/v2/conversations/<conversation_id>/turns/<turn_id>/attempts',
                   methods=['POST'])
@require_scope('chat')
def create_turn_attempt(conversation_id, turn_id):
    data = parse_body()
    command_id = str(data.get('commandId') or '')
    operation = str(data.get('operation') or '')
    expected = data.get('expectedProjectionRevision')
    if not command_id:
        return api_bad_request('commandId required')
    if expected is None:
        return api_bad_request('expectedProjectionRevision required')
    config = data.get('config') or {}
    try:
        result = create_attempt(
            conversation_id, turn_id,
            command_id=command_id,
            operation=operation,
            expected_projection_revision=int(expected),
            config=config,
            resume_anchor=data.get('resumeAnchor'),
            input_update=data.get('inputUpdate'),
            expected_input_projection_revision=data.get(
                'expectedInputProjectionRevision'),
            user_id=_user_id(),
        )
        start_error = _start_attempt(result, config, data)
        if start_error is not None:
            return start_error
        result['turn'] = get_turn(conversation_id, turn_id, user_id=_user_id())
        result['attempt'] = get_attempt(
            result['attempt']['attemptId'], user_id=_user_id())
        result['conversationRevision'] = get_conversation_revision(
            conversation_id, user_id=_user_id())
        _notify_conv_changed(
            conversation_id, rev=result['conversationRevision'], user_id=_user_id())
        return api_ok(_response_payload(result))
    except LifecycleConflict as exc:
        logger.debug('[turns-v2] attempt conflict turn=%s kind=%s',
                     turn_id[:8], exc.code)
        return _conflict(exc)
    except LifecycleNotFound as exc:
        return api_not_found(str(exc))
    except (TypeError, ValueError) as exc:
        return api_bad_request(str(exc))
    except Exception as exc:
        logger.error('[turns-v2] attempt create failed turn=%s: %s',
                     turn_id[:8], exc, exc_info=True)
        return api_internal_error('internal_error')


@turns_v2_bp.route('/api/v2/conversations/<conversation_id>/turns',
                   methods=['GET'])
@require_scope('chat')
def get_conversation_turns(conversation_id):
    try:
        after = request.args.get('afterOrdinal')
        lane_id = request.args.get('laneId') or None
        after_ordinal = (int(after) if after not in (None, '') else None)
        limit = int(request.args.get('limit') or 500)
        light = request.args.get('projection') == 'light'
        result = list_turns(
            conversation_id,
            user_id=_user_id(),
            lane_id=lane_id,
            after_ordinal=after_ordinal,
            limit=limit,
            light=light,
        )
        etag = _turns_etag(
            conversation_id, result.get('conversationRevision'),
            lane_id, after_ordinal, limit, light)
        if request.if_none_match and etag in request.if_none_match:
            response = Response(status=304)
            response.headers['ETag'] = etag
            return response
        response, status = api_ok(result)
        response.headers['ETag'] = etag
        return response, status
    except LifecycleNotFound as exc:
        return api_not_found(str(exc))
    except (TypeError, ValueError) as exc:
        return api_bad_request(str(exc))


@turns_v2_bp.route('/api/v2/attempts/<attempt_id>/stream', methods=['GET'])
@require_scope('chat')
async def stream_attempt(attempt_id):
    try:
        after = int(request.args.get('after') or 0)
        # Ownership/not-found probe happens before response headers are sent.
        await asyncio.to_thread(
            read_events, attempt_id, after=after, user_id=_user_id(), limit=1)
    except LifecycleNotFound as exc:
        return api_not_found(str(exc))
    except (TypeError, ValueError):
        return api_bad_request('after must be an integer')

    user_id = _user_id()

    async def generate():
        cursor = after
        idle_ticks = 0
        storage_error_ticks = 0
        while True:
            try:
                events = await asyncio.to_thread(
                    read_events, attempt_id, after=cursor,
                    user_id=user_id, limit=500)
                terminal = (not events and await asyncio.to_thread(
                    attempt_is_terminal, attempt_id, user_id=user_id))
            except StorageError as exc:
                if not exc.retryable:
                    raise
                # Transient storage stall (network-FS reclaim under cgroup
                # pressure wedges sidecar reads for seconds at a time): hold
                # the stream with backoff + keepalives instead of killing the
                # user's live view — an EventSource reconnect would just
                # re-hit the same wedged read lane.
                storage_error_ticks += 1
                idle_ticks += 1
                if idle_ticks % 60 == 0:
                    yield ': keepalive\n\n'
                await asyncio.sleep(min(0.25 * storage_error_ticks, 2.0))
                continue
            storage_error_ticks = 0
            if events:
                idle_ticks = 0
                for event in events:
                    cursor = max(cursor, int(event.get('seq') or 0))
                    yield (f'id: {cursor}\n'
                           f'event: {event.get("type") or "message"}\n'
                           f'data: {json.dumps(event, ensure_ascii=False)}\n\n')
            else:
                idle_ticks += 1
                if terminal:
                    break
                if idle_ticks % 60 == 0:
                    yield ': keepalive\n\n'
                await asyncio.sleep(0.25)

    return sse_response(
        generate(), timeout_none=True,
        extra_headers={'X-Tofu-Task-Kind': 'generation-attempt'})


@turns_v2_bp.route('/api/v2/attempts/<attempt_id>/abort', methods=['POST'])
@require_scope('chat')
def abort_generation_attempt(attempt_id):
    try:
        return api_ok(abort_attempt(attempt_id, user_id=_user_id()))
    except LifecycleNotFound as exc:
        return api_not_found(str(exc))
    except Exception as exc:
        logger.error('[turns-v2] abort failed attempt=%s: %s',
                     attempt_id[:8], exc, exc_info=True)
        return api_internal_error('internal_error')


__all__ = ['turns_v2_bp']
