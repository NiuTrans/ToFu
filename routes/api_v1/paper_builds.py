"""Paper build commands and immutable owner-scoped artifact reads.

Wire authority: contracts/paper_build_v1.schema.json. The existing generic
task API owns polling, events and explicit abort. Viewer detach releases only
that subscriber; it is distinct from aborting every consumer's task.
"""
from __future__ import annotations

from quart import Blueprint

from lib.api_v4 import raw_success_response
from lib.api_response import api_error, api_not_found, api_ok, api_payload
from lib.openapi import media_responses, api_meta, schema_definition
from lib.paper.builds.contract import limit, schema, validate
from lib.paper.builds.errors import BuildError
from lib.paper.builds.documents import discover_documents, select_document
from lib.paper.builds.runtime import get_scheduler
from lib.paper.builds.service import locate_build, locate_inverse_build, start_preview
from lib.paper.builds.saved_documents import open_document, open_research_reading, save_reading
from lib.request_parser import parse_body
from lib.storage.errors import StorageError, http_status_for_storage_error

from .auth import request_user_id, require_scope

api_v1_paper_builds_bp = Blueprint('api_v1_paper_builds', __name__)


def _body(definition):
    return {'required': True, 'content': {'application/json': {
        'schema': schema_definition(schema(), definition)}}}


def _fields(definition):
    # Field admission mirrors the api_meta RequestV1 contract exactly (one
    # schema authority) — no bare parse, no second field list to drift.
    # anyOf/allOf/oneOf compositions and local $refs resolve through $defs.
    defs = schema()['$defs']
    fields = set()

    def _collect(node):
        if not isinstance(node, dict):
            return
        ref = node.get('$ref')
        if isinstance(ref, str) and ref.startswith('#/$defs/'):
            _collect(defs[ref.rsplit('/', 1)[-1]])
        for key in ('anyOf', 'allOf', 'oneOf'):
            for branch in node.get(key) or []:
                _collect(branch)
        fields.update(node.get('properties') or {})

    _collect(defs[definition])
    return tuple(sorted(fields))


def _failure(exc: BuildError | StorageError):
    """Map a paper-build failure to its HTTP response.

    Every branch preserves the real ``code`` and diagnostic message on the wire
    (via ``kind`` and the error body) so the frontend can render a specific,
    copyable message instead of one opaque ``paper_build_unavailable`` string.
    """
    # ``error_code`` is the route's deliberate wire contract: the v4 boundary
    # honors it as the RFC 7807 ``code`` and the frontend retry classifier reads
    # it, so the specific failure (database_not_found, build_busy, …) reaches the
    # browser instead of a status-table fallback like ``not_found``.
    if isinstance(exc, StorageError):
        # Route storage failures through the shared storage→HTTP taxonomy so
        # the status (404/409/503/…) matches the real code, then carry the
        # specific code + diagnostic message on the body for debugging.
        return api_error(str(exc), status=http_status_for_storage_error(exc),
                         kind=exc.code, error_code=exc.code)
    if exc.code in {'document_not_found', 'build_not_found', 'proposal_not_found'}:
        return api_error(str(exc), status=404, kind=exc.code, error_code=exc.code)
    status = 503 if exc.code in {'build_busy', 'cache_busy', 'cache_limit', 'build_stopped', 'desktop_unavailable', 'overleaf_timeout', 'overleaf_unavailable'} else 400
    if exc.code in {'desktop_capability', 'overleaf_capability', 'source_conflict'}:
        status = 409
    return api_error(str(exc), status=status, kind=exc.code, error_code=exc.code)


@api_v1_paper_builds_bp.route('/api/v1/paper/documents/overleaf/select', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Bind an Overleaf project and exact export capabilities to an owned conversation',
          tags=['paper'], scope='tasks', request_body=_body('PaperOverleafSelectRequestV1'))
def select_overleaf_source():
    from lib.paper.builds.overleaf_capabilities import select_source
    try:
        return api_ok(select_source(parse_body(exact=_fields('PaperOverleafSelectRequestV1')), owner_user_id=request_user_id()))
    except (BuildError, StorageError) as exc:
        return _failure(exc)


