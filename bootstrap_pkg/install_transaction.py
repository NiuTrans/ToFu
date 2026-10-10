"""Commit a verified installer candidate; never modify the selected env.

Entrypoint: commit(root). Dependencies: stdlib. Candidate paths belong to the
checkout install lock. On pointer publication failure restore configuration.
"""
import json
import os
from pathlib import Path
import tempfile


def _durable_replace(path: Path, content: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix='.install-commit-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def commit(root: Path) -> None:
    root = root.resolve()
    pointer = root / '.tofu_env.json'
    candidate = root / '.tofu_env.pending.json'
    configuration = root / '.env'
    staged_configuration = root / '.tofu-env.pending'
    for path in (pointer, candidate, configuration, staged_configuration):
        if path.is_symlink():
            raise ValueError(f'Refusing symlink at installation commit: {path.name}')
    marker = json.loads(candidate.read_bytes())
    prefix = Path(marker['env_prefix']).resolve()
    if not prefix.is_relative_to(root / '.tofu-envs') or not Path(marker['python']).is_file():
        raise ValueError('Candidate environment is missing or outside this checkout')
    previous = configuration.read_bytes() if configuration.exists() else None
    _durable_replace(configuration, staged_configuration.read_bytes())
    try:
        _durable_replace(pointer, candidate.read_bytes())
    except BaseException:
        if previous is None:
            configuration.unlink()
        else:
            _durable_replace(configuration, previous)
        raise
    candidate.unlink()
    staged_configuration.unlink()
