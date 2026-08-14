"""Domain facade for the durable local knowledge corpus.

File parsing and content-addressed source files live here.  SQLite driver,
schema, transaction and query ownership belong exclusively to
``lib.database.knowledge_repository``.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid

from lib.database import knowledge_repository as _repository
from lib.log import get_logger
from lib.runtime_paths import data_root

from .chunking import chunk_document
from .ingest import KnowledgeIngestError, extract
from .assets import proxy_text

logger = get_logger(__name__)

_LOCK = threading.RLock()
_DB_PATH_OVERRIDE: str | None = None  # tests only
_SOURCE_ROOT_OVERRIDE: str | None = None  # tests only
_ASSET_ROOT_OVERRIDE: str | None = None  # tests only


def _root() -> Path:
    return Path(data_root()) / 'knowledge'


def _db_path() -> Path:
    return Path(_DB_PATH_OVERRIDE) if _DB_PATH_OVERRIDE else _root() / 'knowledge.sqlite3'


def _source_root() -> Path:
    return Path(_SOURCE_ROOT_OVERRIDE) if _SOURCE_ROOT_OVERRIDE else _root() / 'sources'


def _asset_root() -> Path:
    if _ASSET_ROOT_OVERRIDE:
        return Path(_ASSET_ROOT_OVERRIDE)
    if _SOURCE_ROOT_OVERRIDE:
        return Path(_SOURCE_ROOT_OVERRIDE).parent / 'assets'
    return _root() / 'assets'


# Pure registration: no file, connection, schema or thread is created. The
# server installs the interposer before tool/plugin discovery, so importing
# this built-in feature closes the raw-driver bypass even for an empty corpus.
_repository.register_store(_db_path())


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
    # than indexed verbatim so Chinese works with stock unicode61 FTS5.
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


def list_documents() -> list[dict]:
    return [
        _row_to_document(row)
        for row in _repository.list_documents(_db_path())
    ]


def get_document_content(
    document_id: str, *, offset: int = 0, limit: int = 80,
) -> dict | None:
    """Expose the durable parsed evidence for an explicit user inspection."""
    row = _repository.find_document_by_id(
        _db_path(), str(document_id or ''))
    if row is None:
        return None
    clean_offset = max(0, int(offset))
    clean_limit = max(1, min(200, int(limit)))
    chunks = _repository.list_document_chunks(
        _db_path(), row['id'], offset=clean_offset, limit=clean_limit)
    total = int(row.get('chunk_count') or 0)
    return {
        'document': _row_to_document(row),
        'chunks': chunks,
        'pagination': {
            'offset': clean_offset,
            'limit': clean_limit,
            'total_items': total,
            'has_more': clean_offset + len(chunks) < total,
        },
    }


def is_enabled() -> bool:
    return _repository.is_enabled(_db_path())


def set_enabled(enabled: bool) -> dict:
    _repository.set_enabled(_db_path(), enabled)
    return get_status()


def visual_enrichment_enabled() -> bool:
    return _repository.visual_enrichment_enabled(_db_path())


def get_activity() -> dict:
    return _repository.enrichment_activity(_db_path())


def set_visual_enrichment(enabled: bool) -> dict:
    _repository.set_visual_enrichment(_db_path(), enabled)
    if enabled:
        from .enrichment import start_visual_enrichment
        start_visual_enrichment()
    return get_status()


def tool_available() -> bool:
    """Cheap gate used while building every model tool schema."""
    try:
        return _repository.tool_available(_db_path())
    except Exception as exc:
        logger.debug('[Knowledge] availability gate failed closed: %s', exc)
        return False


def get_status(
    *, page: int = 1, page_size: int = 30, query: str = '',
    category: str = 'all', sort: str = 'updated_desc',
) -> dict:
    snapshot = _repository.catalog_snapshot(
        _db_path(), page=page, page_size=page_size, query=query,
        category=category, sort=sort)
    snapshot['documents'] = [
        _row_to_document(row) for row in snapshot.get('documents', [])]
    snapshot['available'] = bool(
        snapshot.get('enabled')
        and int(snapshot.get('totals', {}).get('documents') or 0))
    return snapshot


def _safe_source_name(digest: str, kind: str, document_id: str) -> str:
    """Use a unique immutable name so delete/duplicate cleanup cannot race."""
    suffix = kind if re.fullmatch(r'\.[a-z0-9]{1,10}', kind or '') else '.bin'
    return f'{digest}-{document_id}{suffix}'


def _write_source(raw: bytes, stored_name: str) -> Path:
    source_root = _source_root()
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


def _write_asset(raw: bytes, stored_name: str) -> Path:
    asset_root = _asset_root()
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
) -> tuple[list[dict], list[dict], list[Path]]:
    """Write immutable assets and add one primary searchable proxy per asset."""
    rows: list[dict] = []
    paths: list[Path] = []
    enrich_visuals = visual_enrichment_enabled()
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
            path = _write_asset(bytes(raw), stored_name)
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


def add_document(raw: bytes, filename: str) -> dict:
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

    existing = _repository.find_document_by_sha(_db_path(), digest)
    if existing:
        result = _row_to_document(existing)
        result['duplicate'] = True
        return result

    document_id = uuid.uuid4().hex
    parsed = extract(raw, display_name)
    chunks = chunk_document(parsed['text']) if parsed['text'].strip() else []
    now = time.time()
    chunks, assets, asset_paths = _prepare_visual_index(
        display_name, parsed, document_id, now, chunks)
    if not chunks:
        raise KnowledgeIngestError('The document produced no searchable evidence')

    stored_name = _safe_source_name(digest, parsed['kind'], document_id)
    try:
        final_path = _write_source(raw, stored_name)
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
        row, inserted = _repository.insert_document(
            _db_path(), document, indexed_chunks, assets)
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
    if assets and visual_enrichment_enabled():
        from .enrichment import start_visual_enrichment
        start_visual_enrichment()
    return result


def reindex_document(document_id: str) -> dict | None:
    """Re-run the current parser over an immutable stored source.

    This is intentionally a replace-in-one-transaction operation: readers see
    either the previous complete index or the new complete index, never the
    transient empty state between deleting old chunks and inserting new ones.
    """
    row = _repository.find_document_by_id(_db_path(), document_id)
    if row is None:
        return None
    stored_name = str(row.get('stored_name') or '')
    if not stored_name or Path(stored_name).name != stored_name:
        raise KnowledgeIngestError('Stored source path is invalid')
    source_path = _source_root() / stored_name
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
        display_name, parsed, document_id, now, chunks)
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
        updated = _repository.replace_document_index(
            _db_path(), document_id, metadata,
            _indexed_chunks(display_name, chunks), assets)
    except BaseException:
        _unlink_files(asset_paths, reason='reindex rollback')
        raise
    if updated is None:
        _unlink_files(asset_paths, reason='vanished reindex candidate')
        return None
    old_paths = [
        _asset_root() / name
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
    if assets and visual_enrichment_enabled():
        from .enrichment import start_visual_enrichment
        start_visual_enrichment()
    return result


def delete_document(document_id: str) -> bool:
    deleted = _repository.delete_document(_db_path(), document_id)
    if deleted is None:
        return False
    stored_name = str(deleted.get('source') or '')
    try:
        if stored_name and Path(stored_name).name == stored_name:
            (_source_root() / stored_name).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(
            '[Knowledge] source cleanup failed for %s: %s', stored_name, exc)
    asset_paths = [
        _asset_root() / name
        for name in (str(item) for item in (deleted.get('assets') or []))
        if name and Path(name).name == name
    ]
    _unlink_files(asset_paths, reason='deleted asset')
    return True


def get_asset(asset_id: str) -> dict | None:
    return _repository.find_asset_by_id(_db_path(), str(asset_id or ''))


def read_asset(asset_id: str) -> tuple[dict, bytes] | None:
    row = get_asset(asset_id)
    if row is None:
        return None
    stored_name = str(row.get('stored_name') or '')
    if not stored_name or Path(stored_name).name != stored_name:
        return None
    try:
        return row, (_asset_root() / stored_name).read_bytes()
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