@api_v1_paper_builds_bp.route('/api/v1/paper/documents/overleaf/remove', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Remove an owned Overleaf project binding', tags=['paper'], scope='tasks',
          request_body=_body('PaperOverleafRemoveRequestV1'))
def remove_overleaf_source():
    from lib.paper.builds.overleaf_capabilities import remove_source
    try:
        remove_source(parse_body(exact=_fields('PaperOverleafRemoveRequestV1')), owner_user_id=request_user_id())
        return api_ok({'removed': True})
    except (BuildError, StorageError) as exc:
        return _failure(exc)


@api_v1_paper_builds_bp.route('/api/v1/paper/documents/discover', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Discover owned TeX documents without compiling', tags=['paper'], scope='tasks',
          request_body=_body('PaperDocumentDiscoveryRequestV1'))
def discover_paper_documents():
    try:
        return api_ok(discover_documents(parse_body(exact=_fields('PaperDocumentDiscoveryRequestV1')), owner_user_id=request_user_id()))
    except (BuildError, StorageError) as exc:
        return _failure(exc)


@api_v1_paper_builds_bp.route('/api/v1/paper/documents/select', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Persist an owned paper document selection', tags=['paper'], scope='tasks',
          request_body=_body('PaperDocumentSelectionRequestV1'))
def select_paper_document():
    try:
        return api_ok(select_document(parse_body(exact=_fields('PaperDocumentSelectionRequestV1')), owner_user_id=request_user_id()))
    except (BuildError, StorageError) as exc:
        return _failure(exc)


@api_v1_paper_builds_bp.route('/api/v1/paper/documents/open', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Open an owned retained manuscript without compiling', tags=['paper'], scope='tasks',
          request_body=_body('PaperDocumentOpenRequestV1'))
def open_paper_document():
    try:
        return api_ok(document=open_document(parse_body(exact=_fields('PaperDocumentOpenRequestV1')), owner_user_id=request_user_id()))
    except (BuildError, StorageError) as exc:
        return _failure(exc)


@api_v1_paper_builds_bp.route('/api/v1/paper/documents/research/open', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Open owned Research manuscript reading metadata without compiling',
          tags=['paper'], scope='tasks', request_body=_body('PaperResearchReadingOpenRequestV1'))
def open_research_paper_reading():
    try:
        return api_ok(document=open_research_reading(parse_body(exact=_fields('PaperResearchReadingOpenRequestV1')), owner_user_id=request_user_id()))
    except (BuildError, StorageError) as exc:
        return _failure(exc)


@api_v1_paper_builds_bp.route('/api/v1/paper/documents/<document_id>/reading', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Save owned manuscript reading state with a revision fence', tags=['paper'], scope='tasks',
          request_body=_body('PaperReadingStateRequestV1'))
def save_paper_reading(document_id):
    try:
        return api_ok(save_reading(document_id, parse_body(exact=_fields('PaperReadingStateRequestV1')), owner_user_id=request_user_id()))
    except (BuildError, StorageError) as exc:
        return _failure(exc)


@api_v1_paper_builds_bp.route('/api/v1/paper/documents/source', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Read one owned paper source file for the read-only code view',
          tags=['paper'], scope='tasks', request_body=_body('PaperDocumentSourceRequestV1'))
def read_paper_document_source():
    from lib.paper.builds.source_read import read_document_source
    try:
        return api_ok(read_document_source(parse_body(exact=_fields('PaperDocumentSourceRequestV1')), owner_user_id=request_user_id()))
    except (BuildError, StorageError) as exc:
        return _failure(exc)


@api_v1_paper_builds_bp.route('/api/v1/paper/documents/source/edit', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Apply a journaled edit to one owned paper source file',
          tags=['paper'], scope='tasks', request_body=_body('PaperDocumentSourceEditRequestV1'))
def edit_paper_document_source():
    from lib.paper.builds.source_edit import edit_document_source
    try:
        return api_ok(edit_document_source(parse_body(exact=_fields('PaperDocumentSourceEditRequestV1')), owner_user_id=request_user_id()))
    except (BuildError, StorageError) as exc:
        return _failure(exc)


