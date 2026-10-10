"""Open owned retained manuscripts and save reading state without compiling.

Conversation settings authorize source access; the document repository owns
saved identity, build receipts and reading revisions. Source unavailability
disables compilation but never revokes the owner's retained manuscript.
"""
from __future__ import annotations

from .contract import validate
from .document_identity import source_binding_key
from .document_repository import PaperDocumentRepository
from .errors import BuildError
from .service import artifact_url, owned_settings


def _can_compile(source, settings, owner_user_id):
    from .documents import _references, _root, _safe
    try:
        if source['source'] == 'overleaf':
            from .overleaf_capabilities import discover_sources
            sources, _ = discover_sources(settings, owner_user_id=owner_user_id)
            return any(source_binding_key(value) == source_binding_key(source) for value in sources)
        if source['project_root'] not in _references(settings):
            return False
        if source['project_root'].startswith('remote:'):
            from lib.desktop.remote import parse_remote_path, validate_remote_binding
            from lib.desktop import online_agents
            agent_id, root = parse_remote_path(source['project_root'])
            binding, error = validate_remote_binding(agent_id, root, user_id=str(owner_user_id))
            if error or binding is None:
                return False
            return any(row.get('agent_id') == agent_id and str(row.get('user_id')) == str(owner_user_id)
                       and type(row.get('capabilities', {}).get('paper_build')) is int
                       and row['capabilities']['paper_build'] == 1 for row in online_agents())
        root = _root(source['project_root'])
        return _safe(root, source['main_file']).is_file()
    except (BuildError, OSError, ValueError, RuntimeError):
        return False


def open_document(payload, *, owner_user_id):
    validate(payload, 'PaperDocumentOpenRequestV1')
    settings = owned_settings(payload['conv_id'], owner_user_id)
    papers = PaperDocumentRepository(owner_user_id)
    source = payload.get('source', settings.get('paperDocument'))
    if payload.get('document_id'):
        record = papers.get(payload['document_id'])
        if payload.get('source') and source_binding_key(payload['source']) != source_binding_key(record['source']):
            raise BuildError('document_not_found', 'Paper document does not match the requested source')
    elif source is not None:
        validate(source, 'PaperDocumentV1')
        identity = papers.resolve([source])[0]
        if identity is None:
            return None
        if source.get('document_id') and source['document_id'] != identity:
            raise BuildError('document_not_found', 'Paper document does not match the saved source')
        record = papers.get(identity)
    else:
        record = papers.current(payload['conv_id'])
    if record is None:
        return None
    if record['source']['source'] == 'research':
        raise BuildError('document_not_found', 'Open this manuscript from its Research workspace')
    receipt = record.pop('receipt')
    build = ({'receipt': receipt, 'superseded': False, 'retained': True,
              'pdf_url': artifact_url(receipt['document_id'], receipt['build_id'], 'document.pdf')}
             if receipt else None)
    return validate({**record, 'build': build,
                     'can_compile': _can_compile(record['source'], settings, owner_user_id)},
                    'PaperDocumentRecordV1')


def save_reading(document_id, payload, *, owner_user_id):
    validate(payload, 'PaperReadingStateRequestV1')
    result = PaperDocumentRepository(owner_user_id).save_reading(
        document_id, payload['state'], payload['expected_revision'],
        writer_id=payload['writer_id'], sequence=payload['sequence'])
    return validate(result, 'PaperReadingStateResultV1')


def open_research_reading(payload, *, owner_user_id):
    """Resolve reading metadata through the workspace's owned publication.

    No source discovery, compile, or byte hydration occurs. The shared register
    command admits one catalog record, including for a previously retained PDF.
    """
    from lib.research.persistence import _direction_read_candidates
    from lib.storage.errors import StorageError
    validate(payload, 'PaperResearchReadingOpenRequestV1')
    papers = PaperDocumentRepository(owner_user_id)
    for paper_hash, _ in _direction_read_candidates(payload['direction']):
        try:
            record = papers.register({'source': 'research', 'paper_hash': paper_hash,
                'lang': payload['lang'], 'document_id': payload['document_id']})
        except StorageError as exc:
            if exc.code == 'database_not_found':
                continue
            raise
        return validate({key: record[key] for key in ('document_id', 'reading', 'reading_revision')},
                        'PaperReadingRecordV1')
    raise BuildError('document_not_found', 'Research manuscript is unavailable')
