"""Lightweight source admission shared by pinned native extraction adapters.

The generated catalog owns extensions; canonical outline owns explicit skips.
Keep parser packages and other transport adapters out of this boundary.
"""
import os


def is_code_path(name):
    from lib.repo_map import outline
    extension = os.path.splitext(name)[1].lower()
    if extension == '.py':
        return True
    if not extension or extension in outline.SKIP_EXTENSIONS:
        return False
    from ._native_catalog import CATALOG_EXTENSIONS
    return extension in CATALOG_EXTENSIONS
