"""Canonical project identity and push-channel routing.

This leaf is the single non-storage authority for normalizing a project key.
It has no dependency on retired Board/Feed implementations.
"""

from __future__ import annotations

import hashlib
import re


_TRAILING_SEPARATORS = re.compile(r'[/\\]+$')


def normalize_project_path(project_path: str) -> str:
    """Strip trailing separators exactly as the browser project key does."""
    if not project_path:
        return ''
    return _TRAILING_SEPARATORS.sub('', str(project_path))


def project_channel_key(project_path: str) -> str:
    """Return the path-free 16-character routing key for project push."""
    normalized = normalize_project_path(project_path)
    if not normalized:
        return ''
    return hashlib.sha1(
        normalized.encode('utf-8', 'replace')).hexdigest()[:16]


__all__ = ['normalize_project_path', 'project_channel_key']
