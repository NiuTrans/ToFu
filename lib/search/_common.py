"""lib/search/_common.py — Shared constants and helpers for search engines."""

import re
import unicodedata
from html import unescape

__all__ = ['HEADERS', 'clean_text']

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) '
                  'Chrome/121.0.0.0 Safari/537.36',
    'Accept-Encoding': 'gzip, deflate',      # avoid brotli decode issues
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(s):
    """Clean a search result string: strip HTML, decode entities, remove junk chars."""
    if not s:
        return ''
    s = re.sub(r'<[^>]+>', ' ', s)
    s = unescape(s)
    s = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', s)
    s = re.sub(r'[\u200b\u200c\u200d\u200e\u200f\ufeff\u00ad]', '', s)
    s = unicodedata.normalize('NFC', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s
