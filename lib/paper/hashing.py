"""Paper hashing + filesystem-path safety.

Owns the upload directory constants. ``_paper_hash`` is content-addressable
(sha256 of paper text) and is the cache key for every paper-related table:
``paper_reports``, ``paper_translations``, ``paper_library``, the figure
manifest at ``uploads/papers/images/<phash>/manifest.json``.
"""

import hashlib
import os
import re


BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PAPER_DIR = os.path.join(BASE_DIR, 'uploads', 'papers')
PAPER_IMG_DIR = os.path.join(PAPER_DIR, 'images')
os.makedirs(PAPER_DIR, exist_ok=True)
os.makedirs(PAPER_IMG_DIR, exist_ok=True)


def _paper_hash(text):
    """Compute a stable hash of the paper text for DB caching."""
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:32]


def _safe_hash_dir(phash):
    """Validate/normalize a paper hash to prevent path traversal.

    Returns the 32-char hex string or None if invalid.
    """
    if not phash or not isinstance(phash, str):
        return None
    if not re.fullmatch(r'[a-f0-9]{8,64}', phash):
        return None
    return phash
