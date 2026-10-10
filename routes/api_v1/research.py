"""Owner-scoped HTTP boundary for durable research artifacts and programs.

The blueprint serves immutable auto-research results plus the versioned
Research Foundry program, live provider-neutral capability catalog, safe LaTeX
scaffolding, and source ZIP export. Live execution remains on the generic task
API; persistence and normalization stay in ``lib.research``. An unknown
direction is a normal empty/``found=false`` result rather than an HTTP error.
"""

from __future__ import annotations

import asyncio
from quart import Blueprint, Response, request
from quart.wrappers.response import ResponseBody

from lib.api_v4 import raw_success_response
from lib.api_response import (
    api_bad_request,
    api_conflict,
    api_internal_error,
    api_ok,
    api_not_found,
)
from lib.storage.errors import StorageError
from lib.log import get_logger
from lib.openapi import media_responses, api_meta
from lib.request_parser import BadRequest, async_parse_body, parse_body

from .auth import request_user_id, require_auth

logger = get_logger(__name__)

api_v1_research_bp = Blueprint('api_v1_research', __name__)


@api_v1_research_bp.route('/api/v1/research/lookup', methods=['GET'])
@require_auth
@api_meta(
    summary='Look up persisted auto-research artifacts by direction',
    description=(
        'Returns the survey markdown, the open-gap map, the accepted ideas and '
        'the full rejection audit (with four-axis rubric scores) persisted for '
        'a research direction. Served from durable storage, so it keeps working '
        'after the in-memory task has been TTL-swept or the server restarted — '
        'unlike GET /api/v1/tasks/{id}, which is task-id addressed and '
        'in-memory only. An unresearched direction is a 200 with found=false.'),
    tags=['research'],
    parameters=[
        {'name': 'direction', 'in': 'query', 'required': True,
         'schema': {'type': 'string'},
         'description': 'The research direction (case/whitespace-insensitive).'},
        {'name': 'lang', 'in': 'query',
         'schema': {'type': 'string', 'default': 'en'}},
    ])
def research_lookup():
    """Serve persisted research artifacts for a direction."""
    direction = (request.args.get('direction') or '').strip()
    if not direction:
        return api_bad_request("'direction' is required and must be non-empty")
    lang = (request.args.get('lang') or 'en').strip() or 'en'
    try:
        from lib.research.persistence import load_research_artifacts
        artifacts = load_research_artifacts(
            direction, lang, user_id=int(request_user_id()))
    except Exception as e:
        logger.error('[api_v1.research] lookup failed for %.60s: %s',
                     direction, e, exc_info=True)
        return api_internal_error('internal_error')
    logger.info('[api_v1.research] lookup dir=%.60s lang=%s → found=%s '
                '(%d accepted / %d rejected)', direction, lang,
                artifacts.get('found'), len(artifacts.get('accepted') or []),
                len(artifacts.get('rejected') or []))
    return api_ok(artifacts)


@api_v1_research_bp.route('/api/v1/research/list', methods=['GET'])
@require_auth
@api_meta(
    summary='List every research direction that has artifacts on disk',
    description=(
        'Newest first, with accepted/rejected counts. This exists because the '
        'persisted rows are keyed by a ONE-WAY hash of the direction: without '
        'this index a user who forgot their exact original wording could never '
        'reach their own artifacts again, which is indistinguishable from the '
        'data having been deleted. The original text is recovered from the '
        'stored metadata.'),
    tags=['research'],
    parameters=[{'name': 'limit', 'in': 'query',
                 'schema': {'type': 'integer', 'default': 50}}])
def research_list():
    """Serve the index of researched directions."""
    try:
        limit = max(1, min(int(request.args.get('limit') or 50), 200))
    except (ValueError, TypeError) as e:
        logger.debug('[Research] bad limit arg, using default 50: %s', e)
        limit = 50
    try:
        from lib.research.persistence import list_research_directions
        items = list_research_directions(
            user_id=int(request_user_id()), limit=limit)
    except Exception as e:
        logger.error('[api_v1.research] list failed: %s', e, exc_info=True)
        return api_internal_error('internal_error')
    logger.info('[api_v1.research] list → %d direction(s)', len(items))
    return api_ok({'items': items, 'total': len(items)})


