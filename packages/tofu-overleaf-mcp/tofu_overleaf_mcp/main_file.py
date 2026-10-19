"""Recover the compiler's root input from its own complete immutable TeX log."""
from __future__ import annotations

from pathlib import PurePosixPath
import re
import io

from .contract import ExportError, schema


def compiled_main_file(log) -> str:
    stream = io.BytesIO(log) if isinstance(log, bytes) else log
    lines = iter(stream.readline, b'')
    match = next((found for line in lines
                  if (found := re.match(r'^\*\*(.+)$', line.decode('utf-8', 'replace').rstrip('\r\n')))), None)
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
