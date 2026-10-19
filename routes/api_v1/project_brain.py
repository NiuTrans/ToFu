"""Signal-driven Project Brain HTTP projection and human-command surface."""

from __future__ import annotations

from quart import Blueprint

from lib.api_response import api_bad_request, api_error, api_internal_error, api_ok
from lib.log import get_logger
from lib.openapi import api_meta
from lib.request_parser import decode_proxy_path_arg, parse_body

from .auth import request_user_id, require_auth, require_scope

from .auth import request_principal
from lib.project_dispatch.contract import request_body as dispatch_request_body, query_parameters as dispatch_query_parameters
from lib.project_dispatch import continuity_contract

logger = get_logger(__name__)
api_v1_project_brain_bp = Blueprint('api_v1_project_brain', __name__)


def _path(explicit: str = '') -> str:
    path = str(explicit or decode_proxy_path_arg('path') or '').strip()
    if not path:
        raise ValueError('path is required')
    return path


def _owner() -> int:
    return int(request_user_id())


def _safe_route_error_response(exc: Exception):

    if isinstance(exc, PermissionError):
        return api_error('forbidden', status=403)
    if isinstance(exc, ValueError):
        return api_bad_request(str(exc))
    from lib.storage import StorageError, http_status_for_storage_error

    from werkzeug.exceptions import HTTPException
    if isinstance(exc, HTTPException):
        return api_error(exc.description, status=exc.code or 500)
    if isinstance(exc, StorageError):
        return api_error(exc, status=http_status_for_storage_error(exc))
    logger.exception('[ProjectBrain.v1] request failed')
    return api_internal_error(
        exc, context='project:brain', source='routes.api_v1.project_brain',
        log_traceback=False,
    )


@api_v1_project_brain_bp.route('/api/v1/project/coordination', methods=['GET'])
@require_scope('conversations')
@api_meta(summary='Read explicit project coordinator binding', tags=['project'], scope='conversations')
def project_coordination_read():
    try:
        from lib.conversations.project_coordination import ProjectCoordinationRepository
        return api_ok(ProjectCoordinationRepository(_owner()).read(_path()))
    except Exception as exc:
        return _safe_route_error_response(exc)


@api_v1_project_brain_bp.route('/api/v1/project/coordination/conversations', methods=['GET'])
@require_scope('conversations')
@api_meta(summary='Read a bounded project conversation page, including unstarted workers', tags=['project'], scope='conversations')
def project_coordination_conversations():
    from quart import request
    from lib.conversations.coordinator_conversations import conversation_page
    try:
        return api_ok(conversation_page(_owner(), _path(), request.args.get('cursor')))
    except Exception as exc:
        return _safe_route_error_response(exc)


@api_v1_project_brain_bp.route('/api/v1/project/coordination/reports', methods=['GET'])
@require_scope('conversations')
@api_meta(summary='Read the latest Coordinator report and durable unread state', tags=['project'], scope='conversations')
def project_coordinator_report_read():
    try:
        from lib.conversations.coordinator_reports import CoordinatorReports
        return api_ok(CoordinatorReports(_owner(), _path()).read())
    except Exception as exc:
        return _safe_route_error_response(exc)


@api_v1_project_brain_bp.route('/api/v1/project/coordination/budget', methods=['GET'])
@require_scope('conversations')
@api_meta(summary='Read Coordinator request consumption and future request allowances', tags=['project'], scope='conversations')
def project_coordinator_budget_read():
    try:
        from lib.conversations.coordinator_budget import read_state
        return api_ok(read_state(_owner(), _path()))
    except Exception as exc:
        return _safe_route_error_response(exc)


@api_v1_project_brain_bp.route('/api/v1/project/coordination/budget', methods=['POST'])
@require_scope('conversations')
@api_meta(summary='Set finite allowances for future human Coordinator requests', tags=['project'], scope='conversations',
          request_body=dispatch_request_body('coordinatorBudgetUpdate'))
def project_coordinator_budget_update():
    try:
        from lib.project_dispatch.contract import parse
        from lib.conversations.coordinator_budget import update_settings
        return api_ok(update_settings(_owner(), parse('coordinatorBudgetUpdate')))
    except Exception as exc:
        return _safe_route_error_response(exc)


