"""Search-only chunk extraction and disposable trigram maintenance; no authority writes."""
from collections.abc import Mapping

CHUNK_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS storage_search_chunks ("
    "user_id BIGINT NOT NULL,conversation_id TEXT NOT NULL,turn_id TEXT NOT NULL,"
    "chunk_index BIGINT NOT NULL,ordinal BIGINT NOT NULL,role TEXT NOT NULL,"
    "match_field TEXT NOT NULL,search_text TEXT NOT NULL,folded_text TEXT NOT NULL,"
    "generation_token TEXT NOT NULL,PRIMARY KEY(user_id,conversation_id,turn_id,chunk_index))",
    "CREATE INDEX IF NOT EXISTS idx_storage_search_chunks_owner ON "
    "storage_search_chunks(user_id,conversation_id,ordinal,turn_id,chunk_index)",
)
FIELDS = ('originalContent', 'content', 'translatedContent', 'thinking')


def field_text(value):
    """Extract only public text parts; never stringify image/tool/codec objects."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ''.join(part['text'] for part in value
                       if isinstance(part, Mapping) and part.get('type') == 'text'
                       and isinstance(part.get('text'), str))
    return ''


def chunks(projection, actor):
    """Yield separate fields, preserving original text even when content prefixes it."""
    if not isinstance(projection, Mapping):
        return
    role = projection.get('role')
    if role not in ('user', 'assistant'):
        role = 'user' if actor in ('human', 'critic', 'virtual_user', 'user') else 'assistant'
    index = 0
    for field in FIELDS:
        value = field_text(projection.get(field))
        for start in range(0, len(value), 3584):
            text = value[start:start + 4096]
            yield index, role, field, text, text.lower()
            index += 1
            if start + 4096 >= len(value):
                break


def install_fts(session):
    """Optional acceleration; triggers preserve external-content rowids on all writes."""
    if session.backend != 'sqlite':
        return
    import sqlite3
    try:
        session.execute("CREATE VIRTUAL TABLE IF NOT EXISTS storage_search_chunks_fts USING "
                        "fts5(folded_text,content='storage_search_chunks',content_rowid='rowid',tokenize='trigram')")
    except sqlite3.OperationalError:
        return
    session.execute("CREATE TRIGGER IF NOT EXISTS search_chunks_ai AFTER INSERT ON storage_search_chunks BEGIN "
                    "INSERT INTO storage_search_chunks_fts(rowid,folded_text) VALUES(new.rowid,new.folded_text); END")
    session.execute("CREATE TRIGGER IF NOT EXISTS search_chunks_ad AFTER DELETE ON storage_search_chunks BEGIN "
                    "INSERT INTO storage_search_chunks_fts(storage_search_chunks_fts,rowid,folded_text) "
                    "VALUES('delete',old.rowid,old.folded_text); END")
    session.execute("CREATE TRIGGER IF NOT EXISTS search_chunks_au AFTER UPDATE ON storage_search_chunks BEGIN "
                    "INSERT INTO storage_search_chunks_fts(storage_search_chunks_fts,rowid,folded_text) "
                    "VALUES('delete',old.rowid,old.folded_text); "
                    "INSERT INTO storage_search_chunks_fts(rowid,folded_text) VALUES(new.rowid,new.folded_text); END")
