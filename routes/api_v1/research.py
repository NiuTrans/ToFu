"""Owner-scoped HTTP boundary for durable research artifacts and programs.

The blueprint serves immutable auto-research results plus the versioned
Research Foundry program, live provider-neutral capability catalog, safe LaTeX
scaffolding, and source ZIP export. Live execution remains on the generic task
API; persistence and normalization stay in ``lib.research``. An unknown
direction is a normal empty/``found=false`` result rather than an HTTP error.
"""

from __future__ import annotations

from quart import Blueprint, Response, request

from lib.api_response import (
    api_bad_request,
    api_conflict,
    api_internal_error,
    api_ok,
)
from lib.storage.errors import StorageError
from lib.log import get_logger
from lib.openapi import api_meta
from lib.request_parser import BadRequest, async_parse_body

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
    ])
def research_workspace_get():
    direction = (request.args.get('direction') or '').strip()
    if not direction:
        return api_bad_request("'direction' is required and must be non-empty")
    lang = (request.args.get('lang') or 'en').strip() or 'en'
    try:
        from lib.research.program import readiness
        from lib.research.workspace import load_workspace
        workspace = load_workspace(
            direction, lang, user_id=int(request_user_id()))
    except Exception as exc:
        logger.error('[api_v1.research] workspace lookup failed: %s',
                     exc, exc_info=True)
        return api_internal_error('internal_error')
    return api_ok({'workspace': workspace, 'readiness': readiness(workspace)})


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
    summary='Export the current normalized LaTeX source tree as ZIP',
    description='Streams only safe relative source paths from the owner workspace.',
    tags=['research'])
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


__all__ = ['api_v1_research_bp']