@api_v1_paper_builds_bp.route('/api/v1/paper/proposals/get', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Read one owned paper edit proposal for the review panel',
          tags=['paper'], scope='tasks', request_body=_body('PaperProposalRequestV1'))
def get_paper_edit_proposal():
    from lib.paper.builds.proposals import get_proposal
    try:
        body = validate(parse_body(exact=_fields('PaperProposalRequestV1')), 'PaperProposalRequestV1')
        proposal = get_proposal(body['proposal_id'], owner_user_id=request_user_id())
        if proposal is None:
            raise BuildError('proposal_not_found', 'Paper edit proposal is unavailable')
        return api_ok(proposal)
    except (BuildError, StorageError) as exc:
        return _failure(exc)


@api_v1_paper_builds_bp.route('/api/v1/paper/proposals/accept', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Accept selected hunks of an owned paper edit proposal (journaled writes)',
          tags=['paper'], scope='tasks', request_body=_body('PaperProposalRequestV1'))
def accept_paper_edit_proposal():
    from lib.paper.builds.proposals import decide_proposal
    try:
        body = validate(parse_body(exact=_fields('PaperProposalRequestV1')), 'PaperProposalRequestV1')
        outcome = decide_proposal(body['proposal_id'], body.get('indexes'), 'accept',
                                  owner_user_id=request_user_id())
        return api_ok({**outcome['proposal'], 'results': outcome['results']})
    except (BuildError, StorageError) as exc:
        return _failure(exc)


@api_v1_paper_builds_bp.route('/api/v1/paper/proposals/reject', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Reject selected hunks of an owned paper edit proposal',
          tags=['paper'], scope='tasks', request_body=_body('PaperProposalRequestV1'))
def reject_paper_edit_proposal():
    from lib.paper.builds.proposals import decide_proposal
    try:
        body = validate(parse_body(exact=_fields('PaperProposalRequestV1')), 'PaperProposalRequestV1')
        outcome = decide_proposal(body['proposal_id'], body.get('indexes'), 'reject',
                                  owner_user_id=request_user_id())
        return api_ok(outcome['proposal'])
    except (BuildError, StorageError) as exc:
        return _failure(exc)


@api_v1_paper_builds_bp.route('/api/v1/paper/documents/arxiv/import', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Import an arXiv e-print source archive into an owned conversation root',
          tags=['paper'], scope='tasks', request_body=_body('PaperArxivImportRequestV1'))
def import_arxiv_paper_source():
    from lib.paper.builds.imports import import_arxiv_source
    try:
        return api_ok(import_arxiv_source(parse_body(exact=_fields('PaperArxivImportRequestV1')), owner_user_id=request_user_id()))
    except (BuildError, StorageError) as exc:
        return _failure(exc)


@api_v1_paper_builds_bp.route('/api/v1/paper/builds', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Start or join an owned paper preview build', tags=['paper'], scope='tasks',
          request_body=_body('PaperBuildRequestV1'))
def start_paper_build():
    try:
        return api_payload({'ok': True, **start_preview(parse_body(exact=_fields('PaperBuildRequestV1')), owner_user_id=request_user_id())}, status=202)
    except (BuildError, StorageError) as exc:
        return _failure(exc)


@api_v1_paper_builds_bp.route('/api/v1/paper/builds/tasks/<task_id>/detach', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Detach one paper preview subscriber', tags=['paper'], scope='tasks',
          request_body=_body('PaperBuildDetachV1'))
def detach_paper_build(task_id):
    try:
        data = validate(parse_body(exact=_fields('PaperBuildDetachV1')), 'PaperBuildDetachV1')
        detached = get_scheduler().detach(task_id, owner_user_id=request_user_id(), **data)
        return api_ok({'detached': detached})
    except (BuildError, StorageError) as exc:
        return _failure(exc)


@api_v1_paper_builds_bp.route('/api/v1/paper/builds/tasks/<task_id>/renew', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Renew an active paper preview subscription', tags=['paper'], scope='tasks',
          request_body=_body('PaperBuildDetachV1'))