@api_v1_project_brain_bp.route('/api/v1/project/coordination/reports/read', methods=['POST'])
@require_scope('conversations')
@api_meta(summary='Acknowledge the exact displayed Coordinator report', tags=['project'], scope='conversations')
def project_coordinator_report_ack():
    data = parse_body(exact=('path', 'conversationId', 'bindingRevision', 'turnId'))
    try:
        from lib.conversations.coordinator_reports import CoordinatorReports
        return api_ok(CoordinatorReports(_owner(), _path(data.get('path'))).mark_read(data))
    except Exception as exc:
        return _safe_route_error_response(exc)


@api_v1_project_brain_bp.route('/api/v1/project/coordination/bind', methods=['POST'])
@require_scope('conversations')
@api_meta(summary='Clear or reaffirm the current project coordinator binding', tags=['project'], scope='conversations')
def project_coordination_bind():
    from quart import request
    data = parse_body(exact=('path', 'conversationId', 'expectedRevision'))
    try:
        from lib.conversations.project_coordination import ProjectCoordinationRepository
        return api_ok(ProjectCoordinationRepository(_owner()).bind(
            data.get('path'), data.get('conversationId'),
            expected_revision=data.get('expectedRevision'),
            command_id=request.headers.get('Idempotency-Key') or '',
        ))
    except Exception as exc:
        return _safe_route_error_response(exc)


@api_v1_project_brain_bp.route('/api/v1/project/coordination/create', methods=['POST'])
@require_scope('conversations')
@api_meta(summary='Create an independent project coordinator conversation without execution', tags=['project'], scope='conversations')
def project_coordination_create():
    from quart import request
    data = parse_body(exact=('path', 'folderId', 'expectedRevision'))
    try:
        from lib.conversations.project_coordination import ProjectCoordinationRepository
        from lib.conversations.project_identity import normalize_project_path
        from .folders import _repository
        owner = _owner()
        path = _path(data.get('path'))
        folder = next((row for row in _repository().list(owner)
                       if row['id'] == data.get('folderId')), None)
        if folder is None:
            raise PermissionError('Folder is not owned by this user')
        paths = folder.get('projectPaths') or [folder.get('projectPath') or '']
        if normalize_project_path(path) not in [normalize_project_path(p) for p in paths]:
            raise ValueError('Project does not belong to the selected folder')
        return api_ok(ProjectCoordinationRepository(owner).create(
            path, folder_id=folder['id'], expected_revision=data.get('expectedRevision'),
            command_id=request.headers.get('Idempotency-Key') or '',
        ))
    except Exception as exc:
        return _safe_route_error_response(exc)


@api_v1_project_brain_bp.route('/api/v1/project/coordination/messages', methods=['GET'])
@require_scope('conversations')
@api_meta(summary='Read scoped project conversation message receipts', tags=['project'], scope='conversations')
def project_message_receipts():
    from quart import request
    try:
        from lib.conversations.project_messages import page_view
        return api_ok(page_view(_owner(), _path(), request.args.get('conversation_id'),
                                request.args.get('before_id') or ''))
    except Exception as exc:
        return _safe_route_error_response(exc)


def _dispatch_service(project_path):
    from lib.project_dispatch.repository import ProjectDispatchRepository
    from lib.project_dispatch.service import ProjectDispatchService
    principal = request_principal()
    return ProjectDispatchService(ProjectDispatchRepository(principal.require_owner(), project_path)), principal


@api_v1_project_brain_bp.route('/api/v1/project/coordination/assignments', methods=['POST'])
@require_scope('chat')
@api_meta(summary='Explicitly authorize and dispatch a bounded no-tool consultation', tags=['project'],
          scope='chat', request_body=dispatch_request_body('request'))
def project_assignment_create():
    from quart import request
    from lib.project_dispatch.contract import parse
    try:
        data = parse('request')
        service, principal = _dispatch_service(data['projectPath'])
        return api_ok(service.authorize_and_dispatch(data, principal=principal,
                      command_id=request.headers.get('Idempotency-Key') or ''))
    except Exception as exc:
        return _safe_route_error_response(exc)


