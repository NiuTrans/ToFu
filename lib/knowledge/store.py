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
from .ingest import KnowledgeIngestError, detect_kind, extract
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


# Mirrors the sidecar knowledge contract (_knowledge._search_terms), which
# rejects any search term longer than this. Long uninterrupted ASCII runs
# (watermarks, base64 blobs, URLs in PDF proofs) are useless for retrieval;
# skip them instead of failing the whole ingest.
_MAX_TERM_CHARS = 128

_CJK_RE = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff]+')
_WORD_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.+/#-]*')


def search_tokens(text: str, *, cap: int = 256) -> list[str]:
    """Language-agnostic FTS tokens, including CJK bi/tri-grams."""
    tokens: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        value = value.strip().lower()
        if (value and len(value) <= _MAX_TERM_CHARS
                and value not in seen and len(tokens) < cap):
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
    try:
        media_metadata = json.loads(row.get('media_metadata_json') or '{}')
    except (TypeError, ValueError) as exc:
        logger.debug('[Knowledge] malformed media metadata JSON: %s', exc)
        media_metadata = {}
    if not isinstance(media_metadata, dict):
        media_metadata = {}
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
        'scope': str(row.get('scope') or 'library'),
        'media_metadata': media_metadata,
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
    scope: str = 'library',
) -> tuple[list[dict], list[dict], list[Path]]:
    """Write immutable assets and add one primary searchable proxy per asset."""
    rows: list[dict] = []
    paths: list[Path] = []
    enrich_visuals = (
        scope in {'library', 'shared'}
        and visual_enrichment_enabled(user_id=user_id))
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
                'metadata_json': json.dumps(
                    extracted.get('metadata') or {}, ensure_ascii=False),
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


def _reserve_pdf_pipeline(raw: bytes, display_name: str):
    """Reserve classic capacity only for a parser-authoritative PDF source."""
    if not (
        raw.startswith(b'%PDF-')
        or str(display_name or '').lower().endswith('.pdf')
    ):
        return None
    if detect_kind(raw, display_name) != '.pdf':
        return None
    from lib.pdf_parser.admission import CLASSIC_PDF_ADMISSION
    from lib.pdf_parser.policy import resolve_classic_pdf_budget

    budget = resolve_classic_pdf_budget()
    return CLASSIC_PDF_ADMISSION.reserve(budget.unfinished_capacity)


def _index_new_document(
    raw: bytes,
    display_name: str,
    digest: str,
    *,
    user_id: int,
    command_id: str | None,
    scope: str,
    repository,
    pdf_already_admitted: bool,
) -> dict:
    """Parse and atomically persist one digest-miss under caller admission."""
    document_id = uuid.uuid4().hex
    if pdf_already_admitted:
        parsed = extract(
            raw, display_name, _pdf_already_admitted=True)
    else:
        parsed = extract(raw, display_name)
    chunks = chunk_document(parsed['text']) if parsed['text'].strip() else []
    now = time.time()
    chunks, assets, asset_paths = _prepare_visual_index(
        display_name, parsed, document_id, now, chunks,
        user_id=user_id, scope=scope)
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
        'scope': scope,
        'media_metadata_json': json.dumps({
            'media_kind': 'document',
            'status': 'ready',
        }, separators=(',', ':')),
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
        desired_scope = _merged_document_scope(
            str(row.get('scope') or 'library'), scope)
        if desired_scope != str(row.get('scope') or 'library'):
            reconciled = repository.patch_document(
                str(row['id']), updates={'scope': desired_scope},
                command_id=_mutation_command_id(
                    'knowledge.document.share.race', user_id=user_id,
                    command_id=command_id))
            if reconciled is not None:
                row = reconciled
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
    if (scope in {'library', 'shared'} and assets
            and visual_enrichment_enabled(user_id=user_id)):
        from .enrichment import start_visual_enrichment
        start_visual_enrichment(user_id=user_id)
    return result


