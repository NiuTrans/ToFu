"""Data-layer owner for the local knowledge corpus SQLite store.

Application modules pass domain values and receive dictionaries; driver
construction, schema lifecycle, SQL, writer reservation, rollback, retry and
cross-host ownership stay behind this repository boundary.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
from typing import TypeVar
from urllib.parse import quote

from lib.log import get_logger
from lib.database.sqlite_store_owner import assert_store_owner


logger = get_logger(__name__)

_T = TypeVar('_T')
_SCHEMA_VERSION = 3
_BUSY_TIMEOUT_MS = 30_000
_RETRIES = 6
_schema_lock = threading.RLock()
_schema_ready: dict[str, tuple[int, int]] = {}
_writer_locks: dict[str, threading.RLock] = {}

_DOCUMENT_CATEGORY_SQL = '''CASE
    WHEN lower(d.kind) = '.pdf' THEN 'pdf'
    WHEN lower(d.kind) IN ('.doc','.docx','.odt','.rtf') THEN 'document'
    WHEN lower(d.kind) IN ('.xls','.xlsx','.ods','.csv','.tsv') THEN 'spreadsheet'
    WHEN lower(d.kind) IN ('.ppt','.pptx','.odp') THEN 'presentation'
    WHEN lower(d.kind) IN ('.png','.jpg','.jpeg','.gif','.webp','.bmp') THEN 'image'
    WHEN lower(d.kind) = '.eml' THEN 'email'
    WHEN lower(d.kind) = '.epub' THEN 'ebook'
    WHEN lower(d.kind) IN (
        '.txt','.md','.markdown','.json','.jsonl','.xml','.html','.htm',
        '.yaml','.yml','.toml','.ini','.cfg','.rst','.log','.tex','.bib',
        '.srt','.vtt','.sql','.py','.js','.ts','.java','.c','.cpp','.h',
        '.hpp','.go','.rs','.rb','.php','.sh','.bash','.zsh','.css',
        '.scss','.less','.r','.m','.swift'
    ) THEN 'text'
    ELSE 'other'
END'''

_DOCUMENT_SORT_SQL = {
    'updated_desc': 'd.updated_at DESC, d.id DESC',
    'created_desc': 'd.created_at DESC, d.id DESC',
    'name_asc': 'd.name COLLATE NOCASE ASC, d.id ASC',
    'size_desc': 'd.size_bytes DESC, d.id DESC',
}

_DOCUMENT_WITH_ASSET_COUNTS = '''
    SELECT d.*,
           (SELECT COUNT(*) FROM knowledge_assets a
            WHERE a.document_id=d.id) AS asset_count,
           (SELECT COUNT(*) FROM knowledge_assets a
            WHERE a.document_id=d.id
              AND a.enrichment_status IN ('pending','running'))
             AS pending_asset_count,
           (SELECT COUNT(*) FROM knowledge_assets a
            WHERE a.document_id=d.id
              AND a.enrichment_status IN ('no_vision','failed'))
             AS asset_issue_count
    FROM knowledge_documents d
'''

_SCHEMA_STATEMENTS = (
    '''CREATE TABLE IF NOT EXISTS knowledge_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )''',
    '''CREATE TABLE IF NOT EXISTS knowledge_documents (
        id TEXT PRIMARY KEY,
        sha256 TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        stored_name TEXT NOT NULL,
        kind TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        method TEXT NOT NULL,
        warnings_json TEXT NOT NULL DEFAULT '[]',
        text_chars INTEGER NOT NULL DEFAULT 0,
        chunk_count INTEGER NOT NULL DEFAULT 0,
        pages INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )''',
    '''CREATE INDEX IF NOT EXISTS idx_knowledge_documents_updated
       ON knowledge_documents(updated_at DESC, id DESC)''',
    '''CREATE INDEX IF NOT EXISTS idx_knowledge_documents_created
       ON knowledge_documents(created_at DESC, id DESC)''',
    '''CREATE INDEX IF NOT EXISTS idx_knowledge_documents_kind
       ON knowledge_documents(kind)''',
    '''CREATE INDEX IF NOT EXISTS idx_knowledge_documents_name
       ON knowledge_documents(name COLLATE NOCASE)''',
    '''CREATE TABLE IF NOT EXISTS knowledge_chunks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id TEXT NOT NULL
            REFERENCES knowledge_documents(id) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL,
        section TEXT NOT NULL DEFAULT '',
        location TEXT NOT NULL DEFAULT '',
        content TEXT NOT NULL,
        search_text TEXT NOT NULL,
        UNIQUE(document_id, ordinal)
    )''',
    '''CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_document
       ON knowledge_chunks(document_id, ordinal)''',
    '''CREATE TABLE IF NOT EXISTS knowledge_assets (
        id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL
            REFERENCES knowledge_documents(id) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL,
        kind TEXT NOT NULL,
        stored_name TEXT NOT NULL UNIQUE,
        mime_type TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        width INTEGER NOT NULL DEFAULT 0,
        height INTEGER NOT NULL DEFAULT 0,
        page INTEGER NOT NULL DEFAULT 0,
        pages_json TEXT NOT NULL DEFAULT '[]',
        bbox_json TEXT NOT NULL DEFAULT '[]',
        caption TEXT NOT NULL DEFAULT '',
        ocr_text TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        enrichment_status TEXT NOT NULL DEFAULT 'not_requested',
        enrichment_model TEXT NOT NULL DEFAULT '',
        enrichment_error TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        UNIQUE(document_id, ordinal)
    )''',
    '''CREATE INDEX IF NOT EXISTS idx_knowledge_assets_document
       ON knowledge_assets(document_id, ordinal)''',
    '''CREATE INDEX IF NOT EXISTS idx_knowledge_assets_enrichment
       ON knowledge_assets(enrichment_status, updated_at)''',
    '''CREATE TABLE IF NOT EXISTS knowledge_chunk_assets (
        chunk_id INTEGER NOT NULL
            REFERENCES knowledge_chunks(id) ON DELETE CASCADE,
        asset_id TEXT NOT NULL
            REFERENCES knowledge_assets(id) ON DELETE CASCADE,
        relation TEXT NOT NULL DEFAULT 'evidence',
        ordinal INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(chunk_id, asset_id, relation)
    )''',
    '''CREATE INDEX IF NOT EXISTS idx_knowledge_chunk_assets_asset
       ON knowledge_chunk_assets(asset_id, chunk_id)''',
)


def _resolved(path: str | os.PathLike) -> str:
    resolved = str(Path(path).resolve())
    driver_guard = sys.modules.get('lib.database.sqlite_driver_guard')
    if driver_guard is not None:
        driver_guard.register_sqlite_driver_authority(resolved)
    return resolved


def register_store(path: str | os.PathLike) -> str:
    """Register a knowledge authority before any plugin can raw-open it."""
    return _resolved(path)


def _signature(path: str) -> tuple[int, int]:
    stat = os.stat(path)
    return int(stat.st_dev), int(stat.st_ino)


def _writer_lock(path: str) -> threading.RLock:
    with _schema_lock:
        return _writer_locks.setdefault(path, threading.RLock())


def _connect(path: str, *, create: bool) -> sqlite3.Connection:
    target = Path(path)
    if create:
        target.parent.mkdir(parents=True, exist_ok=True)
        database = path
        uri = False
    else:
        database = f'file:{quote(path)}?mode=ro'
        uri = True
    driver_guard = sys.modules.get('lib.database.sqlite_driver_guard')
    capability = (driver_guard.allow_sqlite_driver_connection(
        'knowledge repository connection')
        if driver_guard is not None else nullcontext())
    with capability:
        conn = sqlite3.connect(
            database, uri=uri, timeout=_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(f'PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}')
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA synchronous=NORMAL')
    return conn


def _is_busy(exc: BaseException) -> bool:
    text = str(exc).lower()
    return isinstance(exc, sqlite3.OperationalError) and (
        'locked' in text or 'busy' in text)


def _begin_write(
    conn: sqlite3.Connection,
    path: str,
    *,
    purpose: str,
    operation: Callable[[sqlite3.Connection], _T],
) -> _T:
    """Reserve SQLite's writer before reads and retry only pre-acquisition."""
    for attempt in range(_RETRIES):
        assert_store_owner(path, purpose=purpose)
        acquired = False
        try:
            conn.execute('BEGIN IMMEDIATE')
            acquired = True
            result = operation(conn)
            conn.commit()
            return result
        except BaseException as exc:
            try:
                conn.rollback()
            except sqlite3.Error as rollback_exc:
                logger.debug(
                    '[KnowledgeRepository] rollback after write failure failed: %s',
                    rollback_exc)
            if acquired or not _is_busy(exc) or attempt + 1 >= _RETRIES:
                raise
            time.sleep(min(0.05 * (2 ** attempt), 0.8))
    raise RuntimeError('unreachable knowledge SQLite retry state')