@api_v1_project_brain_bp.route('/api/v1/project/coordination/assignments', methods=['GET'])
@require_scope('chat')
@api_meta(summary='Read bounded assignment facts or reconcile an exact command without execution',
          tags=['project'], scope='chat', parameters=dispatch_query_parameters())
def project_assignment_list():
    from quart import request
    from lib.project_dispatch.repository import _validate_schema
    try:
        query = {'path': _path()}
        for name in ('commandId', 'assignmentId', 'cursor'):
            if name in request.args:
                query[name] = request.args.get(name)
        _validate_schema(query, 'listQuery')
        service, principal = _dispatch_service(query['path'])
        return api_ok(service.history_page(principal=principal, command_id=query.get('commandId'),
            assignment_id=query.get('assignmentId'), cursor=query.get('cursor')))
    except Exception as exc:
        return _safe_route_error_response(exc)


@api_v1_project_brain_bp.route('/api/v1/project/coordination/assignments/dispatch', methods=['POST'])
@require_scope('chat')
@api_meta(summary='Retry the same authorized consultation command without new grant', tags=['project'],
          scope='chat', request_body=dispatch_request_body('retry'))
def project_assignment_dispatch():
    from lib.project_dispatch.contract import parse
    try:
        data = parse('retry')
        service, principal = _dispatch_service(data['projectPath'])
        return api_ok(service.dispatch(data['assignmentId'], principal=principal))
    except Exception as exc:
        return _safe_route_error_response(exc)


@api_v1_project_brain_bp.route('/api/v1/project/coordination/assignments/revoke', methods=['POST'])
@require_scope('chat')
@api_meta(summary='Revoke project grant then cancel its exact existing attempt', tags=['project'],
          scope='chat', request_body=dispatch_request_body('revoke'))
def project_assignment_revoke():
    from lib.project_dispatch.contract import parse
    try:
        data = parse('revoke')
        service, principal = _dispatch_service(data['projectPath'])
        return api_ok(service.revoke(data['assignmentId'], principal=principal,
                      expected_revision=data['expectedRevision']))
    except Exception as exc:
        return _safe_route_error_response(exc)


@api_v1_project_brain_bp.route('/api/v1/project/coordination/assignments/review', methods=['POST'])
@require_scope('chat')
@api_meta(summary='Record review of an exact completed consultation result; not delivery', tags=['project'],
          scope='chat', request_body=dispatch_request_body('reviewRequest'))
def project_assignment_review():
    from lib.project_dispatch.contract import parse
    try:
        data = parse('reviewRequest')
        service, principal = _dispatch_service(data['projectPath'])
        return api_ok(service.review(data['assignmentId'], principal=principal,
                      decision=data['decision'], note=data['note'], result_revision=data['resultRevision'],
                      expected_revision=data['expectedRevision']))
    except Exception as exc:
        return _safe_route_error_response(exc)


@api_v1_project_brain_bp.route('/api/v1/project/board', methods=['GET'])
@require_auth
@api_meta(summary='Read automatic project work items', tags=['project'])
def project_brain_board():
    try:
        from lib.conversations.project_brain import board_projection
        return api_ok(board_projection(_path(), user_id=_owner()))
    except Exception as exc:
        return _safe_route_error_response(exc)


@api_v1_project_brain_bp.route('/api/v1/project/coordination/assignments/publish', methods=['POST'])
@require_scope('chat')
@api_meta(summary='Explicitly authorize publication of an accepted exact file-task result',
          tags=['project'], scope='chat', request_body=dispatch_request_body('publishRequest'))
def project_assignment_publish():
    from lib.project_dispatch.contract import parse
    try:
        data = parse('publishRequest')
        service, principal = _dispatch_service(data['projectPath'])
        return api_ok(service.publish(data['assignmentId'], principal=principal,
            expected_revision=data['expectedRevision'], result_revision=data['resultRevision'],
            expected_checkpoint_sha=data['expectedCheckpointSha']))
    except Exception as exc:
        return _safe_route_error_response(exc)


@api_v1_project_brain_bp.route('/api/v1/project/feed', methods=['GET'])
@require_auth
@api_meta(summary='Read important project narrative events', tags=['project'])
def project_brain_feed():
    try:
        from quart import request
        from lib.conversations.project_brain import feed_projection
        since = int(request.args.get('since') or 0)
        limit = int(request.args.get('limit') or 100)
        return api_ok(feed_projection(
            _path(), user_id=_owner(), since_sequence=since, limit=limit))
    except Exception as exc:
        return _safe_route_error_response(exc)