@api_v1_research_bp.route('/api/v1/research/workspace', methods=['GET'])
@require_auth
@api_meta(
    summary='Load a versioned Research Foundry production workspace',
    description=(
        'Returns the direction-scoped experiment protocol, run ledger, '
        'claim/evidence map and manuscript plan. A new direction returns the '
        'canonical empty workspace with revision zero.'),
    tags=['research'],
    parameters=[
        {'name': 'direction', 'in': 'query', 'required': True,
         'schema': {'type': 'string'}},
        {'name': 'lang', 'in': 'query',
         'schema': {'type': 'string', 'default': 'en'}},
        {'name': 'history_cursor', 'in': 'query',
         'description': '0 starts committed history; next_cursor selects an older bounded page.',
         'schema': {'type': 'integer', 'minimum': 0}},
    ])
def research_workspace_get():
    direction = (request.args.get('direction') or '').strip()
    if not direction:
        return api_bad_request("'direction' is required and must be non-empty")
    lang = (request.args.get('lang') or 'en').strip() or 'en'
    history_cursor = request.args.get('history_cursor')
    if history_cursor is not None:
        if not history_cursor.isascii() or not history_cursor.isdigit() or len(history_cursor) > 20:
            return api_bad_request('Invalid research history cursor')
        history_cursor = int(history_cursor)
    try:
        from lib.research.workspace import load_workspace_response
        result = load_workspace_response(direction, lang, user_id=int(request_user_id()),
                                         history_cursor=history_cursor)
    except ValueError as exc:
        return api_bad_request(str(exc))
    except Exception as exc:
        logger.error('[api_v1.research] workspace lookup failed: %s',
                     exc, exc_info=True)
        return api_internal_error('internal_error')
    return api_ok(result)


@api_v1_research_bp.route('/api/v1/research/workspace', methods=['PUT'])
@require_auth
@api_meta(
    summary='Commit one Research Foundry workspace revision',
    description=(
        'Optimistic compare-and-swap. expected_revision must match the '
        'currently stored revision; stale writers receive HTTP 409 and must '
        'reload rather than overwriting newer experiment evidence.'),
    tags=['research'])
async def research_workspace_put():
    try:
        body = await async_parse_body(strict=True)
    except BadRequest as exc:
        return api_bad_request(str(exc))
    direction = str(body.get('direction') or '').strip()
    workspace = body.get('workspace')
    expected_revision = body.get('expected_revision')
    if not direction or not isinstance(workspace, dict):
        return api_bad_request('direction and workspace are required')
    if (isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0):
        return api_bad_request('expected_revision must be a non-negative integer')
    try:
        from lib.research.workspace import save_workspace
        saved = save_workspace(
            direction, str(body.get('lang') or 'en'), workspace,
            expected_revision=expected_revision,
            user_id=int(request_user_id()),
        )
    except ValueError as exc:
        return api_bad_request(str(exc))
    except StorageError as exc:
        if exc.code == 'database_conflict':
            return api_conflict('research_workspace_stale')
        logger.error('[api_v1.research] workspace storage failed: %s',
                     exc, exc_info=True)
        return api_internal_error('internal_error')
    except Exception as exc:
        logger.error('[api_v1.research] workspace commit failed: %s',
                     exc, exc_info=True)
        return api_internal_error('internal_error')
    from lib.research.program import readiness
    return api_ok({'workspace': saved, 'readiness': readiness(saved)})


@api_v1_research_bp.route('/api/v1/research/workspace/draft-protocol', methods=['POST'])
@require_auth
@api_meta(
    summary='Draft a frozen experiment protocol from one accepted idea',
    request_body={'required': True, 'content': {'application/json': {'schema': {
        'type': 'object', 'required': ['direction', 'idea_id'], 'properties': {
            'direction': {'type': 'string'}, 'lang': {'type': 'string', 'enum': ['en', 'zh']},
            'idea_id': {'type': 'string'}, 'model': {'type': 'string'},
            'source_task_id': {'type': 'string', 'minLength': 1, 'maxLength': 160,
                'description': 'Owned completed research task supplying the exact original evidence.'},
        }}}}},
    description=(
        'One bounded, tool-less model call grounded in the persisted survey, '
        'open-gap map and the selected accepted idea. The draft is a proposal '
        'for the owner to review; nothing is persisted — authority stays with '
        'the versioned workspace PUT. Sync handler so the blocking model call '
        'runs off the event loop.'),
    tags=['research'])
