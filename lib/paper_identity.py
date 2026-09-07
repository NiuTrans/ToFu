"""Dependency-light paper identity and storage paths.

This module deliberately lives outside the eager ``lib.paper`` package so
identity and path consumers do not import PDF, OCR, or LLM runtimes. It is the
single import path for paper hashes and paper storage directories.
"""

import hashlib
import os
import re

from lib.log import get_logger

logger = get_logger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    from lib.runtime_paths import uploads_root as _uploads_root
    PAPER_DIR = os.path.join(_uploads_root(), 'papers')
except Exception as e:  # pragma: no cover - defensive startup fallback
    logger.debug('[Paper:Identity] uploads_root unavailable, using tree: %s', e)
    PAPER_DIR = os.path.join(BASE_DIR, 'uploads', 'papers')
PAPER_IMG_DIR = os.path.join(PAPER_DIR, 'images')
os.makedirs(PAPER_DIR, exist_ok=True)
os.makedirs(PAPER_IMG_DIR, exist_ok=True)


def _paper_hash(text):
    """Canonical content identity: SHA-256 of stripped text, shortened to 32."""
    if not text:
        return ''
    text = text.strip()
    if not text:
        return ''
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:32]


def _safe_hash_dir(phash):
    """Return a validated hexadecimal paper identity or ``None``."""
    if not phash or not isinstance(phash, str):
        return None
    if not re.fullmatch(r'[a-f0-9]{8,64}', phash):
        return None
    return phash


def resolve_paper_hash(client_hash, text):
    """Prefer a valid ingest-minted identity; otherwise derive from text."""
    phash = _safe_hash_dir((client_hash or '').strip())
    return phash if phash else _paper_hash(text)


__all__ = [
    'BASE_DIR', 'PAPER_DIR', 'PAPER_IMG_DIR',
    '_paper_hash', '_safe_hash_dir', 'resolve_paper_hash',
]