@api_v1_project_brain_bp.route('/api/v1/project/charter', methods=['GET'])
@require_auth
@api_meta(summary='Read executable checker-backed Charter', tags=['project'])
def project_brain_charter():
    try:
        from lib.conversations.project_brain import charter_projection
        return api_ok(charter_projection(_path(), user_id=_owner()))
    except Exception as exc:
        return _safe_route_error_response(exc)


@api_v1_project_brain_bp.route('/api/v1/project/brain/status', methods=['GET'])
@require_auth
@api_meta(summary='Read derived Project Brain status', tags=['project'])
def project_brain_status():
    try:
        from lib.conversations.project_brain import status_projection
        return api_ok(status_projection(_path(), user_id=_owner()))
    except Exception as exc:
        return _safe_route_error_response(exc)


@api_v1_project_brain_bp.route('/api/v1/project/brain/summary', methods=['GET'])
@require_auth
@api_meta(summary='Read compact Project Brain projection summary', tags=['project'])
def project_brain_summary():
    try:
        from lib.conversations.project_brain import (
            board_projection, charter_projection,
            status_projection, watch_projection,
        )
        path = _path()
        user_id = _owner()
        return api_ok({
            'board': board_projection(path, user_id=user_id),
            'status': status_projection(path, user_id=user_id),
            'charter': charter_projection(path, user_id=user_id),
            'watch': watch_projection(path, user_id=user_id),
        })
    except Exception as exc:
        return _safe_route_error_response(exc)


@api_v1_project_brain_bp.route('/api/v1/project/brain/watch', methods=['GET'])
@require_auth
@api_meta(summary='Read project Watch items', tags=['project'])
def project_brain_watch():
    try:
        from lib.conversations.project_brain import watch_projection
        return api_ok(watch_projection(_path(), user_id=_owner()))
    except Exception as exc:
        return _safe_route_error_response(exc)


@api_v1_project_brain_bp.route('/api/v1/project/brain/watch/add', methods=['POST'])
@require_auth
@api_meta(summary='Add a human-maintained Watch item', tags=['project'])
def project_brain_watch_add():
    data = parse_body(exact=('conversationId', 'kind', 'path', 'text',))
    try:
        from lib.conversations.project_brain import add_watch_item
        return api_ok({'item': add_watch_item(
            _path(str(data.get('path') or '')),
            kind=str(data.get('kind') or 'concern'),
            text=str(data.get('text') or ''), user_id=_owner(),
            source_conversation_id=str(data.get('conversationId') or ''),
        )})
    except Exception as exc:
        return _safe_route_error_response(exc)


@api_v1_project_brain_bp.route('/api/v1/project/brain/watch/update', methods=['POST'])
@require_auth
@api_meta(summary='Update a human-maintained Watch item', tags=['project'])
def project_brain_watch_update():
    data = parse_body(exact=('itemId', 'latestResult', 'path', 'status', 'text',))
    try:
        from lib.conversations.project_brain import update_watch_item
        return api_ok({'item': update_watch_item(
            _path(str(data.get('path') or '')),
            str(data.get('itemId') or ''), user_id=_owner(),
            text=data.get('text'), status=data.get('status'),
            latest_result=data.get('latestResult'),
        )})
    except Exception as exc:
        return _safe_route_error_response(exc)


@api_v1_project_brain_bp.route('/api/v1/project/brain/watch/delete', methods=['POST'])
@require_auth
@api_meta(summary='Delete a human-maintained Watch item', tags=['project'])
def project_brain_watch_delete():
    data = parse_body(exact=('itemId', 'path',))
    try:
        from lib.conversations.project_brain import delete_watch_item
        delete_watch_item(
            _path(str(data.get('path') or '')),
            str(data.get('itemId') or ''), user_id=_owner())
        return api_ok({'deleted': True})
    except Exception as exc:
        return _safe_route_error_response(exc)