def research_workspace_draft_protocol():
    try:
        body = parse_body(strict=True)
    except BadRequest as exc:
        return api_bad_request(str(exc))
    direction = str(body.get('direction') or '').strip()
    idea_id = str(body.get('idea_id') or '').strip()
    if not direction or not idea_id:
        return api_bad_request('direction and idea_id are required')
    if 'source_task_id' in body and (not isinstance(body['source_task_id'], str) or not body['source_task_id']):
        return api_bad_request('source_task_id must identify a saved research task')
    try:
        from lib.research.draft import draft_protocol
        draft = draft_protocol(
            direction, str(body.get('lang') or 'en'), idea_id,
            user_id=int(request_user_id()),
            model=str(body.get('model') or ''),
            source_task_id=body.get('source_task_id'))
    except ValueError as exc:
        return api_bad_request(str(exc))
    except Exception as exc:
        logger.error('[api_v1.research] protocol draft failed: %s',
                     exc, exc_info=True)
        return api_internal_error('internal_error')
    return api_ok({'draft': draft})


@api_v1_research_bp.route('/api/v1/research/capabilities', methods=['GET'])
@require_auth
@api_meta(
    summary='Discover live provider-neutral research capabilities',
    description=(
        'Projects every enabled MCP tool into a common research capability '
        'catalog. Suggestions are descriptive only; execution requires an '
        'exact capability binding saved in the owner-scoped workspace.'),
    tags=['research'])
def research_capabilities_get():
    try:
        from lib.research.capabilities import build_capability_catalog
        catalog = build_capability_catalog(user_id=int(request_user_id()))
    except Exception as exc:
        logger.error('[api_v1.research] capability discovery failed: %s',
                     exc, exc_info=True)
        return api_internal_error('internal_error')
    return api_ok({'catalog': catalog})


@api_v1_research_bp.route('/api/v1/research/manuscript/scaffold', methods=['POST'])
@require_auth
@api_meta(
    summary='Create a bounded conference-paper LaTeX source tree',
    description=(
        'Adds missing source files without overwriting edited files, then '
        'commits with the same optimistic revision guard as workspace PUT.'),
    tags=['research'])
async def research_manuscript_scaffold():
    try:
        body = await async_parse_body(strict=True)
    except BadRequest as exc:
        return api_bad_request(str(exc))
    direction = str(body.get('direction') or '').strip()
    workspace = body.get('workspace')
    expected_revision = body.get('expected_revision')
    if not direction or not isinstance(workspace, dict):
        return api_bad_request('direction and workspace are required')
    if (isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0):
        return api_bad_request('expected_revision must be a non-negative integer')
    try:
        from lib.research.manuscript import scaffold_source_files
        from lib.research.program import readiness
        from lib.research.workspace import save_workspace
        draft = dict(workspace)
        draft['source_files'] = scaffold_source_files(draft)
        draft['stage'] = 'writing'
        saved = save_workspace(
            direction, str(body.get('lang') or 'en'), draft,
            expected_revision=expected_revision,
            user_id=int(request_user_id()),
        )
    except ValueError as exc:
        return api_bad_request(str(exc))
    except StorageError as exc:
        if exc.code == 'database_conflict':
            return api_conflict('research_workspace_stale')
        logger.error('[api_v1.research] manuscript scaffold storage failed: %s',
                     exc, exc_info=True)
        return api_internal_error('internal_error')
    except Exception as exc:
        logger.error('[api_v1.research] manuscript scaffold failed: %s',
                     exc, exc_info=True)
        return api_internal_error('internal_error')
    return api_ok({'workspace': saved, 'readiness': readiness(saved)})


@api_v1_research_bp.route('/api/v1/research/manuscript/source.zip', methods=['GET'])
@require_auth
@api_meta(
    responses=media_responses('application/zip'),

    summary='Export the current normalized LaTeX source tree as ZIP',
    description='Streams only safe relative source paths from the owner workspace.',
    tags=['research'])
@raw_success_response
def research_manuscript_export():
    direction = (request.args.get('direction') or '').strip()
    if not direction:
        return api_bad_request("'direction' is required and must be non-empty")
    lang = (request.args.get('lang') or 'en').strip() or 'en'
    try:
        from lib.research.manuscript import export_source_zip
        from lib.research.workspace import load_workspace
        workspace = load_workspace(
            direction, lang, user_id=int(request_user_id()))
        if not workspace.get('source_files'):
            return api_bad_request('manuscript source tree is empty')
        archive = export_source_zip(workspace)
    except Exception as exc:
        logger.error('[api_v1.research] manuscript export failed: %s',
                     exc, exc_info=True)
        return api_internal_error('internal_error')
    return Response(
        archive,
        status=200,
        content_type='application/zip',
        headers={'Content-Disposition':
                 'attachment; filename="tofu-research-source.zip"'},
    )