def _schema_is_current(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT value FROM knowledge_settings "
            "WHERE key='__schema_version__'").fetchone()
        if not row or int(row['value']) != _SCHEMA_VERSION:
            return False
        required = {
            'knowledge_documents', 'knowledge_chunks', 'knowledge_assets',
            'knowledge_chunk_assets',
        }
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('knowledge_documents','knowledge_chunks',"
            "'knowledge_assets','knowledge_chunk_assets')"
        ).fetchall()
        return {str(item['name']) for item in rows} == required
    except (sqlite3.Error, TypeError, ValueError) as exc:
        logger.debug('[KnowledgeRepository] schema probe needs migration: %s', exc)
        return False


def _set_wal(conn: sqlite3.Connection, path: str) -> None:
    for attempt in range(_RETRIES):
        assert_store_owner(path, purpose='knowledge schema journal mode')
        try:
            conn.execute('PRAGMA journal_mode=WAL').fetchone()
            return
        except sqlite3.OperationalError as exc:
            if not _is_busy(exc) or attempt + 1 >= _RETRIES:
                raise
            time.sleep(min(0.05 * (2 ** attempt), 0.8))


def _ensure_schema(conn: sqlite3.Connection, path: str) -> None:
    try:
        signature = _signature(path)
    except FileNotFoundError as exc:
        logger.debug('[KnowledgeRepository] schema file not created yet: %s', exc)
        signature = (-1, -1)
    with _schema_lock:
        if _schema_ready.get(path) == signature and signature != (-1, -1):
            return
        if _schema_is_current(conn):
            _schema_ready[path] = _signature(path)
            return
        _set_wal(conn, path)

        def migrate(db: sqlite3.Connection) -> None:
            for statement in _SCHEMA_STATEMENTS:
                db.execute(statement)
            try:
                db.execute('''
                    CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_chunks_fts
                    USING fts5(search_text, tokenize='unicode61 remove_diacritics 2')
                ''')
            except sqlite3.OperationalError as exc:
                if 'no such module' not in str(exc).lower():
                    raise
                logger.warning(
                    '[Knowledge.DB] SQLite FTS5 unavailable; using fallback: %s',
                    exc)
            db.execute('''
                INSERT INTO knowledge_settings(key, value)
                VALUES('__schema_version__', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
            ''', (str(_SCHEMA_VERSION),))

        _begin_write(
            conn, path, purpose='knowledge schema migration', operation=migrate)
        _schema_ready[path] = _signature(path)


