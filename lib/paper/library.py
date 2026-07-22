"""Paper library schema + row→dict converter + soft caps.

Per CLAUDE.md, the bookshelf is server-side authoritative — every
upsert preserves existing big columns (parsed_text, images) when the
client only sends the small mutable state. Caps are deliberately generous
so users can store the full parsed PDF text + ample QA history.
"""

import json

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


def _lib_row_to_dict(row):
    """Convert a paper_library row to the JSON shape the frontend expects."""
    def _j(raw, fallback):
        if not raw:
            return fallback
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError) as e:
            logger.debug('[Paper:Library] Failed to parse JSON column (%s): %s', e, raw[:80])
            return fallback

    return {
        'id': row['id'],
        'title': row['title'] or '',
        'pdfUrl': row['pdf_url'] or '',
        'pdfFilename': row['pdf_filename'] or '',
        'arxivId': row['arxiv_id'] or '',
        'paperHash': row['paper_hash'] or '',
        'parsedText': row['parsed_text'] or '',
        'qaHistory': _j(row['qa_history'], []),
        'images': _j(row['images'], []),
        'babelCache': _j(row['babel_cache'], {}),
        'pageCount': int(row['page_count'] or 0),
        'folderId': (row['folder_id'] or '') if 'folder_id' in row.keys() else '',
        'createdAt': int(row['created_at'] or 0),
        'updatedAt': int(row['updated_at'] or 0),
    }
