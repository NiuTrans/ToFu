"""Paper hashing + filesystem-path safety.

Owns the upload directory constants. ``_paper_hash`` is content-addressable
(sha256 of paper text) and is the cache key for every paper-related table:
``paper_reports``, ``paper_translations``, ``paper_library``, the figure
manifest at ``uploads/papers/images/<phash>/manifest.json``.
"""

import hashlib
import os
import re

from lib.log import get_logger

logger = get_logger(__name__)


BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Paper PDFs + extracted figure manifests are USER STATE referenced from the
# DB (paper_library.pdf_filename, /api/paper/images/ URLs), so they co-locate
# with the DB under the resolved runtime base rather than the code tree — see
# lib/runtime_paths.uploads_root(). Byte-identical to <repo>/uploads/papers in
# the default in-tree layout; falls back to it if runtime_paths is unavailable.
try:
    from lib.runtime_paths import uploads_root as _uploads_root
    PAPER_DIR = os.path.join(_uploads_root(), 'papers')
except Exception as e:  # pragma: no cover — defensive (keeps import-time robust)
    logger.debug('[Paper:Hashing] runtime_paths.uploads_root unavailable, '
                 'falling back to in-tree uploads/papers: %s', e)
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