def _read(
    db_path: str | os.PathLike,
    operation: Callable[[sqlite3.Connection], _T],
    *,
    default: _T,
) -> _T:
    path = _resolved(db_path)
    if not Path(path).is_file():
        return default
    conn = _connect(path, create=False)
    try:
        # Reads are genuinely side-effect free.  A zero-byte, interrupted, or
        # older store is simply unavailable until the next authorized write
        # initializes/migrates it; opening Settings or assembling model tools
        # must never claim SQLite ownership or mutate journal/schema state.
        if not _schema_is_current(conn):
            return default
        return operation(conn)
    finally:
        conn.close()


def _write(
    db_path: str | os.PathLike,
    *,
    purpose: str,
    operation: Callable[[sqlite3.Connection], _T],
) -> _T:
    path = _resolved(db_path)
    with _writer_lock(path):
        conn = _connect(path, create=True)
        try:
            _ensure_schema(conn, path)
            return _begin_write(
                conn, path, purpose=purpose, operation=operation)
        finally:
            conn.close()


def _dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def list_documents(db_path: str | os.PathLike) -> list[dict]:
    return _read(
        db_path,
        lambda db: [dict(row) for row in db.execute(
            _DOCUMENT_WITH_ASSET_COUNTS + ' ORDER BY d.created_at DESC'
        ).fetchall()],
        default=[],
    )