def add_document(
    raw: bytes, filename: str, *, user_id: int,
    command_id: str | None = None,
    scope: str = 'library',
) -> dict:
    """Parse, chunk, persist and atomically index one uploaded document."""
    if not raw:
        raise KnowledgeIngestError('Empty file')
    if scope not in {'draft', 'library', 'attachment'}:
        raise ValueError('scope must be draft, library, or attachment')
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
        desired_scope = _merged_document_scope(
            str(existing.get('scope') or 'library'), scope)
        if desired_scope != str(existing.get('scope') or 'library'):
            promoted = repository.patch_document(
                str(existing['id']), updates={'scope': desired_scope},
                command_id=_mutation_command_id(
                    'knowledge.document.share', user_id=user_id,
                    command_id=command_id))
            if promoted is not None:
                existing = promoted
        result = _row_to_document(existing)
        result['duplicate'] = True
        return result

    pdf_lease = _reserve_pdf_pipeline(raw, display_name)
    try:
        return _index_new_document(
            raw,
            display_name,
            digest,
            user_id=user_id,
            command_id=command_id,
            scope=scope,
            repository=repository,
            pdf_already_admitted=pdf_lease is not None,
        )
    finally:
        if pdf_lease is not None:
            pdf_lease.release()


def _merged_document_scope(existing_scope: str, requested_scope: str) -> str:
    """Merge content identity without letting an unsent draft claim ownership."""
    if requested_scope == 'draft' or existing_scope == 'shared':
        return existing_scope
    if existing_scope == 'draft':
        return requested_scope
    if existing_scope == requested_scope:
        return existing_scope
    if {existing_scope, requested_scope} == {'library', 'attachment'}:
        return 'shared'
    return existing_scope


def _replace_document_index(
    raw: bytes,
    display_name: str,
    document_id: str,
    row: dict,
    *,
    user_id: int,
    command_id: str | None,
    repository,
    pdf_already_admitted: bool,
) -> dict | None:
    """Build and atomically publish one replacement parser projection."""
    if pdf_already_admitted:
        parsed = extract(
            raw, display_name, _pdf_already_admitted=True)
    else:
        parsed = extract(raw, display_name)
    chunks = chunk_document(parsed['text']) if parsed['text'].strip() else []
    now = time.time()
    chunks, assets, asset_paths = _prepare_visual_index(
        display_name, parsed, document_id, now, chunks,
        user_id=user_id, scope=str(row.get('scope') or 'library'))
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
    if (row.get('scope') in {'library', 'shared'} and assets
            and visual_enrichment_enabled(user_id=user_id)):
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
    pdf_lease = _reserve_pdf_pipeline(raw, display_name)
    try:
        return _replace_document_index(
            raw,
            display_name,
            document_id,
            row,
            user_id=user_id,
            command_id=command_id,
            repository=repository,
            pdf_already_admitted=pdf_lease is not None,
        )
    finally:
        if pdf_lease is not None:
            pdf_lease.release()


def get_document_metadata(document_id: str, *, user_id: int) -> dict | None:
    """Read bounded metadata for any explicit library or attachment source."""
    row = _repository(user_id).document_metadata(str(document_id or ''))
    return _row_to_document(row) if row is not None else None


def list_document_assets(
    document_id: str, *, user_id: int, offset: int = 0, limit: int = 80,
) -> list[dict] | None:
    rows = _repository(user_id).document_assets(
        str(document_id or ''), offset=max(0, int(offset)),
        limit=max(1, min(200, int(limit))))
    if rows is None:
        return None
    result = []
    for row in rows:
        item = dict(row)
        try:
            metadata = json.loads(item.get('metadata_json') or '{}')
        except (TypeError, ValueError):
            metadata = {}
        item['metadata'] = metadata if isinstance(metadata, dict) else {}
        result.append(item)
    return result


def search_document_candidates(
    document_id: str, query: str, *, user_id: int, limit: int = 12,
) -> list[dict]:
    """Retrieve bounded evidence inside one explicitly referenced source."""
    tokens = search_tokens(str(query or ''), cap=32)
    if not tokens:
        return []
    return _repository(user_id).search_candidates(
        tokens, limit=max(1, min(40, int(limit))),
        document_id=str(document_id or ''))