@api_v1_research_bp.route('/api/v1/research/records', methods=['GET'])
@require_auth
@api_meta(summary='List durable Research run records', tags=['research'])
def research_records_list():
    from lib.research.records import list_records
    try:
        limit = max(1, min(200, int(request.args.get('limit') or 50)))
    except ValueError:
        return api_bad_request('limit must be an integer')
    items = list_records(user_id=int(request_user_id()), limit=limit)
    return api_ok({'items': items, 'total': len(items)})


@api_v1_research_bp.route('/api/v1/research/records/<task_id>', methods=['GET'])
@require_auth
@api_meta(summary='Reopen a durable Research run snapshot', tags=['research'])
def research_record_get(task_id):
    from lib.research.records import load_record
    return api_ok({'record': load_record(task_id, user_id=int(request_user_id()))})


@api_v1_research_bp.route('/api/v1/research/actions/<task_id>/observations', methods=['GET'])
@require_auth
@api_meta(summary='Recover persisted experiment observation manifests', tags=['research'],
    description='Owner-scoped read-only recovery after task loss; original parts and scientific verdicts are not loaded.',
    parameters=[{'name': 'task_id', 'in': 'path', 'required': True, 'schema': {'type': 'string'}},
                {'name': 'limit', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 1, 'maximum': 16, 'default': 8}},
                {'name': 'cursor', 'in': 'query', 'schema': {'type': 'string'}}])
def research_action_observations(task_id):
    from lib.research.action_observations import load_action_observation_page
    try:
        limit = int(request.args.get('limit', '8'))
        return api_ok(load_action_observation_page(task_id,
            user_id=int(request_user_id()), limit=limit, cursor=request.args.get('cursor', '')))
    except ValueError as exc:
        return api_bad_request(str(exc))


@api_v1_research_bp.route('/api/v1/research/actions/<task_id>/effects', methods=['GET'])
@require_auth
@api_meta(summary='Inspect durable Research tool intents and unresolved effects', tags=['research'],
    description='Read-only owner-scoped recovery. Missing observations remain unknown; neither state grants replay rights.',
    parameters=[{'name': 'task_id', 'in': 'path', 'required': True, 'schema': {'type': 'string'}},
                {'name': 'limit', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 1, 'maximum': 16, 'default': 8}},
                {'name': 'cursor', 'in': 'query', 'schema': {'type': 'string'}}])
def research_action_effects(task_id):
    from lib.research.action_observations import load_action_effect_page
    try:
        return api_ok(load_action_effect_page(task_id, user_id=int(request_user_id()),
            limit=int(request.args.get('limit', '8')), cursor=request.args.get('cursor', '')))
    except ValueError as exc:
        return api_bad_request(str(exc))


@api_v1_research_bp.route('/api/v1/research/actions/<task_id>/model-calls', methods=['GET'])
@require_auth
@api_meta(summary='Inspect durable Research model call and usage checkpoints', tags=['research'],
    description='Read-only logical dispatch accounting. Unknown usage stays unknown and replay is not authorized.',
    parameters=[{'name': 'task_id', 'in': 'path', 'required': True, 'schema': {'type': 'string'}},
                {'name': 'limit', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 1, 'maximum': 16, 'default': 8}},
                {'name': 'cursor', 'in': 'query', 'schema': {'type': 'string'}}])
def research_model_calls(task_id):
    from lib.research.model_accounting import load_model_calls_page
    try:
        return api_ok(load_model_calls_page(task_id, user_id=int(request_user_id()),
            limit=int(request.args.get('limit', '8')), cursor=request.args.get('cursor', '')))
    except ValueError as exc:
        return api_bad_request(str(exc))


@api_v1_research_bp.route('/api/v1/research/actions/<task_id>/terminal', methods=['GET'])
@require_auth
@api_meta(summary='Read a durable Research action settlement receipt', tags=['research'],
    description='Owner-scoped terminal fact and committed workspace revision after task eviction; never authorizes replay.')
def research_action_terminal(task_id):
    from lib.research.action_terminal import load_action_terminal
    try:
        return api_ok(load_action_terminal(task_id, user_id=int(request_user_id())))
    except ValueError as exc:
        return api_bad_request(str(exc))


