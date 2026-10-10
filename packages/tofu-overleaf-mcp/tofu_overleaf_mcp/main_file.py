"""Recover the compiler's root input from its own bounded immutable TeX log."""
from __future__ import annotations

from pathlib import PurePosixPath
import re

from .contract import ExportError, schema


def compiled_main_file(log: bytes) -> str:
    match = re.search(r'^\*\*(.+)$', log[:8192].decode('utf-8', 'replace'), re.MULTILINE)
    if match is None:
        raise ExportError('overleaf_main_file_unavailable')
    value = match[1].strip().strip('"')
    # CLSI's standard root is /compile. Unknown absolute roots are not guessed.
    if value.startswith('/compile/'):
        value = value[len('/compile/'):]
    path = PurePosixPath(value)
    maximum = schema()['$defs']['PaperBuildReceiptV1']['properties']['main_file']['maxLength']
    if (not value or len(value) > maximum or path.is_absolute() or '..' in path.parts
            or any(ord(char) < 32 for char in value) or '\\' in value or ':' in value
            or path.suffix.lower() not in ('.tex', '.ltx')):
        raise ExportError('overleaf_main_file_unavailable')
    return path.as_posix()
