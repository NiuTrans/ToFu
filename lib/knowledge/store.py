"""Domain facade for the durable local knowledge corpus.

File parsing and owner-segregated source files live here. Durable metadata is
accessed only through :class:`lib.knowledge.repository.KnowledgeRepository`.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

from lib.log import get_logger
from lib.identity import require_user_id
from lib.runtime_paths import data_root

from .chunking import chunk_document
from .ingest import KnowledgeIngestError, extract
from .assets import proxy_text

logger = get_logger(__name__)


_SOURCE_ROOT_OVERRIDE: str | None = None  # tests only
_ASSET_ROOT_OVERRIDE: str | None = None  # tests only


def _source_root(user_id: int) -> Path:
    base = (Path(_SOURCE_ROOT_OVERRIDE) if _SOURCE_ROOT_OVERRIDE
            else Path(data_root()) / 'knowledge-files')
    return base / str(require_user_id(user_id, context='knowledge source owner')) / 'sources'


def _asset_root(user_id: int) -> Path:
    base = (Path(_ASSET_ROOT_OVERRIDE) if _ASSET_ROOT_OVERRIDE
            else Path(data_root()) / 'knowledge-files')
    return base / str(require_user_id(user_id, context='knowledge asset owner')) / 'assets'


def _repository(user_id: int):
    from .repository import KnowledgeRepository

    return KnowledgeRepository(user_id)


def _mutation_command_id(
    operation: str, *, user_id: int, command_id: str | None,
) -> str:
    """Bind one storage receipt to one application-level mutation intent."""
    owner_user_id = require_user_id(user_id, context=f'{operation} owner')
    supplied = str(command_id or '').strip()
    if supplied:
        return supplied
    return f'{operation}:{owner_user_id}:{uuid.uuid4().hex}'


def _public_document(document: dict) -> dict:
    row = dict(document)
    if 'assets' in row:
        assets = row.get('assets') or []
        row['asset_count'] = len(assets)
        row['pending_asset_count'] = sum(
            1 for asset in assets
            if asset.get('enrichment_status') in ('pending', 'running'))
        row['asset_issue_count'] = sum(
            1 for asset in assets
            if asset.get('enrichment_status') in ('no_vision', 'failed'))
    return _row_to_document(row)


_CJK_RE = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff]+')
_WORD_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.+/#-]*')


def search_tokens(text: str, *, cap: int = 256) -> list[str]:
    """Language-agnostic FTS tokens, including CJK bi/tri-grams."""
    tokens: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        value = value.strip().lower()
        if value and value not in seen and len(tokens) < cap:
            seen.add(value)
            tokens.append(value)

    for word in _WORD_RE.findall(text or ''):
        add(word)
        # Preserve identifiers but also let a query match one component.
        for part in re.split(r'[_.+/#-]+', word):
            if len(part) >= 2:
                add(part)
    for run in _CJK_RE.findall(text or ''):
        if len(run) == 1:
            add(run)
            continue
        for n in (2, 3):
            if len(run) >= n:
                for i in range(len(run) - n + 1):
                    add(run[i:i + n])
    return tokens


def _index_text(
    name: str, section: str, content: str, *, cap: int = 256
) -> str:
    # Repeat high-signal metadata once. The search body is tokenized rather
    # than indexed verbatim so retrieval is deterministic across backends.
    return ' '.join(search_tokens(
        f'{name} {section} {section} {content}', cap=cap))


def _row_to_document(row: dict) -> dict:
    try:
        warnings = json.loads(row.get('warnings_json') or '[]')
    except (TypeError, ValueError) as exc:
        logger.debug('[Knowledge] malformed document warnings JSON: %s', exc)
        warnings = []
    kind = str(row.get('kind') or '').lower()
    if kind == '.pdf':
        category = 'pdf'
    elif kind in ('.doc', '.docx', '.odt', '.rtf'):
        category = 'document'
    elif kind in ('.xls', '.xlsx', '.ods', '.csv', '.tsv'):
        category = 'spreadsheet'
    elif kind in ('.ppt', '.pptx', '.odp'):
        category = 'presentation'
    elif kind in ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'):
        category = 'image'
    elif kind == '.eml':
        category = 'email'
    elif kind == '.epub':
        category = 'ebook'
    elif kind in (
        '.txt', '.md', '.markdown', '.json', '.jsonl', '.xml', '.html',
        '.htm', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.rst', '.log',
        '.tex', '.bib', '.srt', '.vtt', '.sql', '.py', '.js', '.ts',
        '.java', '.c', '.cpp', '.h', '.hpp', '.go', '.rs', '.rb', '.php',
        '.sh', '.bash', '.zsh', '.css', '.scss', '.less', '.r', '.m',
        '.swift',
    ):
        category = 'text'
    else:
        category = str(row.get('category') or 'other')
    return {
        'id': row['id'],
        'name': row['name'],
        'kind': row['kind'],
        'category': str(row.get('category') or category),
        'size_bytes': row['size_bytes'],
        'method': row['method'],
        'warnings': warnings,
        'text_chars': row['text_chars'],
        'chunk_count': row['chunk_count'],
        'asset_count': int(row.get('asset_count') or 0),
        'pending_asset_count': int(row.get('pending_asset_count') or 0),
        'asset_issue_count': int(row.get('asset_issue_count') or 0),
        'pages': row['pages'],
        'created_at': row['created_at'],
        'updated_at': row['updated_at'],
    }


def list_documents(*, user_id: int) -> list[dict]:
    return [_public_document(row) for row in _repository(user_id).documents()]


def get_document_content(
    document_id: str, *, user_id: int, offset: int = 0, limit: int = 80,
) -> dict | None:
    """Expose the durable parsed evidence for an explicit user inspection."""
    page = _repository(user_id).document_content(
        str(document_id or ''),
        offset=max(0, int(offset)),
        limit=max(1, min(200, int(limit))),
    )
    if page is None:
        return None
    page['document'] = _public_document(dict(page['document']))
    return page


def is_enabled(*, user_id: int) -> bool:
    return bool(_repository(user_id).settings().get('enabled'))


def set_enabled(
    enabled: bool, *, user_id: int, command_id: str | None = None,
) -> dict:
    _repository(user_id).patch_settings(
        enabled=bool(enabled),
        command_id=_mutation_command_id(
            'knowledge.settings.enabled', user_id=user_id,
            command_id=command_id),
    )
    return get_status(user_id=user_id)


def visual_enrichment_enabled(*, user_id: int) -> bool:
    return bool(_repository(user_id).settings().get('visual_enrichment'))


def get_activity(*, user_id: int) -> dict:
    return _repository(user_id).enrichment_activity()


def set_visual_enrichment(
    enabled: bool, *, user_id: int, command_id: str | None = None,
) -> dict:
    _repository(user_id).patch_settings(
        visual_enrichment=bool(enabled),
        command_id=_mutation_command_id(
            'knowledge.settings.visual', user_id=user_id,
            command_id=command_id),
    )
    if enabled:
        from .enrichment import start_visual_enrichment
        start_visual_enrichment(user_id=user_id)
    else:
        from .enrichment import stop_visual_enrichment
        stop_visual_enrichment(user_id=user_id)
    return get_status(user_id=user_id)


def tool_available(*, user_id: int) -> bool:
    """Cheap gate used while building every model tool schema."""
    try:
        return _repository(user_id).available()
    except Exception as exc:
        logger.debug('[Knowledge] availability gate failed closed: %s', exc)
        return False


def get_status(
    *, user_id: int, page: int = 1, page_size: int = 30, query: str = '',
    category: str = 'all', sort: str = 'updated_desc',
) -> dict:
    snapshot = _repository(user_id).catalog(
        page=page, page_size=page_size, query=query,
        category=category, sort=sort)
    snapshot['documents'] = [
        _row_to_document(row) for row in snapshot.get('documents') or []]
    return snapshot


def _safe_source_name(digest: str, kind: str, document_id: str) -> str:
    """Use a unique immutable name so delete/duplicate cleanup cannot race."""
    suffix = kind if re.fullmatch(r'\.[a-z0-9]{1,10}', kind or '') else '.bin'
    return f'{digest}-{document_id}{suffix}'


def _write_source(raw: bytes, stored_name: str, *, user_id: int) -> Path:
    source_root = _source_root(user_id)
    source_root.mkdir(parents=True, exist_ok=True)
    final_path = source_root / stored_name
    temp_path = source_root / f'.{stored_name}.{uuid.uuid4().hex}.tmp'
    try:
        with open(temp_path, 'wb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, final_path)
        return final_path
    finally:
        temp_path.unlink(missing_ok=True)


def _write_asset(raw: bytes, stored_name: str, *, user_id: int) -> Path:
    asset_root = _asset_root(user_id)
    asset_root.mkdir(parents=True, exist_ok=True)
    final_path = asset_root / stored_name
    temp_path = asset_root / f'.{stored_name}.{uuid.uuid4().hex}.tmp'
    try:
        with open(temp_path, 'wb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, final_path)
        return final_path
    finally:
        temp_path.unlink(missing_ok=True)


def _unlink_files(paths: list[Path], *, reason: str) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning('[Knowledge] %s cleanup failed for %s: %s',
                           reason, path.name, exc)


def _indexed_chunks(display_name: str, chunks: list[dict]) -> list[dict]:
    return [
        {
            'ordinal': chunk['ordinal'],
            'section': chunk.get('section', ''),
            'location': chunk.get('location', ''),
            'content': chunk['content'],
            'search_text': _index_text(
                display_name, chunk.get('section', ''), chunk['content'],
                cap=int(chunk.get('search_cap') or 256)),
            'assets': list(chunk.get('assets') or []),
        }
        for chunk in chunks
    ]


def _prepare_visual_index(
    display_name: str,
    parsed: dict,
    document_id: str,
    now: float,
    chunks: list[dict],
    *,
    user_id: int,
) -> tuple[list[dict], list[dict], list[Path]]:
    """Write immutable assets and add one primary searchable proxy per asset."""
    rows: list[dict] = []
    paths: list[Path] = []
    enrich_visuals = visual_enrichment_enabled(user_id=user_id)
    try:
        for ordinal, extracted in enumerate(parsed.get('assets') or []):
            raw = extracted.get('raw')
            if not isinstance(raw, (bytes, bytearray)) or not raw:
                continue
            asset_id = uuid.uuid4().hex
            suffix = str(extracted.get('suffix') or '.bin')
            if not re.fullmatch(r'\.[a-z0-9]{1,8}', suffix):
                suffix = '.bin'
            stored_name = f'{document_id}-{ordinal:04d}-{asset_id}{suffix}'
            path = _write_asset(bytes(raw), stored_name, user_id=user_id)
            paths.append(path)
            row = {
                'id': asset_id,
                'ordinal': ordinal,
                'kind': str(extracted.get('kind') or 'image'),
                'stored_name': stored_name,
                'mime_type': str(extracted.get('mime_type') or 'application/octet-stream'),
                'sha256': str(extracted.get('sha256') or hashlib.sha256(raw).hexdigest()),
                'size_bytes': len(raw),
                'width': int(extracted.get('width') or 0),
                'height': int(extracted.get('height') or 0),
                'page': int(extracted.get('page') or 0),
                'pages_json': json.dumps(extracted.get('pages') or []),
                'bbox_json': json.dumps(extracted.get('bbox') or []),
                'caption': str(extracted.get('caption') or ''),
                'ocr_text': str(extracted.get('ocr_text') or ''),
                'description': str(extracted.get('description') or ''),
                'enrichment_status': (
                    'pending' if enrich_visuals else 'not_requested'),
                'enrichment_model': '',
                'enrichment_error': '',
                'created_at': now,
                'updated_at': now,
            }
            rows.append(row)
            page = row['page']
            chunks.append({
                'ordinal': len(chunks),
                'section': row['caption'] or 'Visual evidence',
                'location': f'Page {page}' if page else 'Image attachment',
                'content': proxy_text(display_name, extracted),
                # Visual proxies can contain a full page of OCR/context. Keep
                # enough CJK bigrams to make terms throughout a normal page
                # retrievable rather than indexing only its first paragraph.
                'search_cap': 4096,
                'assets': [{'id': asset_id, 'relation': 'primary'}],
            })
    except BaseException:
        _unlink_files(paths, reason='visual index rollback')
        raise
    return chunks, rows, paths


def add_document(
    raw: bytes, filename: str, *, user_id: int,
    command_id: str | None = None,
) -> dict:
    """Parse, chunk, persist and atomically index one uploaded document."""
    if not raw:
        raise KnowledgeIngestError('Empty file')
    digest = hashlib.sha256(raw).hexdigest()
    # Browsers normally send a basename, but Windows paths and control bytes
    # still appear through older clients.  Keep the human name without ever
    # treating client input as a local path.
    safe_input_name = str(filename or 'document').replace('\\', '/')
    display_name = os.path.basename(safe_input_name).replace('\x00', '')[:240]
    display_name = ''.join(
        char for char in display_name if char >= ' ' or char == '\t').strip()
    if not display_name:
        display_name = 'document'

    repository = _repository(user_id)
    existing = repository.document_by_digest(digest)
    if existing:
        result = _row_to_document(existing)
        result['duplicate'] = True
        return result

    document_id = uuid.uuid4().hex
    parsed = extract(raw, display_name)
    chunks = chunk_document(parsed['text']) if parsed['text'].strip() else []
    now = time.time()
    chunks, assets, asset_paths = _prepare_visual_index(
        display_name, parsed, document_id, now, chunks, user_id=user_id)
    if not chunks:
        raise KnowledgeIngestError('The document produced no searchable evidence')

    stored_name = _safe_source_name(digest, parsed['kind'], document_id)
    try:
        final_path = _write_source(raw, stored_name, user_id=user_id)
    except BaseException:
        _unlink_files(asset_paths, reason='source write rollback')
        raise
    document = {
        'id': document_id,
        'sha256': digest,
        'name': display_name,
        'stored_name': stored_name,
        'kind': parsed['kind'],
        'size_bytes': len(raw),
        'method': parsed['method'],
        'warnings_json': json.dumps(
            parsed.get('warnings') or [], ensure_ascii=False),
        'text_chars': len(parsed['text']),
        'chunk_count': len(chunks),
        'pages': int(parsed.get('pages') or 0),
        'created_at': now,
        'updated_at': now,
    }
    indexed_chunks = _indexed_chunks(display_name, chunks)

    try:
        document = {**document, 'chunks': indexed_chunks, 'assets': assets}
        row, inserted = repository.create_document(
            document,
            command_id=_mutation_command_id(
                'knowledge.document.create', user_id=user_id,
                command_id=command_id),
        )
    except BaseException:
        # Every candidate owns a unique source path, so its rollback cleanup
        # can never unlink another process's successfully indexed document.
        try:
            final_path.unlink(missing_ok=True)
        except OSError as cleanup_exc:
            logger.warning(
                '[Knowledge] failed to clean source after index error: %s',
                cleanup_exc)
        _unlink_files(asset_paths, reason='failed index')
        raise

    if not inserted:
        # Another process won the digest race while this process parsed. Its
        # stored_name differs by document id and is therefore not endangered.
        try:
            final_path.unlink(missing_ok=True)
        except OSError as cleanup_exc:
            logger.warning(
                '[Knowledge] failed to clean duplicate source candidate: %s',
                cleanup_exc)
        _unlink_files(asset_paths, reason='duplicate candidate')
        result = _row_to_document(row)
        result['duplicate'] = True
        return result

    result = _row_to_document(row)
    result['asset_count'] = len(assets)
    result['duplicate'] = False
    logger.info(
        '[Knowledge] indexed %s: %d chars, %d chunks via %s',
        display_name, result['text_chars'], result['chunk_count'],
        result['method'])
    if assets and visual_enrichment_enabled(user_id=user_id):
        from .enrichment import start_visual_enrichment
        start_visual_enrichment(user_id=user_id)
    return result


def reindex_document(
    document_id: str, *, user_id: int, command_id: str | None = None,
) -> dict | None:
    """Re-run the current parser over an immutable stored source.

    This is intentionally a replace-in-one-transaction operation: readers see
    either the previous complete index or the new complete index, never the
    transient empty state between deleting old chunks and inserting new ones.
    """
    repository = _repository(user_id)
    row = repository.document(document_id)
    if row is None:
        return None
    stored_name = str(row.get('stored_name') or '')
    if not stored_name or Path(stored_name).name != stored_name:
        raise KnowledgeIngestError('Stored source path is invalid')
    source_path = _source_root(user_id) / stored_name
    try:
        raw = source_path.read_bytes()
    except FileNotFoundError as exc:
        raise KnowledgeIngestError('Original source file is missing') from exc
    except OSError as exc:
        raise KnowledgeIngestError(f'Original source could not be read: {exc}') from exc

    display_name = str(row.get('name') or 'document')
    parsed = extract(raw, display_name)
    chunks = chunk_document(parsed['text']) if parsed['text'].strip() else []
    now = time.time()
    chunks, assets, asset_paths = _prepare_visual_index(
        display_name, parsed, document_id, now, chunks, user_id=user_id)
    if not chunks:
        raise KnowledgeIngestError('The document produced no searchable evidence')
    metadata = {
        'kind': parsed['kind'],
        'method': parsed['method'],
        'warnings_json': json.dumps(
            parsed.get('warnings') or [], ensure_ascii=False),
        'text_chars': len(parsed['text']),
        'chunk_count': len(chunks),
        'pages': int(parsed.get('pages') or 0),
        'updated_at': now,
    }
    try:
        replacement = {
            **row,
            **metadata,
            'chunks': _indexed_chunks(display_name, chunks),
            'assets': assets,
        }
        updated = repository.replace_document(
            replacement,
            command_id=_mutation_command_id(
                'knowledge.document.replace', user_id=user_id,
                command_id=command_id),
        )
    except BaseException:
        _unlink_files(asset_paths, reason='reindex rollback')
        raise
    if updated is None:
        _unlink_files(asset_paths, reason='vanished reindex candidate')
        return None
    old_paths = [
        _asset_root(user_id) / name
        for name in (str(item) for item in
                     (updated.get('_replaced_asset_names') or []))
        if name and Path(name).name == name
    ]
    _unlink_files(old_paths, reason='replaced asset')
    result = _row_to_document(updated)
    result['asset_count'] = len(assets)
    logger.info(
        '[Knowledge] reindexed %s: %d chars, %d chunks via %s',
        result['name'], result['text_chars'], result['chunk_count'],
        result['method'])
    if assets and visual_enrichment_enabled(user_id=user_id):
        from .enrichment import start_visual_enrichment
        start_visual_enrichment(user_id=user_id)
    return result


def delete_document(
    document_id: str, *, user_id: int, command_id: str | None = None,
) -> bool:
    deleted = _repository(user_id).delete_document(
        document_id,
        command_id=_mutation_command_id(
            'knowledge.document.delete', user_id=user_id,
            command_id=command_id),
    )
    if deleted is None:
        return False
    stored_name = str(deleted.get('source') or deleted.get('stored_name') or '')
    try:
        if stored_name and Path(stored_name).name == stored_name:
            (_source_root(user_id) / stored_name).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(
            '[Knowledge] source cleanup failed for %s: %s', stored_name, exc)
    asset_paths = []
    for item in (deleted.get('assets') or []):
        name = item.get('stored_name') if isinstance(item, dict) else str(item)
        if name and Path(name).name == name:
            asset_paths.append(_asset_root(user_id) / name)
    _unlink_files(asset_paths, reason='deleted asset')
    return True


def get_asset(asset_id: str, *, user_id: int) -> dict | None:
    return _repository(user_id).asset(str(asset_id or ''))


def read_asset(asset_id: str, *, user_id: int) -> tuple[dict, bytes] | None:
    row = get_asset(asset_id, user_id=user_id)
    if row is None:
        return None
    stored_name = str(row.get('stored_name') or '')
    if not stored_name or Path(stored_name).name != stored_name:
        return None
    try:
        return row, (_asset_root(user_id) / stored_name).read_bytes()
    except (FileNotFoundError, OSError) as exc:
        logger.warning('[Knowledge] asset %s could not be read: %s', asset_id, exc)
        return None


__all__ = [
    'KnowledgeIngestError', 'add_document', 'delete_document', 'get_asset',
    'get_activity', 'get_document_content', 'get_status', 'is_enabled', 'list_documents',
    'read_asset',
    'reindex_document', 'search_tokens', 'set_enabled',
    'set_visual_enrichment', 'tool_available', 'visual_enrichment_enabled',
]
