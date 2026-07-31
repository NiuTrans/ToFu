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
    """Compute the CANONICAL identity hash of a paper's text for DB caching.

    The hash is the single content-addressable identity of a paper — the key
    for ``paper_reports`` / ``paper_translations`` / ``paper_podcasts`` /
    ``paper_library`` and the on-disk figure-manifest / podcast-audio dirs.

    Canonicalization (2026-07-25, epic pt_c9a103fe): the input is ``strip()``ed
    BEFORE hashing, so leading/trailing whitespace never changes a paper's
    identity. This is load-bearing: the report-start route used to strip the
    client text before hashing while ingest hashed the raw parser output, so
    one paper got TWO hashes and every downstream gate keyed on the ingest
    hash (``has_report``, the podcast lookup) was falsely false for any paper
    whose parsed text carried trailing whitespace — 15/21 rows on the live
    DB. Normalizing INSIDE this one function makes every call site agree
    (strip is idempotent: hash(strip(t)) == hash(strip(strip(t)))).

    Returns '' for empty/whitespace-only input (callers guard on it).
    """
    if not text:
        return ''
    text = text.strip()
    if not text:
        return ''
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:32]


def resolve_paper_hash(client_hash, text):
    """Resolve the paper identity for a downstream request: prefer the
    caller-presented hash (minted by the server at ingest and carried by the
    client from the upload response / library row), fall back to computing
    the canonical hash from the request's text.

    The prefer-explicit rule is the second half of the identity-fork fix: a
    downstream writer (report start, translate, Q&A) must NEVER re-derive a
    paper's identity from a payload when the client can present the
    ingest-minted one — re-deriving is what forked the identity in the first
    place. ``client_hash`` is validated with ``_safe_hash_dir`` (hex, 8-64)
    so a malformed/traversal value silently falls back to the text hash.
    """
    phash = _safe_hash_dir((client_hash or '').strip())
    if phash:
        return phash
    return _paper_hash(text)


def _safe_hash_dir(phash):
    """Validate/normalize a paper hash to prevent path traversal.

    Returns the 32-char hex string or None if invalid.
    """
    if not phash or not isinstance(phash, str):
        return None
    if not re.fullmatch(r'[a-f0-9]{8,64}', phash):
        return None
    return phash