def read_source_path(document_id: str, *, user_id: int) -> Path | None:
    """Resolve one owner's immutable original without exposing its filename."""
    row = _repository(user_id).document_metadata(str(document_id or ''))
    if row is None:
        return None
    stored_name = str(row.get('stored_name') or '')
    if not stored_name or Path(stored_name).name != stored_name:
        return None
    path = _source_root(user_id) / stored_name
    return path if path.is_file() else None


def patch_media_metadata(
    document_id: str, updates: dict, *, user_id: int,
    command_id: str | None = None,
) -> dict | None:
    """Merge bounded processing metadata while preserving indexed evidence."""
    repository = _repository(user_id)
    row = repository.document_metadata(str(document_id or ''))
    if row is None:
        return None
    try:
        current = json.loads(row.get('media_metadata_json') or '{}')
    except (TypeError, ValueError):
        current = {}
    if not isinstance(current, dict):
        current = {}
    current.update(dict(updates or {}))
    encoded = json.dumps(current, ensure_ascii=False, separators=(',', ':'))
    if len(encoded) > 100_000:
        raise ValueError('media metadata exceeds 100000 characters')
    updated = repository.patch_document(
        str(document_id or ''),
        updates={'media_metadata_json': encoded},
        command_id=_mutation_command_id(
            'knowledge.document.media.patch', user_id=user_id,
            command_id=command_id),
    )
    return _row_to_document(updated) if updated is not None else None


def set_document_scope(
    document_id: str, scope: str, *, user_id: int,
    command_id: str | None = None,
) -> dict | None:
    if scope not in {'draft', 'library', 'attachment', 'shared'}:
        raise ValueError('invalid document scope')
    updated = _repository(user_id).patch_document(
        str(document_id or ''), updates={'scope': scope},
        command_id=_mutation_command_id(
            'knowledge.document.scope', user_id=user_id,
            command_id=command_id))
    return _row_to_document(updated) if updated is not None else None


