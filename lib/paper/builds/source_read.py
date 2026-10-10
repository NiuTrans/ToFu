"""Read one owned paper source file for the browser's read-only code view.

Authority flows from the durable document binding (never a client path):
the stored project reference resolves through the same symlink-refusing
helpers discovery uses, and reads stay inside the snapshot root with the
contract's source-view byte cap. Binary members are typed, not streamed.
"""
from __future__ import annotations

from .contract import limit, schema, validate
from .documents import _root, _safe
from .errors import BuildError
from .sources import read_source

# Suffixes the code view renders as text; binaries (fonts, images, PDFs)
# belong to the artifact lane, not a source panel.
_TEXT_SUFFIXES = frozenset({
    '.tex', '.ltx', '.bib', '.bst', '.sty', '.cls', '.clo', '.cfg', '.def',
    '.fd', '.pgf', '.tikz', '.csv', '.tsv', '.dat', '.txt', '.enc', '.map',
    '.afm',
})


def read_document_source(payload: dict, *, owner_user_id: int) -> dict:
    validate(payload, 'PaperDocumentSourceRequestV1')
    from lib.storage.errors import StorageError
    from .document_repository import PaperDocumentRepository
    try:
        document = PaperDocumentRepository(owner_user_id).get(payload['document_id'])
    except StorageError as exc:
        raise BuildError('document_not_found', 'Paper document is unavailable') from exc
    if not document:
        raise BuildError('document_not_found', 'Paper document is unavailable')
    source = document.get('source') or {}
    if source.get('source') != 'local':
        raise BuildError('source_path', 'Only local paper sources can be read here')
    root = _root(source['project_root'])
    try:
        main = _safe(root, source.get('main_file') or source['path'])
        base = main.parent  # snapshot-root parity with compiled sources.json labels
        target = _safe(base, payload['path'])
    except OSError as exc:
        raise BuildError('source_path', 'Paper source is unavailable') from exc
    suffix = target.suffix.lower()
    if suffix not in set(schema()['x-source-suffixes']):
        raise BuildError('source_path', 'File is not part of a paper source tree')
    if suffix not in _TEXT_SUFFIXES:
        raise BuildError('source_binary', 'Binary paper source; download it from the project tree instead')
    data = read_source(base, target, limit=limit('source_view_bytes'))
    relative = target.relative_to(base).as_posix()
    return validate({
        'path': relative,
        'content': data.decode('utf-8', errors='replace'),
        'truncated': False,
        'size_bytes': len(data),
        'main_file': main.relative_to(root).as_posix(),
    }, 'PaperDocumentSourceV1')
