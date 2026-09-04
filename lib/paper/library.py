"""Paper library schema + row→dict converter + soft caps.

Per CLAUDE.md, the bookshelf is server-side authoritative — every
upsert preserves existing big columns (parsed_text, images) when the
client only sends the small mutable state. Caps are deliberately generous
so users can store the full parsed PDF text + ample QA history.
"""


from lib.log import get_logger

logger = get_logger(__name__)


_PAPER_LIB_COLUMNS = (
    'id', 'title', 'pdf_url', 'pdf_filename', 'arxiv_id', 'paper_hash',
    'parsed_text', 'qa_history', 'images', 'babel_cache', 'page_count',
    'folder_id', 'created_at', 'updated_at',
)

# Soft caps to keep JSON payloads sane — the full report is in paper_reports,
# not in this row, so we only need enough parsed_text for Q&A / re-rendering.
_LIB_PARSED_TEXT_CAP = 200000
_LIB_QA_HISTORY_CAP = 50       # messages
_LIB_IMAGES_CAP = 60
_LIB_TITLE_CAP = 500