def create_media_source(
    source_path: str | os.PathLike[str], filename: str, *, user_id: int,
    media_metadata: dict, command_id: str | None = None,
    scope: str = 'attachment',
) -> dict:
    """Copy a large local source once and create an attachment-scoped record.

    The copy is streamed and fsynced; callers never load a video-sized payload
    into Python memory. Digest races converge through the Sidecar unique key.
    """
    source = Path(source_path)
    if scope not in {'draft', 'attachment'}:
        raise ValueError('media source scope must be draft or attachment')
    if not source.is_file():
        raise KnowledgeIngestError('Uploaded source file is missing')
    safe_input_name = str(filename or 'media').replace('\\', '/')
    display_name = os.path.basename(safe_input_name).replace('\x00', '')[:240]
    display_name = ''.join(
        char for char in display_name if char >= ' ' or char == '\t').strip()
    if not display_name:
        display_name = 'media'
    kind = Path(display_name).suffix.lower()
    if not re.fullmatch(r'\.[a-z0-9]{1,10}', kind):
        kind = '.bin'

    owner_user_id = require_user_id(user_id, context='media source owner')
    target_root = _source_root(owner_user_id)
    target_root.mkdir(parents=True, exist_ok=True)
    document_id = uuid.uuid4().hex
    temporary = target_root / f'.{document_id}.{uuid.uuid4().hex}.tmp'
    digest_builder = hashlib.sha256()
    size_bytes = 0
    try:
        with open(source, 'rb') as input_handle, open(temporary, 'wb') as output_handle:
            while True:
                block = input_handle.read(1024 * 1024)
                if not block:
                    break
                digest_builder.update(block)
                size_bytes += len(block)
                output_handle.write(block)
            output_handle.flush()
            os.fsync(output_handle.fileno())
        if size_bytes <= 0:
            raise KnowledgeIngestError('Empty file')
        digest = digest_builder.hexdigest()
        repository = _repository(owner_user_id)
        existing = repository.document_by_digest(digest)
        if existing is not None:
            desired_scope = _merged_document_scope(
                str(existing.get('scope') or 'library'), scope)
            if desired_scope != str(existing.get('scope') or 'library'):
                promoted = repository.patch_document(
                    str(existing['id']), updates={'scope': desired_scope},
                    command_id=_mutation_command_id(
                        'knowledge.document.media.share',
                        user_id=owner_user_id, command_id=command_id))
                if promoted is not None:
                    existing = promoted
            result = _row_to_document(existing)
            result['duplicate'] = True
            return result

        stored_name = _safe_source_name(digest, kind, document_id)
        final_path = target_root / stored_name
        os.replace(temporary, final_path)
        now = time.time()
        metadata = dict(media_metadata or {})
        metadata.setdefault('status', 'processing')
        document = {
            'id': document_id,
            'sha256': digest,
            'name': display_name,
            'stored_name': stored_name,
            'kind': kind,
            'size_bytes': size_bytes,
            'method': 'media_pipeline',
            'warnings_json': '[]',
            'text_chars': 0,
            'chunk_count': 0,
            'pages': 0,
            'scope': scope,
            'media_metadata_json': json.dumps(
                metadata, ensure_ascii=False, separators=(',', ':')),
            'created_at': now,
            'updated_at': now,
            'chunks': [],
            'assets': [],
        }
        try:
            row, inserted = repository.create_document(
                document,
                command_id=_mutation_command_id(
                    'knowledge.document.media.create', user_id=owner_user_id,
                    command_id=command_id),
            )
        except BaseException:
            final_path.unlink(missing_ok=True)
            raise
        if not inserted:
            final_path.unlink(missing_ok=True)
            desired_scope = _merged_document_scope(
                str(row.get('scope') or 'library'), scope)
            if desired_scope != str(row.get('scope') or 'library'):
                promoted = repository.patch_document(
                    str(row['id']), updates={'scope': desired_scope},
                    command_id=_mutation_command_id(
                        'knowledge.document.media.share.race',
                        user_id=owner_user_id, command_id=command_id))
                if promoted is not None:
                    row = promoted
        result = _row_to_document(row)
        result['duplicate'] = not inserted
        return result
    finally:
        temporary.unlink(missing_ok=True)


