"""Keep shell diagnostics usable while a selected project Python is unavailable.

Entry point block_unavailable_python modifies only the explicit child mapping.
Failing launchers live in the reconstructible tool cache, never the project venv.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import tempfile


def block_unavailable_python(environment: dict[str, str], *, cache_root: str) -> None:
    """Shadow Python/pip PATH names so an incomplete venv cannot fall through."""
    if os.name == 'nt':
        raise ValueError('Project .venv is incomplete; repair it before running commands')
    names = {'python', 'python3', 'pip', 'pip3'}
    for directory in environment.get('PATH', os.defpath).split(os.pathsep):
        if not directory:
            continue
        try:
            names.update(name for name in os.listdir(directory)
                         if re.fullmatch(r'(?:python|pip)\d*(?:\.\d+)*', name))
        except OSError:
            continue
    destination = Path(cache_root) / 'unavailable-project-python'
    destination.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise ValueError('Python diagnostic launcher directory must not be a symlink')
    script = (b'#!/bin/sh\n'
              b'echo "Project .venv is incomplete; repair it before running Python commands" >&2\n'
              b'exit 127\n')
    for name in names:
        target = destination / name
        if target.is_file() and not target.is_symlink() and target.read_bytes() == script:
            continue
        fd, temporary = tempfile.mkstemp(dir=destination)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(script)
                os.fchmod(stream.fileno(), 0o700)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    environment['PATH'] = str(destination) + os.pathsep + environment.get('PATH', os.defpath)
