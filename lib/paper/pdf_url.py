"""Direct PDF-URL ingest helpers for the paper reader's fetch stream.

A pasted link such as
``https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash/resolve/main/DeepSeek_V41_Tech_Report.pdf``
is not an arXiv reference, so ``_extract_arxiv_id`` rejects it. These helpers
let ``/api/paper/fetch-arxiv-stream`` accept any direct public PDF link and
run it through the same cache → download → parse pipeline as an arXiv paper.

Loaded lazily by the route module (startup-boundary rule, see
``tests/paper/test_paper_arxiv_startup_boundary.py``); the network/parse seams
(``http_get`` / ``fetch_pdf_link_title``) are imported inside functions so
route tests can monkeypatch the module attribute exactly like
``routes.paper_pkg._arxiv.http_get``.
"""

from __future__ import annotations

import hashlib
import os
import re
from urllib.parse import unquote, urlparse

from lib.log import get_logger

logger = get_logger(__name__)

_PDF_URL_MAX_LEN = 2000


def looks_like_pdf_url(value) -> bool:
    """Cheap structural check: an http(s) URL whose path ends in ``.pdf``.

    No network or DNS work — the SSRF/DNS guard lives in
    :func:`lib.safe_fetch.validate_public_url`, called by the download path.
    """
    if not isinstance(value, str):
        return False
    candidate = value.strip()
    if not candidate or len(candidate) > _PDF_URL_MAX_LEN:
        return False
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return False
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        return False
    return parsed.path.lower().endswith('.pdf')


def _sanitized_url_basename(url: str) -> str:
    """Last URL path segment, decoded and reduced to a safe display basename."""
    base = os.path.basename(urlparse(url).path or '')
    try:
        base = unquote(base)
    except Exception as exc:  # malformed % escapes — keep the raw basename
        logger.debug('[Paper:PdfUrl] URL basename unquote failed: %s', exc)
    base = base.strip() or 'paper.pdf'
    if not base.lower().endswith('.pdf'):
        base += '.pdf'
    return base[:200]


def pdf_url_cache_name(url: str) -> str:
    """Deterministic cache filename for one direct PDF URL.

    Carries the URL SHA-256 (16 hex chars — collision-proof for a personal
    cache) plus a readable basename suffix. ``serve_paper_pdf`` basenames
    the route param before joining PAPER_DIR, so the suffix is also stripped
    of path separators as defence-in-depth.
    """
    digest = hashlib.sha256(url.strip().encode('utf-8')).hexdigest()[:16]
    suffix = re.sub(r'[^A-Za-z0-9._-]+', '_', _sanitized_url_basename(url))[:120]
    return f'pdfurl_{digest}_{suffix}'


def fetch_pdf_link_title(url: str) -> str:
    """Best-effort document title: the PDF's /Title metadata, else the
    filename stem. Never raises — a failure just yields the filename."""
    base = _sanitized_url_basename(url)
    stem = re.sub(r'\.pdf$', '', base, flags=re.IGNORECASE)
    stem = re.sub(r'[_-]+', ' ', stem).strip() or base
    try:
        import fitz  # pymupdf — lazy: keeps the PDF engine off this import path

        from lib.safe_fetch import fetch_public_bytes

        # Title metadata lives in the trailer, so a partial download would
        # still need most of the file; one bounded fetch of the whole body is
        # simpler and lands inside the same byte ceiling as the real download.
        body, _ct, _final = fetch_public_bytes(
            url, max_bytes=_title_probe_max_bytes(), timeout=20,
        )
        with fitz.open(stream=body, filetype='pdf') as doc:
            title = (doc.metadata or {}).get('title') or ''
            title = ' '.join(title.split())
            if title:
                return title[:300]
    except Exception as exc:
        logger.debug('[Paper:PdfUrl] metadata title probe failed: %s', exc)
    return stem


def _title_probe_max_bytes() -> int:
    # Optional metadata probing has its own transport budget, never source admission.
    return 200 * 1024 * 1024


__all__ = [
    'fetch_pdf_link_title', 'looks_like_pdf_url', 'pdf_url_cache_name',
]