def replace_media_evidence(
    document_id: str, *, chunks: list[dict], assets: list[dict],
    media_metadata: dict, user_id: int, command_id: str | None = None,
) -> dict | None:
    """Atomically replace derived evidence while preserving the original."""
    if len(assets) > 200:
        raise ValueError('media evidence is limited to 200 assets')
    repository = _repository(user_id)
    current = repository.document(str(document_id or ''))
    if current is None:
        return None
    if str(current.get('scope') or 'library') not in {
            'draft', 'attachment', 'shared'}:
        raise ValueError('media evidence can only replace an attachment source')
    now = time.time()
    rows: list[dict] = []
    written_paths: list[Path] = []
    try:
        for ordinal, candidate in enumerate(assets):
            raw = candidate.get('raw')
            if raw is None and candidate.get('path'):
                raw = Path(str(candidate['path'])).read_bytes()
            if not isinstance(raw, (bytes, bytearray)) or not raw:
                raise ValueError(f'media asset {ordinal} has no bytes')
            raw_bytes = bytes(raw)
            asset_id = uuid.uuid4().hex
            suffix = str(candidate.get('suffix') or '.jpg').lower()
            if not re.fullmatch(r'\.[a-z0-9]{1,8}', suffix):
                suffix = '.bin'
            stored_name = f'{document_id}-{ordinal:04d}-{asset_id}{suffix}'
            written_paths.append(
                _write_asset(raw_bytes, stored_name, user_id=user_id))
            rows.append({
                'id': asset_id,
                'ordinal': ordinal,
                'kind': str(candidate.get('kind') or 'media_frame'),
                'stored_name': stored_name,
                'mime_type': str(candidate.get('mime_type') or 'image/jpeg'),
                'sha256': hashlib.sha256(raw_bytes).hexdigest(),
                'size_bytes': len(raw_bytes),
                'width': int(candidate.get('width') or 0),
                'height': int(candidate.get('height') or 0),
                'page': int(candidate.get('page') or 0),
                'pages_json': '[]',
                'bbox_json': '[]',
                'caption': str(candidate.get('caption') or ''),
                'ocr_text': '',
                'description': str(candidate.get('description') or ''),
                'enrichment_status': 'not_requested',
                'enrichment_model': '',
                'enrichment_error': '',
                'metadata_json': json.dumps(
                    candidate.get('metadata') or {}, ensure_ascii=False,
                    separators=(',', ':')),
                'created_at': now,
                'updated_at': now,
            })

        normalized_chunks = []
        for ordinal, candidate in enumerate(chunks):
            refs = []
            for asset_ordinal in candidate.get('asset_ordinals') or []:
                index = int(asset_ordinal)
                if index < 0 or index >= len(rows):
                    raise ValueError('media chunk references an unknown asset')
                refs.append({'id': rows[index]['id'], 'relation': 'evidence'})
            normalized_chunks.append({
                'ordinal': ordinal,
                'section': str(candidate.get('section') or ''),
                'location': str(candidate.get('location') or ''),
                'content': str(candidate.get('content') or ''),
                'assets': refs,
                'search_cap': int(candidate.get('search_cap') or 4096),
            })
        indexed_chunks = _indexed_chunks(
            str(current.get('name') or 'media'), normalized_chunks)
        final_media_metadata = dict(media_metadata or {})
        if rows:
            final_media_metadata.setdefault('poster_asset_id', rows[0]['id'])
        replacement = {
            **current,
            'method': 'media_pipeline',
            'warnings_json': json.dumps(
                media_metadata.get('warnings') or [], ensure_ascii=False),
            'text_chars': sum(len(chunk['content']) for chunk in normalized_chunks),
            'chunk_count': len(indexed_chunks),
            'media_metadata_json': json.dumps(
                final_media_metadata, ensure_ascii=False,
                separators=(',', ':')),
            'updated_at': now,
            'chunks': indexed_chunks,
            'assets': rows,
        }
        updated = repository.replace_document(
            replacement,
            command_id=_mutation_command_id(
                'knowledge.document.media.replace', user_id=user_id,
                command_id=command_id),
        )
    except BaseException:
        _unlink_files(written_paths, reason='media evidence rollback')
        raise
    if updated is None:
        _unlink_files(written_paths, reason='vanished media evidence')
        return None
    old_paths = [
        _asset_root(user_id) / str(name)
        for name in (updated.get('_replaced_asset_names') or [])
        if name and Path(str(name)).name == str(name)
    ]
    _unlink_files(old_paths, reason='replaced media evidence')
    return _row_to_document(updated)


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


def remove_library_document(
    document_id: str, *, user_id: int, command_id: str | None = None,
) -> bool:
    """Remove library membership without breaking a shared chat attachment."""
    document = get_document_metadata(document_id, user_id=user_id)
    if document is None or document.get('scope') in {'draft', 'attachment'}:
        return False
    if document.get('scope') == 'shared':
        return set_document_scope(
            document_id, 'attachment', user_id=user_id,
            command_id=command_id) is not None
    return delete_document(
        document_id, user_id=user_id, command_id=command_id)


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
    'create_media_source', 'get_activity', 'get_document_content',
    'get_document_metadata', 'get_status', 'is_enabled',
    'list_document_assets', 'list_documents', 'patch_media_metadata',
    'read_asset', 'read_source_path', 'remove_library_document',
    'replace_media_evidence',
    'reindex_document', 'search_document_candidates', 'search_tokens', 'set_enabled',
    'set_document_scope', 'set_visual_enrichment', 'tool_available',
    'visual_enrichment_enabled',
]