@api_v1_project_brain_bp.route('/api/v1/project/brain/checkers', methods=['GET'])
@require_auth
@api_meta(summary='List versioned project checker definitions', tags=['project'])
def project_brain_checkers():
    try:
        from lib.conversations.project_brain import checker_catalog
        return api_ok(checker_catalog(_path(), user_id=_owner()))
    except Exception as exc:
        return _safe_route_error_response(exc)


@api_v1_project_brain_bp.route(
    '/api/v1/project/brain/checkers/register', methods=['POST'])
@require_auth
@api_meta(summary='Register an immutable checker version', tags=['project'])
def project_brain_checker_register():
    data = parse_body(exact=('definition', 'path',))
    try:
        from lib.conversations.project_brain import register_checker
        definition = data.get('definition')
        if not isinstance(definition, dict):
            raise ValueError('definition is required')
        return api_ok({'checker': register_checker(
            _path(str(data.get('path') or '')), definition,
            user_id=_owner())})
    except Exception as exc:
        return _safe_route_error_response(exc)


@api_v1_project_brain_bp.route(
    '/api/v1/project/brain/checkers/run', methods=['POST'])
@require_auth
@api_meta(summary='Run one registered checker version', tags=['project'])
def project_brain_checker_run():
    data = parse_body(exact=('checkerId', 'path', 'version', 'workId',))
    try:
        from lib.conversations.project_brain import run_checker
        return api_ok({'result': run_checker(
            _path(str(data.get('path') or '')),
            str(data.get('checkerId') or ''),
            int(data.get('version') or 0), user_id=_owner(),
            work_id=str(data.get('workId') or ''), reason='manual',
        )})
    except Exception as exc:
        return _safe_route_error_response(exc)


@api_v1_project_brain_bp.route(
    '/api/v1/project/charter/decision/promote', methods=['POST'])
@require_auth
@api_meta(summary='Promote an assistant conclusion with a checker', tags=['project'])
def project_brain_decision_promote():
    data = parse_body(exact=('checkerRef', 'decisionId', 'path', 'sourceConversationId', 'sourceTurnId', 'text',))
    checker_ref = data.get('checkerRef')
    if not isinstance(checker_ref, dict):
        return api_bad_request('checkerRef is required', field='checkerRef')
    try:
        from lib.conversations.project_brain import promote_decision
        decision = promote_decision(
            _path(str(data.get('path') or '')),
            decision_id=str(data.get('decisionId') or ''),
            text=str(data.get('text') or ''),
            checker_id=str(checker_ref.get('id') or ''),
            checker_version=int(checker_ref.get('version') or 0),
            source_conversation_id=str(data.get('sourceConversationId') or ''),
            source_turn_id=str(data.get('sourceTurnId') or ''),
            user_id=_owner(),
        )
        return api_ok({'decision': decision})
    except Exception as exc:
        return _safe_route_error_response(exc)


def _continuity_query_parameters(domain, definition='listQuery'):
    schema = continuity_contract.schema(domain, definition)
    return [{'name': name, 'in': 'query', 'required': name in schema.get('required', []),
             'schema': value} for name, value in schema['properties'].items()]


def _continuity_query(domain, definition='listQuery'):
    """Admit exact schema query fields, rejecting unknown and repeated keys."""
    from quart import request
    schema = continuity_contract.schema(domain, definition)
    query = {}
    for name in request.args:
        if name not in schema['properties'] or len(request.args.getlist(name)) != 1:
            raise ValueError('Unexpected or repeated project continuity query field: ' + name)
        query[name] = decode_proxy_path_arg(name) if name == 'path' else request.args[name]
    return continuity_contract.validate(domain, definition, query)


def _scope_changes_service(project_path):
    from lib.project_dispatch.repository import ProjectDispatchRepository
    from lib.project_dispatch.scope_changes import ProjectScopeChanges
    principal = request_principal()
    return ProjectScopeChanges(ProjectDispatchRepository(principal.require_owner(), project_path)), principal


def _responsibilities_service(project_path):
    from lib.project_dispatch.responsibilities import ProjectResponsibilities
    principal = request_principal()
    return ProjectResponsibilities(principal.require_owner(), project_path), principal


def _shared_files_service(project_path):
    from lib.project_dispatch.shared_files import ProjectSharedFiles
    principal = request_principal()
    return ProjectSharedFiles(principal.require_owner(), project_path), principal