class _VerifiedObservationBody(ResponseBody):
    """One-request verified spool; close also covers an unconsumed response."""
    def __init__(self, spool):
        self.spool = spool
        self.pending = None

    async def __aenter__(self):
        return self

    async def __aiter__(self):
        while True:
            self.pending = asyncio.create_task(asyncio.to_thread(self.spool.read, 65536))
            chunk = await asyncio.shield(self.pending)
            self.pending = None
            if not chunk:
                return
            yield chunk

    async def __aexit__(self, exc_type, exc_value, traceback):
        cancelled = False
        async def settle(task):
            nonlocal cancelled
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled = True
                except Exception:
                    break
        if self.pending is not None:
            await settle(self.pending)
            # The iterator already owns any read error; teardown must still close.
            if not self.pending.cancelled():
                self.pending.exception()
            self.pending = None
        closing = asyncio.create_task(asyncio.to_thread(self.spool.close))
        await settle(closing)
        closing.result()
        if cancelled and exc_type is None:
            raise asyncio.CancelledError


@api_v1_research_bp.route('/api/v1/research/actions/<task_id>/observations/<int:ordinal>/original', methods=['GET'])
@require_auth
@api_meta(summary='Download a verified original experiment observation', tags=['research'],
    description='Verifies all immutable parts before streaming UTF-8 text; no execution or scientific verdict.',
    responses=media_responses('text/plain'))
@raw_success_response
def research_action_observation_original(task_id, ordinal):
    from lib.research.action_observations import prepare_action_observation_export
    try:
        prepared = prepare_action_observation_export(task_id, ordinal, user_id=int(request_user_id()))
    except ValueError as exc:
        return api_bad_request(str(exc))
    if prepared is None:
        return api_not_found('research_observation_not_found')
    manifest, spool, byte_count = prepared
    try:
        response = Response(_VerifiedObservationBody(spool), content_type='text/plain; charset=utf-8',
            headers={'Content-Disposition': f'attachment; filename="research-observation-{ordinal}.txt"',
                     'ETag': '"' + manifest['result_digest'] + '"',
                     'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff'})
        response.content_length = byte_count
        return response
    except BaseException:
        spool.close()
        raise


@api_v1_research_bp.route('/api/v1/research/records/<task_id>/resume', methods=['POST'])
@require_auth
@api_meta(summary='Explicitly resume interrupted Research checkpoints', tags=['research'])
def research_record_resume(task_id):
    from lib.research.records import resume_record
    try:
        return api_ok(resume_record(task_id, user_id=int(request_user_id())))
    except ValueError as exc:
        return api_bad_request(str(exc))


@api_v1_research_bp.route('/api/v1/research/actions/<task_id>/resume', methods=['POST'])
@require_auth
@api_meta(summary='Continue an owned interrupted Research action with its original budget', tags=['research'],
    description='Selects the latest complete unpaid frontier. Proven process death, original workspace/tool policy and complete retained usage are required. No historical tool is replayed.',
    request_body={'required': True, 'content': {'application/json': {'schema': {
        'type': 'object', 'additionalProperties': False, 'properties': {}}}}})
def research_action_resume(task_id):
    try:
        body = parse_body(strict=True)
    except BadRequest as exc:
        return api_bad_request(str(exc))
    if body:
        return api_bad_request('Resume uses the original task policy; replacement fields are not accepted')
    from lib.research.action_continuation import resume_action
    try:
        return api_ok(resume_action(task_id, user_id=int(request_user_id())))
    except LookupError:
        return api_not_found('Research action not found')
    except ValueError as exc:
        return api_bad_request(str(exc))
    except StorageError as exc:
        if exc.code == 'database_conflict':
            return api_conflict(str(exc), error_code='research_resume_conflict')
        raise


@api_v1_research_bp.route('/api/v1/research/actions/unfinished', methods=['GET'])
@require_auth
@api_meta(summary='Discover owned unfinished actions for a Research workspace', tags=['research'],
    description='Read-only scoped metadata after registry loss. Unknown execution evidence remains unknown; two candidates indicate ambiguity. Never authorizes replay.',
    parameters=[{'name':'direction','in':'query','required':True,'schema':{'type':'string','minLength':1,'maxLength':2000}},
                {'name':'lang','in':'query','schema':{'type':'string','enum':['en','zh'],'default':'en'}}])
def research_unfinished_actions():
    from lib.research.action_discovery import discover_unfinished_actions
    try:
        return api_ok(discover_unfinished_actions(request.args.get('direction'),
            lang=request.args.get('lang','en'),user_id=int(request_user_id())))
    except ValueError as exc:
        return api_bad_request(str(exc))


__all__ = ['api_v1_research_bp']
