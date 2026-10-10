"""Import an arXiv e-print source archive into an owned conversation root.

One command: bounded download → atomic bounded extraction → document
discovery → explicit selection. The conversation's roots are the only write
authority; a read-only root is refused. Everything the extractor produces
stays inside one fresh ``arxiv-<id>/`` directory, so an import never
overwrites user files and is trivially removable.
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

from lib.paper.archive import extract_archive
from lib.paper.arxiv import _extract_arxiv_id, fetch_eprint

from .contract import validate
from .documents import discover_documents, select_document
from .errors import BuildError
from .service import owned_settings


def _local_roots(settings: dict, requested: str) -> list[tuple[Path, str]]:
    roots = []
    for value in [settings.get('projectPath'), *(settings.get('projectPaths') or [])]:
        if not isinstance(value, str) or not value.strip() or value.startswith('remote:'):
            continue
        if requested and value != requested:
            continue
        try:
            path = Path(value).expanduser().resolve(strict=True)
        except OSError:
            continue
        if path.is_dir():
            roots.append((path, value))
    return roots


def import_arxiv_source(payload: dict, *, owner_user_id: int) -> dict:
    validate(payload, 'PaperArxivImportRequestV1')
    arxiv_id = _extract_arxiv_id(payload['arxiv'])
    if not arxiv_id:
        raise BuildError('arxiv_unavailable', 'Not a recognizable arXiv id or URL')
    settings = owned_settings(payload['conv_id'], owner_user_id)
    roots = _local_roots(settings, payload.get('project_root') or '')
    if not roots:
        raise BuildError('project_root',
                         'Attach a local project folder to this conversation first')
    root, reference = roots[0]
    try:
        from lib.project_mod.write_tools import _enforce_not_readonly
        _enforce_not_readonly(str(root), conv_id=payload['conv_id'])
    except ValueError as exc:
        raise BuildError('project_root', str(exc)) from exc

    safe_id = re.sub(r'[^0-9A-Za-z._-]+', '-', arxiv_id)[:80]
    staging = tempfile.NamedTemporaryFile(prefix='tofu-eprint-', suffix='.bin',
                                          delete=False)
    staging_path = Path(staging.name)
    staging.close()
    try:
        fetch_eprint(arxiv_id, staging_path)
        receipt = extract_archive(staging_path, root, name_hint=f'arxiv-{safe_id}')
    finally:
        staging_path.unlink(missing_ok=True)

    relative = receipt.directory.relative_to(root).as_posix()
    discovered = discover_documents(
        {'conv_id': payload['conv_id'], 'project_root': reference, 'path': relative},
        owner_user_id=owner_user_id)
    candidates = [doc for doc in discovered['documents'] if doc['source'] == 'local']
    document = None
    if candidates:
        chosen = candidates[0]
        document = select_document(
            {'conv_id': payload['conv_id'], 'source': 'local',
             'project_root': reference, 'path': chosen['path'],
             'main_file': chosen.get('main_file') or chosen['path']},
            owner_user_id=owner_user_id)['selected']
    return validate({
        'directory': relative,
        'files': receipt.files,
        'bytes': receipt.bytes_written,
        'skipped': [[name, reason] for name, reason in receipt.skipped[:16]],
        'candidates': [doc['path'] for doc in candidates[:16]],
        'document': document,
    }, 'PaperArxivImportResultV1')