@api_v1_project_brain_bp.route('/api/v1/project/coordination/scope-changes', methods=['POST'])
@require_scope('chat')
@api_meta(summary='Apply explicit human scope withdrawal with exact grant revision pins', tags=['project'],
          scope='chat', request_body=continuity_contract.request_body('scope_changes', 'request'))
def project_scope_change_create():
    from quart import request
    try:
        data = continuity_contract.parse('scope_changes', 'request')
        service, principal = _scope_changes_service(data['projectPath'])
        return api_ok(service.create(data, principal=principal,
                      command_id=request.headers.get('Idempotency-Key') or ''))
    except Exception as exc:
        return _safe_route_error_response(exc)


@api_v1_project_brain_bp.route('/api/v1/project/coordination/scope-changes', methods=['GET'])
@require_scope('chat')
@api_meta(summary='Read scope withdrawal receipts or reconcile an exact command without effects', tags=['project'],
          scope='chat', parameters=_continuity_query_parameters('scope_changes'))
def project_scope_change_list():
    try:
        query = _continuity_query('scope_changes')
        service, principal = _scope_changes_service(query['path'])
        return api_ok({'scopeChanges': service.list(principal=principal, command_id=query.get('commandId'))})
    except Exception as exc:
        return _safe_route_error_response(exc)


@api_v1_project_brain_bp.route('/api/v1/project/coordination/scope-changes/retry', methods=['POST'])
@require_scope('chat')
@api_meta(summary='Retry cancellation and passive notification of the same revoked scope change', tags=['project'],
          scope='chat', request_body=continuity_contract.request_body('scope_changes', 'retry'))
def project_scope_change_retry():
    try:
        data = continuity_contract.parse('scope_changes', 'retry')
        service, principal = _scope_changes_service(data['projectPath'])
        return api_ok(service.retry(data['changeId'], principal=principal))
    except Exception as exc:
        return _safe_route_error_response(exc)


@api_v1_project_brain_bp.route('/api/v1/project/coordination/responsibilities', methods=['POST'])
@require_scope('chat')
@api_meta(summary='Create a finite human-authorized responsibility through existing automation', tags=['project'],
          scope='chat', request_body=continuity_contract.request_body('responsibilities', 'create'))
def project_responsibility_create():
    from quart import request
    try:
        data = continuity_contract.parse('responsibilities', 'create')
        service, principal = _responsibilities_service(data['projectPath'])
        return api_ok(service.create(data, principal=principal,
                      command_id=request.headers.get('Idempotency-Key') or ''))
    except Exception as exc:
        return _safe_route_error_response(exc)


@api_v1_project_brain_bp.route('/api/v1/project/coordination/responsibilities', methods=['GET'])
@require_scope('chat')
@api_meta(summary='Read bounded responsibility facts without triggering an occurrence', tags=['project'],
          scope='chat', parameters=_continuity_query_parameters('responsibilities'))
def project_responsibility_list():
    try:
        query = _continuity_query('responsibilities')
        service, principal = _responsibilities_service(query['path'])
        return api_ok({'responsibilities': service.list(principal=principal)})
    except Exception as exc:
        return _safe_route_error_response(exc)


@api_v1_project_brain_bp.route('/api/v1/project/coordination/responsibilities/read', methods=['GET'])
@require_scope('chat')
@api_meta(summary='Read an exact responsibility without renewing authority', tags=['project'],
          scope='chat', parameters=_continuity_query_parameters('responsibilities', 'readQuery'))
def project_responsibility_read():
    try:
        query = _continuity_query('responsibilities', 'readQuery')
        service, principal = _responsibilities_service(query['path'])
        return api_ok(service.read(query['responsibilityId'], principal=principal))
    except Exception as exc:
        return _safe_route_error_response(exc)


@api_v1_project_brain_bp.route('/api/v1/project/coordination/responsibilities/pause', methods=['POST'])
@require_scope('chat')
@api_meta(summary='Pause future responsibility occurrences at the current revision', tags=['project'],
          scope='chat', request_body=continuity_contract.request_body('responsibilities', 'pause'))
