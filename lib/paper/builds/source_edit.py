"""Journaled edits to one owned paper source file (the browser editing lane).

Authority mirrors source_read: the durable document binding (never a client
path) resolves the target through the same symlink-refusing helpers and text
suffix gates, and reads stay inside the source-view byte cap. Every mutation
funnels through the journaled project write tools, so a browser edit appears
in the file-changes bar and is undoable exactly like an agent edit. Optimistic
concurrency is a client-supplied sha256 of the bytes it last read.
"""
from __future__ import annotations

import hashlib

from .contract import limit, schema, validate
from .documents import _root, _safe
from .errors import BuildError
from .source_read import _TEXT_SUFFIXES
from .sources import read_source


def resolve_local_document(document_id: str, *, owner_user_id: int) -> tuple[dict, object, object]:
    """Return (document, base, main) for an owned LOCAL paper document.

    ``base`` is the main file's parent — the same root source_read resolves
    targets against and the compiled sources.json labels agree with.
    """
    from lib.storage.errors import StorageError
    from .document_repository import PaperDocumentRepository
    try:
        document = PaperDocumentRepository(owner_user_id).get(document_id)
    except StorageError as exc:
        raise BuildError('document_not_found', 'Paper document is unavailable') from exc
    if not document:
        raise BuildError('document_not_found', 'Paper document is unavailable')
    source = document.get('source') or {}
    if source.get('source') != 'local':
        raise BuildError('source_path', 'Only local paper sources can be edited here')
    root = _root(source['project_root'])
    try:
        main = _safe(root, source.get('main_file') or source['path'])
    except OSError as exc:
        raise BuildError('source_path', 'Paper source is unavailable') from exc
    return document, main.parent, main


def resolve_edit_target(base, path):
    """Resolve one edit target inside *base*; refuse non-paper and binary files."""
    try:
        target = _safe(base, path)
    except OSError as exc:
        raise BuildError('source_path', 'Paper source is unavailable') from exc
    suffix = target.suffix.lower()
    if suffix not in set(schema()['x-source-suffixes']):
        raise BuildError('source_path', 'File is not part of a paper source tree')
    if suffix not in _TEXT_SUFFIXES:
        raise BuildError('source_binary', 'Binary paper source; download it from the project tree instead')
    return target


def edit_document_source(payload: dict, *, owner_user_id: int) -> dict:
    validate(payload, 'PaperDocumentSourceEditRequestV1')
    has_content = payload.get('content') is not None
    has_edits = bool(payload.get('edits'))
    if has_content == has_edits:
        raise BuildError('build_contract', 'Exactly one of content or edits is required')
    document, base, _main = resolve_local_document(
        payload['document_id'], owner_user_id=owner_user_id)
    target = resolve_edit_target(base, payload['path'])
    relative = target.relative_to(base).as_posix()
    current = read_source(base, target, limit=limit('source_view_bytes'))
    expected = payload.get('base_sha256')
    if expected and hashlib.sha256(current).hexdigest() != expected:
        raise BuildError('source_conflict',
                         'Paper source changed since it was read; reload and retry')
    # The document record carries no conversation binding for a plain source
    # edit; journaled attribution falls back to the owning root, matching the
    # browser project_write route's unattributed writes.
    conv_id = document.get('conv_id') or None
    results = None
    if has_content:
        from lib.project_mod.write_tools import tool_write_file
        outcome = tool_write_file(str(base), relative, payload['content'],
                                  description='paper source edit', conv_id=conv_id)
        if not outcome.get('ok'):
            raise BuildError('source_path',
                             outcome.get('error') or 'Paper source write failed')
    else:
        from lib.project_mod.write_tools import tool_apply_diff
        results = []
        for index, edit in enumerate(payload['edits']):
            outcome = tool_apply_diff(str(base), relative, edit['search'], edit['replace'],
                                      description=edit.get('description') or '',
                                      conv_id=conv_id,
                                      replace_all=bool(edit.get('replace_all')))
            if outcome.get('ok'):
                results.append({'index': index, 'ok': True})
            else:
                results.append({'index': index, 'ok': False,
                                'error': (outcome.get('error') or 'Paper source edit failed')[:2000]})
    final = read_source(base, target, limit=limit('source_view_bytes'))
    response = {'path': relative, 'size_bytes': len(final),
                'sha256': hashlib.sha256(final).hexdigest()}
    if results is not None:
        response['results'] = results
    return validate(response, 'PaperDocumentSourceEditV1')