def renew_paper_build(task_id):
    try:
        data = validate(parse_body(exact=_fields('PaperBuildDetachV1')), 'PaperBuildDetachV1')
        return api_ok({'renewed': get_scheduler().renew(task_id, owner_user_id=request_user_id(), **data)})
    except (BuildError, StorageError) as exc:
        return _failure(exc)


@api_v1_paper_builds_bp.route(
    '/api/v1/paper/builds/<document_id>/<build_id>/viewers', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Retain, renew or release an owned PDF reader reservation', tags=['paper'], scope='tasks',
          request_body=_body('PaperBuildViewerRequestV1'))
def update_paper_build_viewer(document_id, build_id):
    try:
        body = parse_body(exact=_fields('PaperBuildViewerRequestV1'))
        validate(body, 'PaperBuildViewerRequestV1')
        from lib.paper.builds.artifact_repository import PaperBuildArtifactRepository
        return api_ok(PaperBuildArtifactRepository(request_user_id()).viewer(
            get_scheduler().cache, document_id, build_id, body))
    except (BuildError, StorageError) as exc:
        return _failure(exc)


@api_v1_paper_builds_bp.route(
    '/api/v1/paper/builds/<document_id>/<build_id>/location', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Locate a source line in an existing paper build', tags=['paper'], scope='tasks',
          request_body=_body('PaperBuildLocationRequestV1'))
def locate_paper_build(document_id, build_id):
    try:
        return api_ok(locate_build(document_id, build_id, parse_body(exact=_fields('PaperBuildLocationRequestV1')), owner_user_id=request_user_id()))
    except (BuildError, StorageError) as exc:
        return _failure(exc)


@api_v1_paper_builds_bp.route(
    '/api/v1/paper/builds/<document_id>/<build_id>/location/inverse', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Map a PDF point back to a paper source line (inverse SyncTeX)',
          tags=['paper'], scope='tasks', request_body=_body('PaperBuildInverseLocationRequestV1'))
def locate_paper_build_inverse(document_id, build_id):
    try:
        return api_ok(locate_inverse_build(document_id, build_id, parse_body(exact=_fields('PaperBuildInverseLocationRequestV1')),
                                           owner_user_id=request_user_id(),
                                           scheduler=get_scheduler()))
    except (BuildError, StorageError) as exc:
        return _failure(exc)


@api_v1_paper_builds_bp.route(
    '/api/v1/paper/builds/<document_id>/<build_id>/layout', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Page map, section map and last-page fullness from one verified build',
          tags=['paper'], scope='tasks')
def paper_build_layout(document_id, build_id):
    from lib.paper.builds.document_repository import PaperDocumentRepository
    from lib.paper.builds.layout import document_layout
    try:
        document = PaperDocumentRepository(request_user_id()).get(document_id)
        return api_ok(document_layout(document, build_id, owner_user_id=request_user_id(),
                                      scheduler=get_scheduler()))
    except (BuildError, StorageError) as exc:
        return _failure(exc)


@api_v1_paper_builds_bp.route(
    '/api/v1/paper/builds/<document_id>/<build_id>/artifacts/<name>', methods=['GET'])
@require_scope('tasks')
@api_meta(
    responses=media_responses('application/pdf', 'application/gzip', 'text/plain', 'application/json', conditional=True),
)
@raw_success_response
def read_paper_build_artifact(document_id, build_id, name):
    from lib.file_serving import send_leased_artifact
    from lib.paper.builds.artifact_repository import PaperBuildArtifactRepository
    media_types = {'document.pdf': 'application/pdf', 'document.synctex.gz': 'application/gzip',
                   'build.log': 'text/plain', 'sources.json': 'application/json'}
    if name not in media_types:
        return api_not_found('paper_build_unavailable')
    try:
        lease = PaperBuildArtifactRepository(request_user_id()).open_artifact(
            get_scheduler().cache, document_id, build_id, name)
        return send_leased_artifact(lease, mimetype=media_types[name], buffer_size=limit('transfer_chunk_bytes'))
    except (BuildError, StorageError) as exc:
        return _failure(exc)