def project_responsibility_pause():
    from quart import request
    try:
        data = continuity_contract.parse('responsibilities', 'pause')
        service, principal = _responsibilities_service(data['projectPath'])
        return api_ok(service.pause(data, principal=principal,
                      command_id=request.headers.get('Idempotency-Key') or ''))
    except Exception as exc:
        return _safe_route_error_response(exc)


@api_v1_project_brain_bp.route('/api/v1/project/coordination/responsibilities/cancel', methods=['POST'])
@require_scope('chat')
@api_meta(summary='Cancel a revision-pinned responsibility and revoke its exact active children', tags=['project'],
          scope='chat', request_body=continuity_contract.request_body('responsibilities', 'cancel'))
def project_responsibility_cancel():
    from quart import request
    try:
        data = continuity_contract.parse('responsibilities', 'cancel')
        service, principal = _responsibilities_service(data['projectPath'])
        return api_ok(service.cancel(data, principal=principal,
                      command_id=request.headers.get('Idempotency-Key') or ''))
    except Exception as exc:
        return _safe_route_error_response(exc)


@api_v1_project_brain_bp.route('/api/v1/project/coordination/responsibilities/reconcile', methods=['POST'])
@require_scope('chat')
@api_meta(summary='Reconcile exact existing responsibility commands without renewed allowance', tags=['project'],
          scope='chat', request_body=continuity_contract.request_body('responsibilities', 'reconcile'))
def project_responsibility_reconcile():
    from quart import request
    try:
        data = continuity_contract.parse('responsibilities', 'reconcile')
        service, principal = _responsibilities_service(data['projectPath'])
        return api_ok(service.reconcile(data, principal=principal,
                      command_id=request.headers.get('Idempotency-Key') or ''))
    except Exception as exc:
        return _safe_route_error_response(exc)


@api_v1_project_brain_bp.route('/api/v1/project/coordination/shared-files', methods=['GET'])
@require_scope('chat')
@api_meta(summary='Read a bounded page of explicitly declared shared file versions', tags=['project'],
          scope='chat', parameters=_continuity_query_parameters('shared_files'))
def project_shared_files_list():
    try:
        query = _continuity_query('shared_files')
        service, principal = _shared_files_service(query['path'])
        return api_ok(service.list(principal=principal, cursor=query.get('cursor')))
    except Exception as exc:
        return _safe_route_error_response(exc)


@api_v1_project_brain_bp.route('/api/v1/project/coordination/shared-files/register', methods=['POST'])
@require_scope('chat')
@api_meta(summary='Register an explicit shared file with its source and current index revision', tags=['project'],
          scope='chat', request_body=continuity_contract.request_body('shared_files', 'register'))
def project_shared_file_register():
    from quart import request
    try:
        data = continuity_contract.parse('shared_files', 'register')
        service, principal = _shared_files_service(data['projectPath'])
        return api_ok(service.register(data, principal=principal,
                      command_id=request.headers.get('Idempotency-Key') or ''))
    except Exception as exc:
        return _safe_route_error_response(exc)


@api_v1_project_brain_bp.route('/api/v1/project/coordination/shared-files/read', methods=['POST'])
@require_scope('chat')
@api_meta(summary='Read an exact pinned shared file version as untrusted DATA only', tags=['project'],
          scope='chat', request_body=continuity_contract.request_body('shared_files', 'read'))
def project_shared_file_read():
    try:
        data = continuity_contract.parse('shared_files', 'read')
        service, principal = _shared_files_service(data['projectPath'])
        return api_ok(service.read(data, principal=principal))
    except Exception as exc:
        return _safe_route_error_response(exc)


@api_v1_project_brain_bp.route('/api/v1/project/coordination/shared-files/refresh', methods=['POST'])
@require_scope('chat')
@api_meta(summary='Refresh exact-version observations or record a human-authorized replacement path', tags=['project'],
          scope='chat', request_body=continuity_contract.request_body('shared_files', 'refresh'))
def project_shared_file_refresh():
    try:
        data = continuity_contract.parse('shared_files', 'refresh')
        service, principal = _shared_files_service(data['projectPath'])
        return api_ok(service.refresh(data, principal=principal))
    except Exception as exc:
        return _safe_route_error_response(exc)


__all__ = ['api_v1_project_brain_bp']