def catalog_snapshot(
    db_path: str | os.PathLike,
    *,
    page: int = 1,
    page_size: int = 30,
    query: str = '',
    category: str = 'all',
    sort: str = 'updated_desc',
) -> dict:
    """Load one bounded management page plus corpus-wide aggregates.

    The workbench must never materialize the full corpus: this query keeps the
    DOM and response bounded while aggregate statistics and category facets
    remain corpus-wide.  Asset counts are correlated only for the selected
    page and use ``idx_knowledge_assets_document``.
    """
    clean_page = max(1, int(page))
    clean_size = max(1, min(100, int(page_size)))
    clean_query = str(query or '').strip()[:200]
    clean_category = str(category or 'all').lower()
    clean_sort = str(sort or 'updated_desc').lower()
    if clean_sort not in _DOCUMENT_SORT_SQL:
        clean_sort = 'updated_desc'

    def load(db: sqlite3.Connection) -> dict:
        setting_rows = db.execute(
            "SELECT key, value FROM knowledge_settings "
            "WHERE key IN ('enabled','visual_enrichment')"
        ).fetchall()
        settings = {str(row['key']): str(row['value']) for row in setting_rows}
        truthy = ('1', 'true', 'yes', 'on')

        document_totals = db.execute('''
            SELECT COUNT(*) AS documents,
                   COALESCE(SUM(chunk_count), 0) AS chunks,
                   COALESCE(SUM(text_chars), 0) AS text_chars,
                   COALESCE(SUM(size_bytes), 0) AS size_bytes
            FROM knowledge_documents
        ''').fetchone()
        asset_totals = db.execute('''
            SELECT COUNT(*) AS assets,
                   COALESCE(SUM(CASE WHEN enrichment_status IN
                       ('pending','running') THEN 1 ELSE 0 END), 0)
                       AS pending_assets,
                   COALESCE(SUM(CASE WHEN enrichment_status IN
                       ('no_vision','failed') THEN 1 ELSE 0 END), 0)
                       AS asset_issues
            FROM knowledge_assets
        ''').fetchone()
        facets = db.execute(f'''
            SELECT {_DOCUMENT_CATEGORY_SQL} AS category, COUNT(*) AS count
            FROM knowledge_documents d
            GROUP BY category
            ORDER BY count DESC, category ASC
        ''').fetchall()

        where = []
        params: list[object] = []
        if clean_query:
            escaped = (clean_query.replace('\\', '\\\\')
                       .replace('%', '\\%').replace('_', '\\_'))
            where.append("d.name LIKE ? ESCAPE '\\' COLLATE NOCASE")
            params.append(f'%{escaped}%')
        if clean_category != 'all':
            where.append(f'({_DOCUMENT_CATEGORY_SQL}) = ?')
            params.append(clean_category)
        where_sql = (' WHERE ' + ' AND '.join(where)) if where else ''
        filtered = int(db.execute(
            'SELECT COUNT(*) FROM knowledge_documents d' + where_sql,
            params,
        ).fetchone()[0])
        total_pages = max(1, (filtered + clean_size - 1) // clean_size)
        bounded_page = min(clean_page, total_pages)
        offset = (bounded_page - 1) * clean_size
        page_select = _DOCUMENT_WITH_ASSET_COUNTS.replace(
            '    FROM knowledge_documents d\n',
            f',\n           ({_DOCUMENT_CATEGORY_SQL}) AS category\n'
            '    FROM knowledge_documents d\n',
        )
        rows = db.execute(
            page_select
            + where_sql
            + ' ORDER BY ' + _DOCUMENT_SORT_SQL[clean_sort]
            + ' LIMIT ? OFFSET ?',
            [*params, clean_size, offset],
        ).fetchall()
        totals = {
            'documents': int(document_totals['documents'] or 0),
            'chunks': int(document_totals['chunks'] or 0),
            'assets': int(asset_totals['assets'] or 0),
            'pending_assets': int(asset_totals['pending_assets'] or 0),
            'asset_issues': int(asset_totals['asset_issues'] or 0),
            'text_chars': int(document_totals['text_chars'] or 0),
            'size_bytes': int(document_totals['size_bytes'] or 0),
        }
        return {
            'enabled': settings.get('enabled', '').lower() in truthy,
            'visual_enrichment': (
                settings.get('visual_enrichment', '').lower() in truthy),
            'documents': [dict(row) for row in rows],
            'totals': totals,
            'facets': [
                {'category': str(row['category']), 'count': int(row['count'])}
                for row in facets
            ],
            'pagination': {
                'page': bounded_page,
                'page_size': clean_size,
                'total_items': filtered,
                'total_pages': total_pages,
                'has_previous': bounded_page > 1,
                'has_next': bounded_page < total_pages,
            },
            'filters': {
                'query': clean_query,
                'category': clean_category,
                'sort': clean_sort,
            },
        }

    empty = {
        'enabled': False, 'visual_enrichment': False, 'documents': [],
        'totals': {
            'documents': 0, 'chunks': 0, 'assets': 0,
            'pending_assets': 0, 'asset_issues': 0,
            'text_chars': 0, 'size_bytes': 0,
        },
        'facets': [],
        'pagination': {
            'page': 1, 'page_size': clean_size, 'total_items': 0,
            'total_pages': 1, 'has_previous': False, 'has_next': False,
        },
        'filters': {
            'query': clean_query, 'category': clean_category,
            'sort': clean_sort,
        },
    }
    return _read(db_path, load, default=empty)


def status_snapshot(db_path: str | os.PathLike) -> tuple[bool, list[dict]]:
    def load(db: sqlite3.Connection) -> tuple[bool, list[dict]]:
        enabled = db.execute(
            "SELECT value FROM knowledge_settings WHERE key='enabled'"
        ).fetchone()
        docs = db.execute(
            _DOCUMENT_WITH_ASSET_COUNTS
            + ' ORDER BY d.created_at DESC').fetchall()
        on = bool(enabled and str(enabled['value']).lower()
                  in ('1', 'true', 'yes', 'on'))
        return on, [dict(row) for row in docs]

    return _read(db_path, load, default=(False, []))


def is_enabled(db_path: str | os.PathLike) -> bool:
    return _read(
        db_path,
        lambda db: bool((row := db.execute(
            "SELECT value FROM knowledge_settings WHERE key='enabled'"
        ).fetchone()) and str(row['value']).lower()
            in ('1', 'true', 'yes', 'on')),
        default=False,
    )


def set_enabled(db_path: str | os.PathLike, enabled: bool) -> None:
    def update(db: sqlite3.Connection) -> None:
        db.execute('''
            INSERT INTO knowledge_settings(key, value) VALUES('enabled', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        ''', ('1' if enabled else '0',))

    _write(db_path, purpose='set knowledge availability', operation=update)


def visual_enrichment_enabled(db_path: str | os.PathLike) -> bool:
    return _read(
        db_path,
        lambda db: bool((row := db.execute(
            "SELECT value FROM knowledge_settings WHERE key='visual_enrichment'"
        ).fetchone()) and str(row['value']).lower() in ('1', 'true', 'yes', 'on')),
        default=False,
    )


def enrichment_activity(db_path: str | os.PathLike) -> dict:
    """Return the cheap polling projection without scanning the catalogue."""
    def load(db: sqlite3.Connection) -> dict:
        row = db.execute('''
            SELECT
              (SELECT COUNT(*) FROM knowledge_assets
               WHERE enrichment_status IN ('pending','running'))
                AS pending_assets,
              (SELECT COUNT(*) FROM knowledge_assets
               WHERE enrichment_status IN ('no_vision','failed'))
                AS asset_issues,
              (SELECT value FROM knowledge_settings
               WHERE key='visual_enrichment') AS visual_enrichment
        ''').fetchone()
        return {
            'pending_assets': int(row['pending_assets'] or 0),
            'asset_issues': int(row['asset_issues'] or 0),
            'visual_enrichment': str(row['visual_enrichment'] or '').lower()
                in ('1', 'true', 'yes', 'on'),
        }

    return _read(db_path, load, default={
        'pending_assets': 0, 'asset_issues': 0,
        'visual_enrichment': False,
    })


def set_visual_enrichment(
    db_path: str | os.PathLike, enabled: bool
) -> None:
    """Persist consent and atomically queue/cancel work that has not started."""
    def update(db: sqlite3.Connection) -> None:
        db.execute('''
            INSERT INTO knowledge_settings(key, value)
            VALUES('visual_enrichment', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        ''', ('1' if enabled else '0',))
        if enabled:
            db.execute('''
                UPDATE knowledge_assets
                SET enrichment_status='pending', enrichment_error='', updated_at=?
                WHERE enrichment_status IN ('not_requested','no_vision','failed')
            ''', (time.time(),))
        else:
            db.execute('''
                UPDATE knowledge_assets
                SET enrichment_status='not_requested', updated_at=?
                WHERE enrichment_status='pending'
            ''', (time.time(),))

    _write(
        db_path, purpose='set knowledge visual enrichment', operation=update)


def claim_pending_asset(db_path: str | os.PathLike) -> dict | None:
    """Lease one pending asset to the current in-process worker."""
    def claim(db: sqlite3.Connection) -> dict | None:
        stale_before = time.time() - 30 * 60
        row = db.execute('''
            SELECT a.*, d.name AS document_name
            FROM knowledge_assets a
            JOIN knowledge_documents d ON d.id=a.document_id
            WHERE a.enrichment_status='pending'
               OR (a.enrichment_status='running' AND a.updated_at < ?)
            ORDER BY CASE a.kind
                WHEN 'image' THEN 0 WHEN 'figure' THEN 1
                WHEN 'table' THEN 2 ELSE 3 END,
                a.created_at, a.ordinal
            LIMIT 1
        ''', (stale_before,)).fetchone()
        if row is None:
            return None
        cursor = db.execute('''
            UPDATE knowledge_assets
            SET enrichment_status='running', enrichment_error='', updated_at=?
            WHERE id=? AND (
                enrichment_status='pending'
                OR (enrichment_status='running' AND updated_at < ?)
            )
        ''', (time.time(), str(row['id']), stale_before))
        return dict(row) if cursor.rowcount == 1 else None

    return _write(
        db_path, purpose='claim knowledge visual enrichment', operation=claim)


def mark_pending_assets_no_vision(db_path: str | os.PathLike) -> None:
    def update(db: sqlite3.Connection) -> None:
        stale_before = time.time() - 30 * 60
        db.execute('''
            UPDATE knowledge_assets
            SET enrichment_status='no_vision',
                enrichment_error='No vision-capable model slot is configured',
                updated_at=?
            WHERE enrichment_status='pending'
               OR (enrichment_status='running' AND updated_at < ?)
        ''', (time.time(), stale_before))

    _write(
        db_path, purpose='defer knowledge visual enrichment', operation=update)


def tool_available(db_path: str | os.PathLike) -> bool:
    def probe(db: sqlite3.Connection) -> bool:
        row = db.execute('''
            SELECT EXISTS(
                SELECT 1 FROM knowledge_settings
                WHERE key='enabled' AND lower(value) IN ('1','true','yes','on')
            ) AS enabled,
            EXISTS(SELECT 1 FROM knowledge_documents LIMIT 1) AS has_documents
        ''').fetchone()
        return bool(row and row['enabled'] and row['has_documents'])

    return _read(db_path, probe, default=False)


def find_document_by_sha(
    db_path: str | os.PathLike, digest: str
) -> dict | None:
    return _read(
        db_path,
        lambda db: _dict(db.execute(
            _DOCUMENT_WITH_ASSET_COUNTS + ' WHERE d.sha256=?',
            (digest,)).fetchone()),
        default=None,
    )


def find_document_by_id(
    db_path: str | os.PathLike, document_id: str
) -> dict | None:
    return _read(
        db_path,
        lambda db: _dict(db.execute(
            _DOCUMENT_WITH_ASSET_COUNTS + ' WHERE d.id=?',
            (document_id,)).fetchone()),
        default=None,
    )


def list_document_chunks(
    db_path: str | os.PathLike, document_id: str, *,
    offset: int = 0, limit: int | None = None,
) -> list[dict]:
    """Return the user-visible parsed chunks without private FTS payloads."""
    clean_offset = max(0, int(offset))
    clean_limit = None if limit is None else max(1, min(200, int(limit)))

    def load(db: sqlite3.Connection) -> list[dict]:
        sql = '''
            SELECT ordinal, section, location, content
            FROM knowledge_chunks
            WHERE document_id=?
            ORDER BY ordinal
        '''
        params: list[object] = [document_id]
        if clean_limit is not None:
            sql += ' LIMIT ? OFFSET ?'
            params.extend((clean_limit, clean_offset))
        return [dict(row) for row in db.execute(sql, params).fetchall()]

    return _read(
        db_path,
        load,
        default=[],
    )


def list_assets(
    db_path: str | os.PathLike,
    *,
    document_id: str | None = None,
    statuses: Sequence[str] | None = None,
) -> list[dict]:
    """List durable image evidence without exposing filesystem paths."""
    def load(db: sqlite3.Connection) -> list[dict]:
        clauses: list[str] = []
        params: list[object] = []
        if document_id:
            clauses.append('document_id=?')
            params.append(document_id)
        if statuses:
            clean = [str(item) for item in statuses if str(item)]
            if clean:
                clauses.append(
                    'enrichment_status IN (' + ','.join('?' for _ in clean) + ')')
                params.extend(clean)
        where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
        rows = db.execute(
            'SELECT * FROM knowledge_assets' + where
            + ' ORDER BY document_id, ordinal', tuple(params)).fetchall()
        return [dict(row) for row in rows]

    return _read(db_path, load, default=[])


def find_asset_by_id(
    db_path: str | os.PathLike, asset_id: str
) -> dict | None:
    return _read(
        db_path,
        lambda db: _dict(db.execute(
            'SELECT * FROM knowledge_assets WHERE id=?',
            (asset_id,)).fetchone()),
        default=None,
    )


def _insert_asset(
    db: sqlite3.Connection,
    document_id: str,
    asset: Mapping,
) -> None:
    db.execute('''
        INSERT INTO knowledge_assets(
            id, document_id, ordinal, kind, stored_name, mime_type, sha256,
            size_bytes, width, height, page, pages_json, bbox_json, caption,
            ocr_text, description, enrichment_status, enrichment_model,
            enrichment_error, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ''', (
        str(asset['id']), document_id, int(asset['ordinal']),
        str(asset['kind']), str(asset['stored_name']),
        str(asset['mime_type']), str(asset['sha256']),
        int(asset['size_bytes']), int(asset.get('width') or 0),
        int(asset.get('height') or 0), int(asset.get('page') or 0),
        str(asset.get('pages_json') or '[]'),
        str(asset.get('bbox_json') or '[]'), str(asset.get('caption') or ''),
        str(asset.get('ocr_text') or ''),
        str(asset.get('description') or ''),
        str(asset.get('enrichment_status') or 'not_requested'),
        str(asset.get('enrichment_model') or ''),
        str(asset.get('enrichment_error') or ''),
        float(asset['created_at']), float(asset['updated_at']),
    ))


def _insert_chunk(
    db: sqlite3.Connection,
    document_id: str,
    chunk: Mapping,
) -> None:
    cursor = db.execute('''
        INSERT INTO knowledge_chunks(
            document_id, ordinal, section, location, content, search_text
        ) VALUES(?,?,?,?,?,?)
    ''', (
        document_id, int(chunk['ordinal']), str(chunk.get('section', '')),
        str(chunk.get('location', '')), str(chunk['content']),
        str(chunk['search_text']),
    ))
    try:
        db.execute(
            'INSERT INTO knowledge_chunks_fts(rowid, search_text) VALUES(?,?)',
            (cursor.lastrowid, str(chunk['search_text'])))
    except sqlite3.OperationalError as exc:
        if 'no such table' not in str(exc).lower():
            raise
    for link_ordinal, link in enumerate(chunk.get('assets') or []):
        if isinstance(link, Mapping):
            asset_id = str(link.get('id') or '')
            relation = str(link.get('relation') or 'evidence')
            ordinal = int(link.get('ordinal', link_ordinal))
        else:
            asset_id = str(link or '')
            relation = 'evidence'
            ordinal = link_ordinal
        if not asset_id:
            continue
        db.execute('''
            INSERT INTO knowledge_chunk_assets(
                chunk_id, asset_id, relation, ordinal
            ) VALUES(?,?,?,?)
        ''', (cursor.lastrowid, asset_id, relation, ordinal))


def insert_document(
    db_path: str | os.PathLike,
    document: Mapping,
    chunks: Sequence[Mapping],
    assets: Sequence[Mapping] = (),
) -> tuple[dict, bool]:
    """Idempotently insert a document, assets, chunks and links atomically."""
    digest = str(document['sha256'])

    def insert(db: sqlite3.Connection) -> tuple[dict, bool]:
        existing = db.execute(
            _DOCUMENT_WITH_ASSET_COUNTS + ' WHERE d.sha256=?',
            (digest,)).fetchone()
        if existing is not None:
            return dict(existing), False
        db.execute('''
            INSERT INTO knowledge_documents(
                id, sha256, name, stored_name, kind, size_bytes, method,
                warnings_json, text_chars, chunk_count, pages,
                created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        ''', tuple(document[key] for key in (
            'id', 'sha256', 'name', 'stored_name', 'kind', 'size_bytes',
            'method', 'warnings_json', 'text_chars', 'chunk_count', 'pages',
            'created_at', 'updated_at')))
        for asset in assets:
            _insert_asset(db, str(document['id']), asset)
        for chunk in chunks:
            _insert_chunk(db, str(document['id']), chunk)
        # A first upload makes the feature useful by default.  Once the user
        # has explicitly chosen a value, however, indexing another document
        # must not silently override that choice.
        db.execute('''
            INSERT INTO knowledge_settings(key, value) VALUES('enabled', '1')
            ON CONFLICT(key) DO NOTHING
        ''')
        row = db.execute(
            _DOCUMENT_WITH_ASSET_COUNTS + ' WHERE d.id=?',
            (str(document['id']),)).fetchone()
        return dict(row), True

    return _write(
        db_path, purpose='index knowledge document', operation=insert)


def replace_document_index(
    db_path: str | os.PathLike,
    document_id: str,
    metadata: Mapping,
    chunks: Sequence[Mapping],
    assets: Sequence[Mapping] = (),
) -> dict | None:
    """Atomically replace parsed metadata, assets, chunks and FTS rows."""
    def replace(db: sqlite3.Connection) -> dict | None:
        existing = db.execute(
            'SELECT id FROM knowledge_documents WHERE id=?',
            (document_id,)).fetchone()
        if existing is None:
            return None
        replaced_assets = [
            str(item['stored_name']) for item in db.execute(
                'SELECT stored_name FROM knowledge_assets WHERE document_id=?',
                (document_id,)).fetchall()
        ]
        try:
            db.execute('''
                DELETE FROM knowledge_chunks_fts
                WHERE rowid IN (
                    SELECT id FROM knowledge_chunks WHERE document_id=?
                )
            ''', (document_id,))
        except sqlite3.OperationalError as exc:
            if 'no such table' not in str(exc).lower():
                raise
        db.execute(
            'DELETE FROM knowledge_chunks WHERE document_id=?',
            (document_id,))
        db.execute(
            'DELETE FROM knowledge_assets WHERE document_id=?',
            (document_id,))
        db.execute('''
            UPDATE knowledge_documents
            SET kind=?, method=?, warnings_json=?, text_chars=?,
                chunk_count=?, pages=?, updated_at=?
            WHERE id=?
        ''', tuple(metadata[key] for key in (
            'kind', 'method', 'warnings_json', 'text_chars', 'chunk_count',
            'pages', 'updated_at')) + (document_id,))
        for asset in assets:
            _insert_asset(db, document_id, asset)
        for chunk in chunks:
            _insert_chunk(db, document_id, chunk)
        row = db.execute(
            _DOCUMENT_WITH_ASSET_COUNTS + ' WHERE d.id=?',
            (document_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result['_replaced_asset_names'] = replaced_assets
        return result

    return _write(
        db_path, purpose='reindex knowledge document', operation=replace)


def delete_document(
    db_path: str | os.PathLike, document_id: str
) -> dict | None:
    def delete(db: sqlite3.Connection) -> dict | None:
        row = db.execute(
            'SELECT stored_name FROM knowledge_documents WHERE id=?',
            (document_id,)).fetchone()
        if row is None:
            return None
        asset_rows = db.execute(
            'SELECT stored_name FROM knowledge_assets WHERE document_id=?',
            (document_id,)).fetchall()
        try:
            db.execute('''
                DELETE FROM knowledge_chunks_fts
                WHERE rowid IN (
                    SELECT id FROM knowledge_chunks WHERE document_id=?
                )
            ''', (document_id,))
        except sqlite3.OperationalError as exc:
            if 'no such table' not in str(exc).lower():
                raise
        db.execute(
            'DELETE FROM knowledge_documents WHERE id=?', (document_id,))
        return {
            'source': str(row['stored_name']),
            'assets': [str(item['stored_name']) for item in asset_rows],
        }

    return _write(
        db_path, purpose='delete knowledge document', operation=delete)


def search_candidates(
    db_path: str | os.PathLike,
    *,
    fts_query: str,
    candidate_limit: int,
    fallback_limit: int = 5000,
) -> list[dict]:
    def candidates(db: sqlite3.Connection) -> list[dict]:
        if fts_query:
            try:
                rows = db.execute('''
                    SELECT c.id, c.document_id, c.ordinal, c.section,
                           c.location, c.content, d.name, d.kind,
                           bm25(knowledge_chunks_fts) AS bm25_score,
                           next.content AS next_content,
                           next.section AS next_section
                    FROM knowledge_chunks_fts
                    JOIN knowledge_chunks c
                      ON c.id=knowledge_chunks_fts.rowid
                    JOIN knowledge_documents d ON d.id=c.document_id
                    LEFT JOIN knowledge_chunks next
                      ON next.document_id=c.document_id
                     AND next.ordinal=c.ordinal + 1
                    WHERE knowledge_chunks_fts MATCH ?
                    ORDER BY bm25_score ASC
                    LIMIT ?
                ''', (fts_query, int(candidate_limit))).fetchall()
                if rows:
                    return _attach_assets(db, rows)
            except sqlite3.OperationalError as exc:
                text = str(exc).lower()
                if ('fts5' not in text and 'no such table' not in text
                        and 'malformed match' not in text):
                    raise
                logger.debug(
                    '[Knowledge.DB] FTS unavailable, using fallback: %s', exc)
        rows = db.execute('''
            SELECT c.id, c.document_id, c.ordinal, c.section, c.location,
                   c.content, d.name, d.kind, 0.0 AS bm25_score,
                   next.content AS next_content, next.section AS next_section
            FROM knowledge_chunks c
            JOIN knowledge_documents d ON d.id=c.document_id
            LEFT JOIN knowledge_chunks next
              ON next.document_id=c.document_id
             AND next.ordinal=c.ordinal + 1
            ORDER BY d.updated_at DESC, c.ordinal ASC
            LIMIT ?
        ''', (int(fallback_limit),)).fetchall()
        return _attach_assets(db, rows)

    return _read(db_path, candidates, default=[])


def _attach_assets(
    db: sqlite3.Connection,
    rows: Sequence[sqlite3.Row],
) -> list[dict]:
    """Attach ordered asset rows to candidate chunks in one bounded query."""
    output = [dict(row) for row in rows]
    chunk_ids = [int(row['id']) for row in output]
    if not chunk_ids:
        return output
    placeholders = ','.join('?' for _ in chunk_ids)
    linked = db.execute(f'''
        SELECT ca.chunk_id, ca.relation, ca.ordinal AS link_ordinal, a.*
        FROM knowledge_chunk_assets ca
        JOIN knowledge_assets a ON a.id=ca.asset_id
        WHERE ca.chunk_id IN ({placeholders})
        ORDER BY ca.chunk_id, ca.ordinal, a.ordinal
    ''', tuple(chunk_ids)).fetchall()
    by_chunk: dict[int, list[dict]] = {}
    for item in linked:
        value = dict(item)
        chunk_id = int(value.pop('chunk_id'))
        by_chunk.setdefault(chunk_id, []).append(value)
    for row in output:
        row['assets'] = by_chunk.get(int(row['id']), [])
    return output


def update_asset_enrichment(
    db_path: str | os.PathLike,
    asset_id: str,
    *,
    description: str,
    status: str,
    model: str = '',
    error: str = '',
    chunk_content: str | None = None,
    chunk_search_text: str | None = None,
) -> dict | None:
    """Update one asset and its searchable proxy in one writer snapshot."""
    def update(db: sqlite3.Connection) -> dict | None:
        existing = db.execute(
            'SELECT id FROM knowledge_assets WHERE id=?',
            (asset_id,)).fetchone()
        if existing is None:
            return None
        db.execute('''
            UPDATE knowledge_assets
            SET description=?, enrichment_status=?, enrichment_model=?,
                enrichment_error=?, updated_at=?
            WHERE id=?
        ''', (description, status, model, error, time.time(), asset_id))
        if chunk_content is not None and chunk_search_text is not None:
            linked = db.execute('''
                SELECT c.id FROM knowledge_chunks c
                JOIN knowledge_chunk_assets ca ON ca.chunk_id=c.id
                WHERE ca.asset_id=? AND ca.relation='primary'
            ''', (asset_id,)).fetchall()
            for row in linked:
                chunk_id = int(row['id'])
                db.execute('''
                    UPDATE knowledge_chunks SET content=?, search_text=?
                    WHERE id=?
                ''', (chunk_content, chunk_search_text, chunk_id))
                try:
                    db.execute(
                        'DELETE FROM knowledge_chunks_fts WHERE rowid=?',
                        (chunk_id,))
                    db.execute('''
                        INSERT INTO knowledge_chunks_fts(rowid, search_text)
                        VALUES(?,?)
                    ''', (chunk_id, chunk_search_text))
                except sqlite3.OperationalError as exc:
                    if 'no such table' not in str(exc).lower():
                        raise
        row = db.execute(
            'SELECT * FROM knowledge_assets WHERE id=?',
            (asset_id,)).fetchone()
        return dict(row) if row is not None else None

    return _write(
        db_path, purpose='update knowledge visual enrichment', operation=update)


__all__ = [
    'catalog_snapshot', 'delete_document', 'enrichment_activity',
    'find_asset_by_id', 'find_document_by_id',
    'find_document_by_sha',
    'claim_pending_asset', 'insert_document', 'mark_pending_assets_no_vision',
    'replace_document_index',
    'is_enabled', 'list_assets', 'list_documents', 'register_store',
    'search_candidates', 'set_enabled', 'set_visual_enrichment',
    'status_snapshot', 'tool_available', 'update_asset_enrichment',
    'visual_enrichment_enabled',
]
